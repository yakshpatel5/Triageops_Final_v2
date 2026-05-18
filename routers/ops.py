"""
routers/ops.py — Internal operations API.

All endpoints are:
  - Read-only (V1 constraint — no state mutation)
  - Tenant-scoped via X-API-Key → tenant_id (same auth middleware as webhooks)
  - Paginated (page + page_size query params)
  - Sorted newest-first by default

Endpoints:
  GET /ops/alerts                — paginated alert list with enrichment status
  GET /ops/alerts/{id}          — full alert detail with enrichment + approval
  GET /ops/alerts/{id}/enrichment — enrichment detail only
  GET /ops/approvals            — pending / recent approvals
  GET /ops/approvals/{id}       — approval detail with escalation history
  GET /ops/escalations          — escalation event history
  GET /ops/stats                — tenant dashboard stats (24h + all-time)
  GET /ops/stats/hotspots       — top N hosts by alert volume
  GET /ops/stats/distribution   — triage decision distribution
  GET /ops/health               — queue + DB liveness (ops use)
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from loguru import logger
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from db.session import get_db
from models import Alert, AlertEnrichment, EscalationEvent, SlackApproval
from pydantic import BaseModel
from routers.ops_schemas import (
    AlertDetail,
    AlertSummary,
    ApprovalDetail,
    ApprovalSummary,
    DecisionDistribution,
    EnrichmentDetail,
    EscalationSummary,
    HostHotspot,
    Page,
    RunbookRefOut,
    TenantStats,
)

router = APIRouter(prefix="/ops", tags=["ops"])

class LoginRequest(BaseModel):
    api_key: str

# Max page_size cap — prevents runaway queries
_MAX_PAGE_SIZE = 100
_DEFAULT_PAGE_SIZE = 25


# ---------------------------------------------------------------------------
# Dependency: resolve tenant_id from request.state (set by auth middleware)
# ---------------------------------------------------------------------------

def get_tenant(request: Request) -> str:
    tenant_id = getattr(request.state, "tenant_id", None)
    if not tenant_id:
        raise HTTPException(status_code=401, detail="Tenant not resolved")
    return tenant_id


TenantDep = Annotated[str, Depends(get_tenant)]
DBDep = Annotated[AsyncSession, Depends(get_db)]


# ---------------------------------------------------------------------------
# Auth: Login / Logout (Cookie-based)
# ---------------------------------------------------------------------------

@router.post("/auth/login")
async def login(
    request: LoginRequest,
    session: DBDep,
) -> dict:
    """
    Verify API key and set an httpOnly cookie for the frontend.
    This protects the key from XSS.
    """
    from middleware.auth import _resolve_tenant
    
    tenant_id = await _resolve_tenant(request.api_key, session)
    if not tenant_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key"
        )
    
    response = JSONResponse(content={"status": "ok", "tenant_id": tenant_id})
    
    # Set httpOnly cookie
    # In production, secure=True is required
    response.set_cookie(
        key="triageops_session",
        value=request.api_key,
        httponly=True,
        secure=os.getenv("APP_ENV") == "production",
        samesite="lax",
        max_age=86400 * 7,  # 7 days
    )
    return response


@router.post("/auth/logout")
async def logout() -> dict:
    """Clear the session cookie."""
    response = JSONResponse(content={"status": "ok"})
    response.delete_cookie("triageops_session")
    return response


@router.get("/auth/verify")
async def verify_auth(tenant_id: TenantDep) -> dict:
    """Check if the current session/key is valid."""
    return {"status": "ok", "tenant_id": tenant_id}


# ---------------------------------------------------------------------------
# GET /ops/alerts
# ---------------------------------------------------------------------------

@router.get("/alerts", response_model=Page[AlertSummary])
async def list_alerts(
    tenant_id: TenantDep,
    session: DBDep,
    page:      int = Query(1,  ge=1),
    page_size: int = Query(_DEFAULT_PAGE_SIZE, ge=1, le=_MAX_PAGE_SIZE),
    severity:  str | None = Query(None, description="Filter: CRITICAL|HIGH|MEDIUM|LOW|UNKNOWN"),
    source:    str | None = Query(None, description="Filter: PRTG|DATADOG"),
    decision:  str | None = Query(None, description="Filter: CRITICAL|NOISE|NEEDS_REVIEW"),
    host:      str | None = Query(None, description="Exact host filter"),
    since:     datetime | None = Query(None, description="ISO datetime lower bound on received_at"),
) -> Page[AlertSummary]:
    """
    Paginated alert list. Optionally joins enrichment to surface triage_decision
    and confidence_score without a second request.
    """
    # Base query — left-join enrichment for denormalised decision column
    base = (
        select(
            Alert,
            AlertEnrichment.triage_decision,
            AlertEnrichment.confidence_score,
        )
        .outerjoin(AlertEnrichment, AlertEnrichment.alert_db_id == Alert.id)
        .where(Alert.tenant_id == tenant_id)
    )

    if severity:
        base = base.where(Alert.severity == severity.upper())
    if source:
        base = base.where(Alert.source == source.upper())
    if decision:
        base = base.where(AlertEnrichment.triage_decision == decision.upper())
    if host:
        base = base.where(Alert.host == host)
    if since:
        base = base.where(Alert.received_at >= since)

    # Total count (same filters, no pagination)
    count_q = select(func.count()).select_from(base.subquery())
    total = (await session.execute(count_q)).scalar_one()

    # Paginated results
    offset = (page - 1) * page_size
    rows = (
        await session.execute(
            base.order_by(Alert.created_at.desc()).offset(offset).limit(page_size)
        )
    ).all()

    items = [
        AlertSummary(
            id=row.Alert.id,
            alert_id=row.Alert.alert_id,
            tenant_id=row.Alert.tenant_id,
            source=row.Alert.source.value,
            severity=row.Alert.severity.value,
            host=row.Alert.host,
            message=row.Alert.message,
            received_at=row.Alert.received_at,
            created_at=row.Alert.created_at,
            triage_decision=row.triage_decision,
            confidence_score=row.confidence_score,
        )
        for row in rows
    ]

    return Page(
        items=items,
        total=total,
        page=page,
        page_size=page_size,
        has_next=(offset + page_size) < total,
    )


# ---------------------------------------------------------------------------
# GET /ops/alerts/{alert_id}
# ---------------------------------------------------------------------------

@router.get("/alerts/{alert_id}", response_model=AlertDetail)
async def get_alert(
    alert_id: uuid.UUID,
    tenant_id: TenantDep,
    session: DBDep,
) -> AlertDetail:
    """Full alert detail with enrichment and latest approval."""
    alert = await _require_alert(alert_id, tenant_id, session)

    enrichment_row = (
        await session.execute(
            select(AlertEnrichment).where(AlertEnrichment.alert_db_id == alert.id)
        )
    ).scalar_one_or_none()

    approval_row = (
        await session.execute(
            select(SlackApproval)
            .where(SlackApproval.alert_db_id == alert.id)
            .order_by(SlackApproval.sent_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    return AlertDetail(
        id=alert.id,
        alert_id=alert.alert_id,
        tenant_id=alert.tenant_id,
        source=alert.source.value,
        severity=alert.severity.value,
        host=alert.host,
        message=alert.message,
        received_at=alert.received_at,
        created_at=alert.created_at,
        triage_decision=enrichment_row.triage_decision if enrichment_row else None,
        confidence_score=enrichment_row.confidence_score if enrichment_row else None,
        enrichment=_serialise_enrichment(enrichment_row),
        latest_approval=_serialise_approval_summary(approval_row),
    )


# ---------------------------------------------------------------------------
# GET /ops/alerts/{alert_id}/enrichment
# ---------------------------------------------------------------------------

@router.get("/alerts/{alert_id}/enrichment", response_model=EnrichmentDetail)
async def get_enrichment(
    alert_id: uuid.UUID,
    tenant_id: TenantDep,
    session: DBDep,
) -> EnrichmentDetail:
    """Enrichment detail for a single alert. 404 if not yet enriched."""
    alert = await _require_alert(alert_id, tenant_id, session)
    enrichment = (
        await session.execute(
            select(AlertEnrichment).where(AlertEnrichment.alert_db_id == alert.id)
        )
    ).scalar_one_or_none()
    if not enrichment:
        raise HTTPException(status_code=404, detail="Alert not yet enriched")
    return _serialise_enrichment(enrichment)


# ---------------------------------------------------------------------------
# GET /ops/approvals
# ---------------------------------------------------------------------------

@router.get("/approvals", response_model=Page[ApprovalSummary])
async def list_approvals(
    tenant_id: TenantDep,
    session: DBDep,
    page:      int = Query(1, ge=1),
    page_size: int = Query(_DEFAULT_PAGE_SIZE, ge=1, le=_MAX_PAGE_SIZE),
    status_filter: str | None = Query(None, alias="status",
                                      description="PENDING|ACTIONED|EXPIRED|ERROR"),
) -> Page[ApprovalSummary]:
    """Recent approvals for this tenant, newest first."""
    base = select(SlackApproval).where(SlackApproval.tenant_id == tenant_id)
    if status_filter:
        base = base.where(SlackApproval.status == status_filter.upper())

    total = (await session.execute(
        select(func.count()).select_from(base.subquery())
    )).scalar_one()

    offset = (page - 1) * page_size
    rows = (
        await session.execute(
            base.order_by(SlackApproval.sent_at.desc()).offset(offset).limit(page_size)
        )
    ).scalars().all()

    return Page(
        items=[_serialise_approval_summary(r) for r in rows],
        total=total,
        page=page,
        page_size=page_size,
        has_next=(offset + page_size) < total,
    )


# ---------------------------------------------------------------------------
# GET /ops/approvals/{approval_id}
# ---------------------------------------------------------------------------

@router.get("/approvals/{approval_id}", response_model=ApprovalDetail)
async def get_approval(
    approval_id: uuid.UUID,
    tenant_id: TenantDep,
    session: DBDep,
) -> ApprovalDetail:
    approval = (
        await session.execute(
            select(SlackApproval)
            .where(SlackApproval.id == approval_id)
            .where(SlackApproval.tenant_id == tenant_id)
        )
    ).scalar_one_or_none()
    if not approval:
        raise HTTPException(status_code=404, detail="Approval not found")
    return ApprovalDetail(
        id=approval.id,
        enrichment_id=approval.enrichment_id,
        status=approval.status,
        action=approval.action,
        actioned_by=approval.actioned_by,
        actioned_by_name=approval.actioned_by_name,
        actioned_at=approval.actioned_at,
        action_note=approval.action_note,
        slack_channel=approval.slack_channel,
        slack_ts=approval.slack_ts,
        sent_at=approval.sent_at,
        expires_at=approval.expires_at,
    )


# ---------------------------------------------------------------------------
# GET /ops/escalations
# ---------------------------------------------------------------------------

@router.get("/escalations", response_model=Page[EscalationSummary])
async def list_escalations(
    tenant_id: TenantDep,
    session: DBDep,
    page:      int = Query(1, ge=1),
    page_size: int = Query(_DEFAULT_PAGE_SIZE, ge=1, le=_MAX_PAGE_SIZE),
    provider:  str | None = Query(None, description="PAGERDUTY|OPSGENIE|WEBHOOK"),
    esc_status: str | None = Query(None, alias="status",
                                   description="TRIGGERED|FAILED|SKIPPED"),
) -> Page[EscalationSummary]:
    base = select(EscalationEvent).where(EscalationEvent.tenant_id == tenant_id)
    if provider:
        base = base.where(EscalationEvent.provider == provider.upper())
    if esc_status:
        base = base.where(EscalationEvent.status == esc_status.upper())

    total = (await session.execute(
        select(func.count()).select_from(base.subquery())
    )).scalar_one()

    offset = (page - 1) * page_size
    rows = (
        await session.execute(
            base.order_by(EscalationEvent.triggered_at.desc())
            .offset(offset).limit(page_size)
        )
    ).scalars().all()

    return Page(
        items=[
            EscalationSummary(
                id=r.id,
                provider=r.provider,
                status=r.status,
                provider_incident_id=r.provider_incident_id,
                provider_incident_url=r.provider_incident_url,
                triggered_by=r.triggered_by,
                triggered_at=r.triggered_at,
                error_detail=r.error_detail,
            )
            for r in rows
        ],
        total=total,
        page=page,
        page_size=page_size,
        has_next=(offset + page_size) < total,
    )


# ---------------------------------------------------------------------------
# GET /ops/stats
# ---------------------------------------------------------------------------

@router.get("/stats", response_model=TenantStats)
async def tenant_stats(
    tenant_id: TenantDep,
    session: DBDep,
) -> TenantStats:
    """Aggregated dashboard metrics for the authenticated tenant."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)

    # ── Alert counts ──────────────────────────────────────────────────────
    def _alert_count(extra_filters=None):
        q = select(func.count(Alert.id)).where(Alert.tenant_id == tenant_id)
        if extra_filters:
            for f in extra_filters:
                q = q.where(f)
        return q

    total_alerts  = (await session.execute(_alert_count())).scalar_one()
    alerts_24h    = (await session.execute(_alert_count([Alert.received_at >= cutoff]))).scalar_one()

    # Decisions in last 24h via join
    decision_counts = (
        await session.execute(
            select(AlertEnrichment.triage_decision, func.count())
            .join(Alert, Alert.id == AlertEnrichment.alert_db_id)
            .where(Alert.tenant_id == tenant_id)
            .where(Alert.received_at >= cutoff)
            .group_by(AlertEnrichment.triage_decision)
        )
    ).all()
    dec_map = {row[0]: row[1] for row in decision_counts}
    critical_24h     = dec_map.get("CRITICAL", 0)
    noise_24h        = dec_map.get("NOISE", 0)
    needs_review_24h = dec_map.get("NEEDS_REVIEW", 0)

    # Noise ratio
    denom = critical_24h + noise_24h
    noise_ratio = round(noise_24h / denom, 4) if denom > 0 else None

    # ── Pending approvals ─────────────────────────────────────────────────
    pending_approvals = (
        await session.execute(
            select(func.count(SlackApproval.id))
            .where(SlackApproval.tenant_id == tenant_id)
            .where(SlackApproval.status == "PENDING")
        )
    ).scalar_one()

    # ── Escalations ───────────────────────────────────────────────────────
    escalations_24h = (
        await session.execute(
            select(func.count(EscalationEvent.id))
            .where(EscalationEvent.tenant_id == tenant_id)
            .where(EscalationEvent.triggered_at >= cutoff)
            .where(EscalationEvent.status == "TRIGGERED")
        )
    ).scalar_one()

    total_escalations = (
        await session.execute(
            select(func.count(EscalationEvent.id))
            .where(EscalationEvent.tenant_id == tenant_id)
            .where(EscalationEvent.status == "TRIGGERED")
        )
    ).scalar_one()

    # ── LLM quality metrics ───────────────────────────────────────────────
    quality = (
        await session.execute(
            select(
                func.avg(func.cast(AlertEnrichment.confidence_score, sa_numeric())),
                func.avg(func.cast(AlertEnrichment.latency_ms, sa_numeric())),
            )
            .join(Alert, Alert.id == AlertEnrichment.alert_db_id)
            .where(Alert.tenant_id == tenant_id)
            .where(Alert.received_at >= cutoff)
        )
    ).one()
    avg_confidence = float(round(quality[0], 4)) if quality[0] else None
    avg_latency    = float(round(quality[1], 2)) if quality[1] else None

    return TenantStats(
        tenant_id=tenant_id,
        alerts_24h=alerts_24h,
        critical_24h=critical_24h,
        noise_24h=noise_24h,
        needs_review_24h=needs_review_24h,
        pending_approvals=pending_approvals,
        escalations_24h=escalations_24h,
        total_alerts=total_alerts,
        total_escalations=total_escalations,
        avg_confidence_24h=avg_confidence,
        avg_latency_ms_24h=avg_latency,
        noise_ratio_24h=noise_ratio,
    )


