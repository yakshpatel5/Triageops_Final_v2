"""
llm/context.py — Build EnrichmentContext for the LLM prompt.

For V1: populates alert fields + similar past alerts from DB.
For V1.2: add log line fetcher (Elasticsearch / CloudWatch).

HALLUCINATION RISK:
  similar_past_alerts feeds historical decisions into the prompt.
  If past decisions were wrong, the model will anchor on them.
  Only use enrichments with confidence_score >= 0.75 to avoid
  propagating low-confidence history.
"""

from __future__ import annotations

from typing import Any

from loguru import logger
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from llm.schemas import EnrichmentContext
from models import Alert, AlertEnrichment


# Minimum confidence to include a past alert as "similar" context.
# Prevents low-quality historical decisions from anchoring the model.
_MIN_CONTEXT_CONFIDENCE = 0.75


async def build_context(
    alert: Alert,
    session: AsyncSession,
) -> EnrichmentContext:
    """
    Assemble all context for one alert into an EnrichmentContext.
    Queries are scoped to tenant_id — no cross-tenant data leakage.
    """
    similar = await _fetch_similar_past_alerts(
        host=alert.host,
        tenant_id=alert.tenant_id,
        exclude_alert_id=str(alert.id),
        session=session,
    )

    logger.debug(
        "Context built | tenant={} alert_id={} similar_count={}",
        alert.tenant_id,
        alert.alert_id,
        len(similar),
    )

    return EnrichmentContext(
        alert_id=alert.alert_id,
        tenant_id=alert.tenant_id,
        source=alert.source.value,
        severity=alert.severity.value,
        host=alert.host,
        message=alert.message,
        recent_log_lines=[],        # V1.2: fetch from log aggregator
        similar_past_alerts=similar,
        host_metadata={},           # V1.2: fetch from CMDB / asset inventory
    )


async def _fetch_similar_past_alerts(
    host: str,
    tenant_id: str,
    exclude_alert_id: str,
    session: AsyncSession,
    limit: int = 3,
) -> list[dict[str, Any]]:
    """
    Fetch recent enriched alerts for the same host within same tenant.

    Scope: same tenant + same host + resolved (has enrichment) + high confidence.
    Returns list of dicts for JSON serialisation into the prompt.
    """
    try:
        result = await session.execute(
            select(
                Alert.alert_id,
                Alert.message,
                Alert.severity,
                Alert.received_at,
                AlertEnrichment.triage_decision,
                AlertEnrichment.confidence_score,
                AlertEnrichment.suggested_action,
            )
            .join(AlertEnrichment, Alert.id == AlertEnrichment.alert_db_id)
            .where(Alert.tenant_id == tenant_id)
            .where(Alert.host == host)
            .where(Alert.id != text(f"'{exclude_alert_id}'::uuid"))
            # Only include high-confidence past decisions
            .where(
                AlertEnrichment.confidence_score.cast(
                    # confidence stored as string "0.95" — cast for comparison
                    text("numeric")
                ) >= _MIN_CONTEXT_CONFIDENCE
            )
            .order_by(Alert.received_at.desc())
            .limit(limit)
        )
        rows = result.all()
    except Exception as exc:
        # Non-fatal — log but don't fail enrichment over missing context
        logger.warning(
            "Failed to fetch similar alerts | tenant={} host={} error={}",
            tenant_id,
            host,
            exc,
        )
        return []

    return [
        {
            "alert_id": row.alert_id,
            "message": row.message[:200],   # truncate to bound tokens
            "severity": row.severity.value,
            "received_at": row.received_at.isoformat(),
            "triage_decision": row.triage_decision,
            "confidence_score": row.confidence_score,
            "action_taken": row.suggested_action,
        }
        for row in rows
    ]
