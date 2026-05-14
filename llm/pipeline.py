"""
llm/pipeline.py — Orchestration layer for alert enrichment.

Called by the Celery task. Owns the full flow:
  1. Load Alert from DB
  2. Build enrichment context (past alerts, log lines)
  3. Call LLM classifier
  4. Persist AlertEnrichment (upsert — safe to re-enrich)
  5. Return EnrichmentResult for downstream (Slack notification in Week 3)

Error handling strategy:
  - DB not found: raise, Celery will retry
  - LLM retryable error: raise, Celery will retry
  - LLM non-retryable error: persist NEEDS_REVIEW + human review, don't retry
  - DB write failure: raise, Celery will retry (idempotent upsert)
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import sentry_sdk
from loguru import logger
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from db.session import get_session
from llm.client import LLMError, classify_alert
from llm.context import build_context
from llm.schemas import EnrichmentResult
from models import Alert, AlertEnrichment


class AlertNotFound(Exception):
    pass


async def run_enrichment_pipeline(
    db_alert_id: str,
    tenant_id: str,
) -> EnrichmentResult:
    """
    Full enrichment pipeline for one alert.

    Args:
        db_alert_id: UUID string of alerts.id (not the source alert_id)
        tenant_id:   scoped for all queries — double-checked against DB row

    Returns:
        EnrichmentResult — also persisted to alert_enrichments table

    Raises:
        AlertNotFound: alert row missing (Celery should not retry)
        LLMError(retryable=True): transient API failure (Celery retries)
        LLMError(retryable=False): model/parse failure — handled internally,
            NEEDS_REVIEW persisted, human notified
    """
    async with get_session() as session:
        # 1. Load alert — validate tenant scope
        alert = await _load_alert(db_alert_id, tenant_id, session)

        logger.info(
            "Pipeline start | tenant={} db_id={} alert_id={} severity={} host={}",
            tenant_id,
            db_alert_id,
            alert.alert_id,
            alert.severity,
            alert.host,
        )

        # 2. Build context
        ctx = await build_context(alert, session)

    # 3. Call LLM (outside DB session — can take 5-15s, don't hold connection)
    try:
        result = await classify_alert(ctx)
    except LLMError as exc:
        if exc.retryable:
            logger.warning(
                "LLM retryable error | tenant={} alert_id={} error={}",
                tenant_id,
                alert.alert_id,
                exc,
            )
            raise   # Celery will retry

        # Non-retryable: persist NEEDS_REVIEW so alert doesn't disappear
        logger.error(
            "LLM non-retryable error — persisting NEEDS_REVIEW | "
            "tenant={} alert_id={} error={}",
            tenant_id,
            alert.alert_id,
            exc,
        )
        sentry_sdk.capture_exception(exc)
        result = _make_failure_result(exc)

    # 4. Persist enrichment (upsert — safe if pipeline is re-run)
    async with get_session() as session:
        await _upsert_enrichment(db_alert_id, tenant_id, result, session)

    logger.info(
        "Pipeline complete | tenant={} alert_id={} decision={} confidence={}",
        tenant_id,
        alert.alert_id,
        result.triage_output.decision,
        result.triage_output.confidence_score,
    )

    return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _load_alert(
    db_alert_id: str,
    tenant_id: str,
    session: AsyncSession,
) -> Alert:
    """Load and tenant-scope-validate the Alert row."""
    try:
        alert_uuid = uuid.UUID(db_alert_id)
    except ValueError:
        raise AlertNotFound(f"Invalid UUID: {db_alert_id!r}")

    result = await session.execute(
        select(Alert)
        .where(Alert.id == alert_uuid)
        .where(Alert.tenant_id == tenant_id)   # tenant scope guard
    )
    alert = result.scalar_one_or_none()
    if alert is None:
        raise AlertNotFound(
            f"Alert {db_alert_id!r} not found for tenant {tenant_id!r}"
        )
    return alert


async def _upsert_enrichment(
    db_alert_id: str,
    tenant_id: str,
    result: EnrichmentResult,
    session: AsyncSession,
) -> None:
    """
    INSERT ... ON CONFLICT (alert_db_id) DO UPDATE.
    Safe to call multiple times — re-enrichment overwrites previous result.
    """
    t = result.triage_output

    stmt = (
        pg_insert(AlertEnrichment)
        .values(
            id=uuid.uuid4(),
            alert_db_id=uuid.UUID(db_alert_id),
            tenant_id=tenant_id,
            triage_decision=t.decision,
            confidence_score=f"{t.confidence_score:.4f}",
            llm_reasoning=t.reasoning,
            suggested_action=t.suggested_action,
            runbook_refs=[r.model_dump() for r in t.runbook_refs],
            similar_past_alerts=None,    # stored in context, not re-persisted here
            prompt_version=result.prompt_version,
            raw_llm_response=result.raw_llm_response,
            model_used=result.model_used,
            prompt_tokens=str(result.prompt_tokens),
            completion_tokens=str(result.completion_tokens),
            latency_ms=str(result.latency_ms),
            enriched_at=datetime.now(timezone.utc),
        )
        .on_conflict_do_update(
            index_elements=["alert_db_id"],
            set_={
                "triage_decision": t.decision,
                "confidence_score": f"{t.confidence_score:.4f}",
                "llm_reasoning": t.reasoning,
                "suggested_action": t.suggested_action,
                "runbook_refs": [r.model_dump() for r in t.runbook_refs],
                "prompt_version": result.prompt_version,
                "raw_llm_response": result.raw_llm_response,
                "model_used": result.model_used,
                "prompt_tokens": str(result.prompt_tokens),
                "completion_tokens": str(result.completion_tokens),
                "latency_ms": str(result.latency_ms),
                "enriched_at": datetime.now(timezone.utc),
            },
        )
    )
    await session.execute(stmt)
    logger.debug(
        "AlertEnrichment upserted | tenant={} alert_db_id={}",
        tenant_id,
        db_alert_id,
    )


def _make_failure_result(exc: LLMError) -> EnrichmentResult:
    """
    Construct a safe NEEDS_REVIEW result when LLM pipeline fails unrecoverably.
    Ensures the alert surfaces for human review rather than silently dropping.
    """
    from llm.schemas import TriageOutput
    from llm.prompts import PROMPT_VERSION

    return EnrichmentResult(
        triage_output=TriageOutput(
            decision="NEEDS_REVIEW",
            confidence_score=0.0,
            reasoning=f"LLM pipeline failed: {str(exc)[:500]}. Manual review required.",
            suggested_action=None,
            runbook_refs=[],
            alert_category="unknown",
            is_flapping=False,
        ),
        prompt_version=PROMPT_VERSION,
        model_used="none",
        raw_llm_response={"error": str(exc), "raw": exc.raw},
        prompt_tokens=0,
        completion_tokens=0,
        latency_ms=0,
    )
