from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status, Response
from loguru import logger
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.session import get_db
from models import SuppressionRule
from routers.ops_schemas import Page
from suppression.engine import _invalidate_cache, _matches

router = APIRouter(prefix="/ops/suppression", tags=["suppression"])

_MAX_PAGE = 100


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class SuppressionRuleCreate(BaseModel):
    match_field:      str = Field(..., description="HOST|ALERT_ID|MESSAGE|HOST_MESSAGE")
    pattern_type:     str = Field("EXACT", description="EXACT|PREFIX|CONTAINS|REGEX")
    host_pattern:     str | None = None
    message_pattern:  str | None = None
    alert_id_prefix:  str | None = None
    reason:           str | None = None
    expires_at:       datetime | None = None

    def validate_combo(self):
        f = self.match_field.upper()
        if f in ("HOST", "HOST_MESSAGE") and not self.host_pattern:
            raise ValueError("host_pattern required for HOST/HOST_MESSAGE match_field")
        if f in ("MESSAGE", "HOST_MESSAGE") and not self.message_pattern:
            raise ValueError("message_pattern required for MESSAGE/HOST_MESSAGE match_field")
        if f == "ALERT_ID" and not self.alert_id_prefix:
            raise ValueError("alert_id_prefix required for ALERT_ID match_field")


class SuppressionRulePatch(BaseModel):
    is_active:        str | None = None   # "1" or "0"
    reason:           str | None = None
    expires_at:       datetime | None = None
    host_pattern:     str | None = None
    message_pattern:  str | None = None


class SuppressionRuleOut(BaseModel):
    id:               uuid.UUID
    tenant_id:        str
    match_field:      str
    pattern_type:     str
    host_pattern:     str | None
    message_pattern:  str | None
    alert_id_prefix:  str | None
    reason:           str | None
    is_active:        str
    hit_count:        str
    last_hit_at:      datetime | None
    source_approval_id: uuid.UUID | None
    created_by:       str | None
    created_at:       datetime
    expires_at:       datetime | None

    model_config = {"from_attributes": True}


class SuppressionTestRequest(BaseModel):
    """Test a pattern against a sample alert without persisting anything."""
    match_field:      str
    pattern_type:     str = "EXACT"
    host_pattern:     str | None = None
    message_pattern:  str | None = None
    alert_id_prefix:  str | None = None
    # Sample alert to test against
    sample_host:      str = ""
    sample_message:   str = ""
    sample_alert_id:  str = ""


class SuppressionTestResult(BaseModel):
    matched:          bool
    matched_on:       str | None = None   # which field triggered the match
    rule_preview:     dict


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------

def _tenant(request: Request) -> str:
    t = getattr(request.state, "tenant_id", None)
    if not t:
        raise HTTPException(status_code=401, detail="Tenant not resolved")
    return t

TenantDep = Annotated[str, Depends(_tenant)]
DBDep     = Annotated[AsyncSession, Depends(get_db)]


# ---------------------------------------------------------------------------
# GET /ops/suppression
# ---------------------------------------------------------------------------

@router.get("", response_model=Page[SuppressionRuleOut])
async def list_rules(
    tenant_id: TenantDep,
    session: DBDep,
    page:      int  = Query(1, ge=1),
    page_size: int  = Query(25, ge=1, le=_MAX_PAGE),
    active_only: bool = Query(True),
) -> Page[SuppressionRuleOut]:
    base = select(SuppressionRule).where(SuppressionRule.tenant_id == tenant_id)
    if active_only:
        base = base.where(SuppressionRule.is_active == "1")

    total = (await session.execute(
        select(func.count()).select_from(base.subquery())
    )).scalar_one()

    offset = (page - 1) * page_size
    rows = (
        await session.execute(
            base.order_by(SuppressionRule.created_at.desc())
            .offset(offset).limit(page_size)
        )
    ).scalars().all()

    return Page(
        items=[SuppressionRuleOut.model_validate(r) for r in rows],
        total=total, page=page, page_size=page_size,
        has_next=(offset + page_size) < total,
    )


# ---------------------------------------------------------------------------
# POST /ops/suppression
# ---------------------------------------------------------------------------

@router.post("", response_model=SuppressionRuleOut, status_code=status.HTTP_201_CREATED)
async def create_rule(
    request: Request,
    body: SuppressionRuleCreate,
    tenant_id: TenantDep,
    session: DBDep,
) -> SuppressionRuleOut:
    try:
        body.validate_combo()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    rule = SuppressionRule(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        match_field=body.match_field.upper(),
        pattern_type=body.pattern_type.upper(),
        host_pattern=(body.host_pattern or "").lower() or None,
        message_pattern=(body.message_pattern or "").lower() or None,
        alert_id_prefix=(body.alert_id_prefix or "").lower() or None,
        reason=body.reason,
        expires_at=body.expires_at,
        is_active="1",
        created_by="api",
    )
    session.add(rule)
    await session.flush()
    await _invalidate_cache(tenant_id)

    logger.info(
        "Suppression rule created via API | tenant={} id={} field={} type={}",
        tenant_id, rule.id, rule.match_field, rule.pattern_type,
    )
    return SuppressionRuleOut.model_validate(rule)