# ---------------------------------------------------------------------------
# GET /ops/stats/hotspots
# ---------------------------------------------------------------------------

@router.get("/stats/hotspots", response_model=list[HostHotspot])
async def hotspots(
    tenant_id: TenantDep,
    session: DBDep,
    hours: int = Query(24, ge=1, le=168, description="Lookback window in hours"),
    limit: int = Query(10, ge=1, le=50),
) -> list[HostHotspot]:
    """Top hosts by alert volume in the given window."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)

    rows = (
        await session.execute(
            select(
                Alert.host,
                func.count(Alert.id).label("alert_count"),
                func.sum(
                    func.cast(Alert.severity == "CRITICAL", sa_integer())
                ).label("critical_count"),
                func.max(Alert.received_at).label("last_seen"),
            )
            .where(Alert.tenant_id == tenant_id)
            .where(Alert.received_at >= cutoff)
            .group_by(Alert.host)
            .order_by(func.count(Alert.id).desc())
            .limit(limit)
        )
    ).all()

    return [
        HostHotspot(
            host=r.host,
            alert_count=r.alert_count,
            critical_count=int(r.critical_count or 0),
            last_seen=r.last_seen,
        )
        for r in rows
    ]


# ---------------------------------------------------------------------------
# GET /ops/stats/distribution
# ---------------------------------------------------------------------------

@router.get("/stats/distribution", response_model=list[DecisionDistribution])
async def decision_distribution(
    tenant_id: TenantDep,
    session: DBDep,
    hours: int = Query(24, ge=1, le=720),
) -> list[DecisionDistribution]:
    """Triage decision distribution as counts + percentages."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)

    rows = (
        await session.execute(
            select(AlertEnrichment.triage_decision, func.count().label("cnt"))
            .join(Alert, Alert.id == AlertEnrichment.alert_db_id)
            .where(Alert.tenant_id == tenant_id)
            .where(Alert.received_at >= cutoff)
            .group_by(AlertEnrichment.triage_decision)
            .order_by(func.count().desc())
        )
    ).all()

    total = sum(r.cnt for r in rows)
    return [
        DecisionDistribution(
            decision=r.triage_decision,
            count=r.cnt,
            percentage=round(r.cnt / total * 100, 2) if total else 0.0,
        )
        for r in rows
    ]


