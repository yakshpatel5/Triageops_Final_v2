"""
Webhook ingestion router — /webhook/prtg and /webhook/datadog.

Critical path requirements:
  - Respond < 500ms (Datadog will retry on timeout)
  - Idempotent: duplicate (alert_id, tenant_id) → 200 {"status":"duplicate"}
  - Never lose raw payload — write to DB before queuing
  - Enqueue Celery task for async LLM enrichment (V1: stub task)
"""

from typing import Any

import sentry_sdk
from fastapi import APIRouter, Depends, HTTPException, Request, status
from loguru import logger
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from db.session import get_db
from models import Alert
from schemas import (
    AlertIngestionEvent,
    DatadogPayload,
    IngestResponse,
    PRTGPayload,
)
from tasks import enqueue_enrichment
from suppression.engine import check_suppression

router = APIRouter(prefix="/webhook", tags=["webhook"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _check_duplicate(
    session: AsyncSession,
    alert_id: str,
    tenant_id: str,
) -> bool:
    """
    Application-level dedup check before attempting insert.
    The DB unique constraint is a second safety net (catches race conditions).
    """
    result = await session.execute(
        select(Alert.id)
        .where(Alert.alert_id == alert_id)
        .where(Alert.tenant_id == tenant_id)
        .limit(1)
    )
    return result.first() is not None


async def _persist_alert(
    session: AsyncSession,
    event: AlertIngestionEvent,
    suppressed: bool = False,
) -> Alert:
    """Insert normalised alert; caller handles IntegrityError for race-condition dedup."""
    alert = Alert(
        alert_id=event.alert_id,
        tenant_id=event.tenant_id,
        source=event.source,
        severity=event.severity,
        host=event.host,
        message=event.message,
        raw_payload=event.raw_payload,
        received_at=event.received_at,
        is_suppressed="1" if suppressed else "0",
    )
    session.add(alert)
    await session.flush()   # get DB-assigned id before commit
    return alert


async def _ingest(
    raw_body: dict[str, Any],
    event: AlertIngestionEvent,
    session: AsyncSession,
) -> IngestResponse:
    """
    Shared ingest logic used by both endpoints.
    Returns IngestResponse; raises HTTPException on validation failure.
    """
    logger.info(
        "Ingest | tenant={} source={} alert_id={} severity={} host={}",
        event.tenant_id,
        event.source,
        event.alert_id,
        event.severity,
        event.host,
    )

    # ── Suppression check (before dedup and before DB write) ─────────────
    # Matched alerts are still persisted for audit but skip enrichment + Slack
    matched_rule = await check_suppression(event, session)
    if matched_rule:
        # Persist the alert with a suppressed marker, then return immediately
        try:
            alert = await _persist_alert(session, event, suppressed=True)
            logger.info(
                "Alert persisted (suppressed) | db_id={} rule_id={}",
                alert.id, getattr(matched_rule, 'id', '?'),
            )
        except IntegrityError:
            await session.rollback()
        return IngestResponse(
            status="suppressed",
            alert_id=event.alert_id,
            detail=f"Matched suppression rule {getattr(matched_rule, 'id', '?')}",
        )

    # Application-level dedup (fast path — avoids DB write on duplicate)
    if await _check_duplicate(session, event.alert_id, event.tenant_id):
        logger.info(
            "Duplicate alert | tenant={} alert_id={}",
            event.tenant_id,
            event.alert_id,
        )
        return IngestResponse(status="duplicate", alert_id=event.alert_id)

    try:
        alert = await _persist_alert(session, event)
    except IntegrityError:
        # Race condition: another request inserted between our SELECT and INSERT
        await session.rollback()
        logger.info(
            "Race-condition duplicate | tenant={} alert_id={}",
            event.tenant_id,
            event.alert_id,
        )
        return IngestResponse(
            status="duplicate",
            alert_id=event.alert_id,
            detail="Concurrent duplicate write detected",
        )

    # Enqueue LLM enrichment task (non-blocking — responds before enrichment runs)
    # V1: stub task logs intent; LLM pipeline wired in Week 2
    try:
        enqueue_enrichment(str(alert.id), event.tenant_id)
    except Exception as exc:
        # Task queue failure must NOT block ingest — alert is already persisted
        sentry_sdk.capture_exception(exc)
        logger.error(
            "Failed to enqueue enrichment task | alert_id={} error={}",
            event.alert_id,
            exc,
        )

    logger.info(
        "Alert persisted | db_id={} tenant={} alert_id={}",
        alert.id,
        event.tenant_id,
        event.alert_id,
    )
    return IngestResponse(status="accepted", alert_id=event.alert_id)


# ---------------------------------------------------------------------------
# PRTG endpoint
# ---------------------------------------------------------------------------

@router.post(
    "/prtg",
    response_model=IngestResponse,
    summary="Ingest PRTG sensor alert",
    status_code=status.HTTP_200_OK,
)
async def ingest_prtg(
    request: Request,
    body: dict[str, Any],   # accept raw dict; validation in PRTGPayload
    session: AsyncSession = Depends(get_db),
) -> IngestResponse:
    """
    Receives PRTG JSON webhook notifications.
    Configure PRTG notification template to POST JSON to this endpoint.
    """
    tenant_id: str = request.state.tenant_id   # set by APIKeyMiddleware

    try:
        payload = PRTGPayload.model_validate(body)
        event = payload.normalise(tenant_id=tenant_id, raw=body)
    except ValueError as exc:
        # Normalisation failure — likely missing required PRTG fields
        logger.warning(
            "PRTG payload normalisation failed | tenant={} error={} body={}",
            tenant_id,
            exc,
            body,
        )
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"PRTG payload normalisation failed: {exc}",
        )
    except Exception as exc:
        sentry_sdk.capture_exception(exc)
        logger.error("Unexpected PRTG parse error | tenant={} error={}", tenant_id, exc)
        raise HTTPException(status_code=500, detail="Internal server error")

    return await _ingest(body, event, session)


# ---------------------------------------------------------------------------
# Datadog endpoint
# ---------------------------------------------------------------------------

@router.post(
    "/datadog",
    response_model=IngestResponse,
    summary="Ingest Datadog monitor alert",
    status_code=status.HTTP_200_OK,
)
async def ingest_datadog(
    request: Request,
    body: dict[str, Any],
    session: AsyncSession = Depends(get_db),
) -> IngestResponse:
    """
    Receives Datadog monitor webhook notifications.
    Configure Datadog Webhook integration to POST to this endpoint.
    Datadog expects a 200 response; retries on 5xx.
    """
    tenant_id: str = request.state.tenant_id

    try:
        payload = DatadogPayload.model_validate(body)
        event = payload.normalise(tenant_id=tenant_id, raw=body)
    except ValueError as exc:
        logger.warning(
            "Datadog payload normalisation failed | tenant={} error={} body={}",
            tenant_id,
            exc,
            body,
        )
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Datadog payload normalisation failed: {exc}",
        )
    except Exception as exc:
        sentry_sdk.capture_exception(exc)
        logger.error(
            "Unexpected Datadog parse error | tenant={} error={}", tenant_id, exc
        )
        raise HTTPException(status_code=500, detail="Internal server error")

    return await _ingest(body, event, session)