# ---------------------------------------------------------------------------
# GET /ops/suppression/{rule_id}
# ---------------------------------------------------------------------------

@router.get("/{rule_id}", response_model=SuppressionRuleOut)
async def get_rule(
    rule_id: uuid.UUID,
    tenant_id: TenantDep,
    session: DBDep,
) -> SuppressionRuleOut:
    rule = await _require_rule(rule_id, tenant_id, session)
    return SuppressionRuleOut.model_validate(rule)


# ---------------------------------------------------------------------------
# PATCH /ops/suppression/{rule_id}
# ---------------------------------------------------------------------------

@router.patch("/{rule_id}", response_model=SuppressionRuleOut)
async def update_rule(
    rule_id: uuid.UUID,
    body: SuppressionRulePatch,
    tenant_id: TenantDep,
    session: DBDep,
) -> SuppressionRuleOut:
    rule = await _require_rule(rule_id, tenant_id, session)

    if body.is_active is not None:
        if body.is_active not in ("0", "1"):
            raise HTTPException(status_code=422, detail="is_active must be \'0\' or \'1\'")
        rule.is_active = body.is_active
    if body.reason      is not None: rule.reason          = body.reason
    if body.expires_at  is not None: rule.expires_at      = body.expires_at
    if body.host_pattern is not None:
        rule.host_pattern = body.host_pattern.lower()
    if body.message_pattern is not None:
        rule.message_pattern = body.message_pattern.lower()

    await session.flush()
    await _invalidate_cache(tenant_id)

    logger.info(
        "Suppression rule updated | tenant={} id={} is_active={}",
        tenant_id, rule_id, rule.is_active,
    )
    return SuppressionRuleOut.model_validate(rule)


# ---------------------------------------------------------------------------
# DELETE /ops/suppression/{rule_id}
# ---------------------------------------------------------------------------

@router.delete("/{rule_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
async def delete_rule(
    rule_id: uuid.UUID,
    tenant_id: TenantDep,
    session: DBDep,
):
    rule = await _require_rule(rule_id, tenant_id, session)
    await session.delete(rule)
    await _invalidate_cache(tenant_id)
    logger.info("Suppression rule deleted | tenant={} id={}", tenant_id, rule_id)


# ---------------------------------------------------------------------------
# POST /ops/suppression/test
# ---------------------------------------------------------------------------

@router.post("/test", response_model=SuppressionTestResult)
async def test_rule(body: SuppressionTestRequest) -> SuppressionTestResult:
    """
    Dry-run: test a pattern against sample alert fields without persisting.
    Safe to call repeatedly — no DB writes.
    """
    from schemas import AlertIngestionEvent, AlertSource, AlertSeverity

    sample = AlertIngestionEvent(
        alert_id    = body.sample_alert_id or "test-alert-id",
        tenant_id   = "test",
        source      = AlertSource.PRTG,
        severity    = AlertSeverity.UNKNOWN,
        host        = body.sample_host or "test-host",
        message     = body.sample_message or "test message",
        raw_payload = {},
        received_at = datetime.now(timezone.utc),
    )

    rule_dict = {
        "id":              "test-rule",
        "match_field":     body.match_field.upper(),
        "pattern_type":    body.pattern_type.upper(),
        "host_pattern":    (body.host_pattern or "").lower() or None,
        "message_pattern": (body.message_pattern or "").lower() or None,
        "alert_id_prefix": (body.alert_id_prefix or "").lower() or None,
        "is_active":       "1",
        "expires_at":      None,
    }

    matched = _matches(rule_dict, sample)
    return SuppressionTestResult(
        matched=matched,
        matched_on=body.match_field if matched else None,
        rule_preview=rule_dict,
    )


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

async def _require_rule(
    rule_id: uuid.UUID,
    tenant_id: str,
    session: AsyncSession,
) -> SuppressionRule:
    result = await session.execute(
        select(SuppressionRule)
        .where(SuppressionRule.id == rule_id)
        .where(SuppressionRule.tenant_id == tenant_id)
    )
    rule = result.scalar_one_or_none()
    if not rule:
        raise HTTPException(status_code=404, detail="Suppression rule not found")
    return rule