# ---------------------------------------------------------------------------
# GET /ops/health  (deep liveness — checks DB + Redis)
# ---------------------------------------------------------------------------

@router.get("/auth/verify", summary="Verify API key and return tenant info")
async def verify_auth(tenant_id: TenantDep) -> dict:
    """Validate an API key. Used by the React dashboard login flow."""
    return {"status": "ok", "tenant_id": tenant_id}


@router.get("/health", include_in_schema=False)
async def ops_health(session: DBDep) -> dict:
    """Deep health check — verifies DB connectivity."""
    try:
        await session.execute(text("SELECT 1"))
        db_ok = True
    except Exception as exc:
        logger.error("ops/health DB check failed: {}", exc)
        db_ok = False

    return {
        "db": "ok" if db_ok else "error",
        "status": "ok" if db_ok else "degraded",
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _require_alert(
    alert_id: uuid.UUID,
    tenant_id: str,
    session: AsyncSession,
) -> Alert:
    alert = (
        await session.execute(
            select(Alert)
            .where(Alert.id == alert_id)
            .where(Alert.tenant_id == tenant_id)
        )
    ).scalar_one_or_none()
    if not alert:
        raise HTTPException(status_code=404, detail="Alert not found")
    return alert


def _serialise_enrichment(e: AlertEnrichment | None) -> EnrichmentDetail | None:
    if e is None:
        return None
    refs = e.runbook_refs or []
    return EnrichmentDetail(
        id=e.id,
        triage_decision=e.triage_decision,
        confidence_score=e.confidence_score,
        llm_reasoning=e.llm_reasoning,
        suggested_action=e.suggested_action,
        runbook_refs=[RunbookRefOut(**r) for r in refs if isinstance(r, dict)],
        prompt_version=e.prompt_version,
        model_used=e.model_used,
        prompt_tokens=e.prompt_tokens,
        completion_tokens=e.completion_tokens,
        latency_ms=e.latency_ms,
        enriched_at=e.enriched_at,
    )


def _serialise_approval_summary(a: SlackApproval | None) -> ApprovalSummary | None:
    if a is None:
        return None
    return ApprovalSummary(
        id=a.id,
        status=a.status,
        action=a.action,
        actioned_by_name=a.actioned_by_name,
        actioned_at=a.actioned_at,
        slack_channel=a.slack_channel,
        sent_at=a.sent_at,
        expires_at=a.expires_at,
    )


# SQLAlchemy type helpers for aggregate casts
from sqlalchemy import Numeric as _Numeric, Integer as _Integer  # noqa: E402

def sa_numeric():
    return _Numeric(precision=10, scale=4)

def sa_integer():
    return _Integer()
