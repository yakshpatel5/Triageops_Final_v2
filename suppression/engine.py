"""
suppression/engine.py — Alert suppression rule matching engine.

Called from webhook ingest BEFORE LLM enrichment. A matching rule means:
  - Alert is still persisted (is_suppressed="1") for audit
  - Alert skips LLM enrichment (no OpenAI cost)
  - Alert skips Slack notification (no noise for the NOC team)

Cache layer: active rules are stored in Redis (60s TTL). On ingest the hot
path hits Redis only — no DB query on every alert.

Pattern types:
  EXACT    — case-insensitive equality
  PREFIX   — case-insensitive str.startswith
  CONTAINS — case-insensitive substring match
  REGEX    — re.search (case-insensitive). Only creatable via API, never
             auto-generated from Slack SUPPRESS (too risky without review).

Fail-open: any exception in check_suppression() returns None so ingest
continues normally. Suppression bugs must never block alert delivery.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from loguru import logger
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from db.session import AsyncSessionLocal, get_session
from models import SuppressionRule
from schemas import AlertIngestionEvent

_CACHE_TTL_SECS   = 60
_CACHE_KEY_PREFIX = "suppression_rules"


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

async def check_suppression(
    event: AlertIngestionEvent,
    session: AsyncSession,
) -> SuppressionRule | None:
    """Return first matching SuppressionRule or None. Never raises (fail-open)."""
    try:
        rules = await _load_rules(event.tenant_id, session)
    except Exception as exc:
        logger.warning(
            "Suppression rule load failed (fail-open) | tenant={} error={}",
            event.tenant_id, exc,
        )
        return None

    for rule in rules:
        if _matches(rule, event):
            import asyncio
            asyncio.create_task(_record_hit(rule["id"], event.tenant_id))
            logger.info(
                "Alert suppressed | tenant={} alert_id={} rule_id={} "
                "field={} type={} host_pat={!r}",
                event.tenant_id, event.alert_id, rule["id"],
                rule["match_field"], rule["pattern_type"], rule.get("host_pattern"),
            )
            return _dict_to_rule(rule)
    return None


async def build_rule_from_suppression(
    approval_id: str,
    alert: Any,
    actioned_by: str,
    reason: str | None = None,
) -> SuppressionRule | None:
    """
    Auto-create suppression rules from a Slack SUPPRESS action.
    Creates HOST EXACT + HOST_MESSAGE CONTAINS rules (conservative defaults).
    Never creates REGEX rules automatically.
    """
    if not alert.host or alert.host == "unknown-host":
        logger.warning(
            "No suppression rules created — alert has generic host | "
            "tenant={} host={!r}", alert.tenant_id, alert.host,
        )
        return None

    note = reason or f"Auto-suppressed via Slack by {actioned_by}"
    rules_to_add = [
        SuppressionRule(
            id=uuid.uuid4(),
            tenant_id=alert.tenant_id,
            match_field="HOST",
            pattern_type="EXACT",
            host_pattern=alert.host.lower(),
            reason=note,
            source_approval_id=uuid.UUID(approval_id),
            created_by=actioned_by,
            is_active="1",
        )
    ]

    msg_prefix = (alert.message or "")[:80].strip().lower()
    if msg_prefix:
        rules_to_add.append(SuppressionRule(
            id=uuid.uuid4(),
            tenant_id=alert.tenant_id,
            match_field="HOST_MESSAGE",
            pattern_type="CONTAINS",
            host_pattern=alert.host.lower(),
            message_pattern=msg_prefix,
            reason=f"{note} (host+message)",
            source_approval_id=uuid.UUID(approval_id),
            created_by=actioned_by,
            is_active="1",
        ))

    async with get_session() as session:
        for rule in rules_to_add:
            session.add(rule)

    await _invalidate_cache(alert.tenant_id)
    logger.info(
        "Suppression rules created | tenant={} count={} host={}",
        alert.tenant_id, len(rules_to_add), alert.host,
    )
    return rules_to_add[0]


# ---------------------------------------------------------------------------
# Pattern matching
# ---------------------------------------------------------------------------

def _matches(rule: dict, event: AlertIngestionEvent) -> bool:
    # Skip expired rules
    expires_at = rule.get("expires_at")
    if expires_at:
        try:
            exp = datetime.fromisoformat(str(expires_at))
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            if exp < datetime.now(timezone.utc):
                return False
        except (ValueError, TypeError):
            pass

    field    = rule.get("match_field", "HOST")
    ptype    = rule.get("pattern_type", "EXACT")
    host_pat = (rule.get("host_pattern")    or "").lower()
    msg_pat  = (rule.get("message_pattern") or "").lower()
    id_pat   = (rule.get("alert_id_prefix") or "").lower()

    if field == "HOST":
        return _cmp(event.host.lower(), host_pat, ptype)
    if field == "ALERT_ID":
        return _cmp(event.alert_id.lower(), id_pat, ptype)
    if field == "MESSAGE":
        return _cmp(event.message.lower(), msg_pat, ptype)
    if field == "HOST_MESSAGE":
        host_ok = _cmp(event.host.lower(),    host_pat, ptype) if host_pat else True
        msg_ok  = _cmp(event.message.lower(), msg_pat,  ptype) if msg_pat  else True
        return host_ok and msg_ok
    return False


def _cmp(value: str, pattern: str, ptype: str) -> bool:
    if not pattern:
        return False
    if ptype == "EXACT":
        return value == pattern
    if ptype == "PREFIX":
        return value.startswith(pattern)
    if ptype == "CONTAINS":
        return pattern in value
    if ptype == "REGEX":
        try:
            return bool(re.search(pattern, value, re.IGNORECASE))
        except re.error:
            logger.warning("Invalid regex in suppression rule: {!r}", pattern)
            return False
    return False


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------

def _ck(tenant_id: str) -> str:
    return f"{_CACHE_KEY_PREFIX}:{tenant_id}"


async def _load_rules(tenant_id: str, session: AsyncSession) -> list[dict]:
    try:
        from middleware.rate_limit import _get_redis
        cached = await _get_redis().get(_ck(tenant_id))
        if cached:
            return json.loads(cached)
    except Exception:
        pass

    result = await session.execute(
        select(SuppressionRule)
        .where(SuppressionRule.tenant_id == tenant_id)
        .where(SuppressionRule.is_active == "1")
        .order_by(SuppressionRule.created_at.desc())
        .limit(500)
    )
    dicts = [_rule_to_dict(r) for r in result.scalars().all()]

    try:
        from middleware.rate_limit import _get_redis
        await _get_redis().setex(_ck(tenant_id), _CACHE_TTL_SECS, json.dumps(dicts, default=str))
    except Exception:
        pass

    return dicts


async def _invalidate_cache(tenant_id: str) -> None:
    try:
        from middleware.rate_limit import _get_redis
        await _get_redis().delete(_ck(tenant_id))
    except Exception:
        pass


async def _record_hit(rule_id: str, tenant_id: str) -> None:
    """Increment hit_count atomically. Fire-and-forget — never blocks ingest."""
    try:
        async with AsyncSessionLocal() as session:
            rule_uuid = uuid.UUID(rule_id)
            # Read current count, increment, write back
            result = await session.execute(
                select(SuppressionRule.hit_count)
                .where(SuppressionRule.id == rule_uuid)
            )
            row = result.scalar_one_or_none()
            if row is not None:
                new_count = str(int(row or "0") + 1)
                await session.execute(
                    update(SuppressionRule)
                    .where(SuppressionRule.id == rule_uuid)
                    .values(hit_count=new_count, last_hit_at=datetime.now(timezone.utc))
                )
                await session.commit()
    except Exception as exc:
        logger.debug("Suppression hit record failed (non-fatal): {}", exc)


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------

def _rule_to_dict(rule: SuppressionRule) -> dict:
    return {
        "id":               str(rule.id),
        "tenant_id":        rule.tenant_id,
        "match_field":      rule.match_field,
        "pattern_type":     rule.pattern_type,
        "host_pattern":     rule.host_pattern,
        "message_pattern":  rule.message_pattern,
        "alert_id_prefix":  rule.alert_id_prefix,
        "is_active":        rule.is_active,
        "expires_at":       rule.expires_at.isoformat() if rule.expires_at else None,
    }


def _dict_to_rule(d: dict) -> SuppressionRule:
    r = object.__new__(SuppressionRule)
    for k, v in d.items():
        object.__setattr__(r, k, v)
    return r
