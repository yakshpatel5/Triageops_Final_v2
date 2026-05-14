"""
cron.py — Celery Beat periodic task logic.

Two coroutines called by Celery tasks in tasks.py:

  _run_expire_stale_approvals()
      Marks PENDING SlackApprovals past their expires_at as EXPIRED.
      For CRITICAL alerts that expired unanswered: posts a Slack warning.

  _run_escalate_unactioned_criticals()
      Finds EXPIRED approvals for CRITICAL alerts with no escalation yet.
      Auto-dispatches to PagerDuty / OpsGenie / webhook.
      Deduplicates: will not re-escalate if a TRIGGERED EscalationEvent exists.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import sentry_sdk
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.session import get_session
from models import Alert, AlertEnrichment, EscalationEvent, SlackApproval


# ---------------------------------------------------------------------------
# Expire stale approvals
# ---------------------------------------------------------------------------

async def _run_expire_stale_approvals() -> dict:
    now = datetime.now(timezone.utc)
    expired_ids: list[str] = []
    renotified_ids: list[str] = []

    async with get_session() as session:
        result = await session.execute(
            select(SlackApproval)
            .where(SlackApproval.status == "PENDING")
            .where(SlackApproval.expires_at <= now)
            .limit(200)
        )
        approvals = result.scalars().all()

        if not approvals:
            logger.debug("expire_stale_approvals: nothing to expire")
            return {"expired": 0, "renotified": 0}

        logger.info("expire_stale_approvals: {} expired approvals found", len(approvals))

        for approval in approvals:
            approval.status = "EXPIRED"
            expired_ids.append(str(approval.id))

        await session.flush()

    # Re-notify for CRITICAL expired approvals (outside DB session)
    for approval in approvals:
        try:
            decision = await _get_triage_decision(str(approval.alert_db_id))
            if decision == "CRITICAL":
                await _send_expiry_renotification(approval)
                renotified_ids.append(str(approval.id))
        except Exception as exc:
            sentry_sdk.capture_exception(exc)
            logger.error(
                "Failed to re-notify expired CRITICAL approval | id={} error={}",
                approval.id, exc,
            )

    logger.info(
        "expire_stale_approvals complete | expired={} renotified={}",
        len(expired_ids), len(renotified_ids),
    )
    return {"expired": len(expired_ids), "renotified": len(renotified_ids)}


async def _get_triage_decision(alert_db_id: str) -> str | None:
    try:
        alert_uuid = uuid.UUID(alert_db_id)
    except ValueError:
        return None
    async with get_session() as session:
        result = await session.execute(
            select(AlertEnrichment.triage_decision)
            .where(AlertEnrichment.alert_db_id == alert_uuid)
            .limit(1)
        )
        row = result.first()
        return row[0] if row else None


async def _send_expiry_renotification(approval: SlackApproval) -> None:
    from slack.client import post_message

    text = (
        f"⚠️ *CRITICAL alert expired without human action* "
        f"(approval ID: `{approval.id}`). "
        "Escalation will be triggered automatically."
    )
    payload: dict = {"channel": approval.slack_channel, "text": text}
    if approval.slack_ts:
        payload["thread_ts"] = approval.slack_ts

    try:
        await post_message(payload)
        logger.info("Expiry re-notification sent | approval_id={}", approval.id)
    except Exception as exc:
        logger.warning("Failed to send expiry re-notification | error={}", exc)


# ---------------------------------------------------------------------------
# Auto-escalate expired CRITICAL approvals
# ---------------------------------------------------------------------------

async def _run_escalate_unactioned_criticals() -> dict:
    """
    Find EXPIRED approvals on CRITICAL alerts that have no TRIGGERED escalation
    yet, and auto-dispatch to the configured escalation provider.

    FIX: subquery now correctly checks EscalationEvent (not Alert) to determine
    whether an escalation has already been sent.
    """
    from escalation.dispatcher import dispatch_escalation

    escalated: list[str] = []
    skipped:   list[str] = []

    # Find escalation candidates: EXPIRED approval + CRITICAL decision
    async with get_session() as session:
        result = await session.execute(
            select(SlackApproval, Alert, AlertEnrichment)
            .join(Alert,           Alert.id == SlackApproval.alert_db_id)
            .join(AlertEnrichment, AlertEnrichment.alert_db_id == Alert.id)
            .where(SlackApproval.status == "EXPIRED")
            .where(AlertEnrichment.triage_decision == "CRITICAL")
            .limit(50)
        )
        rows = result.all()

    if not rows:
        logger.debug("escalate_unactioned_criticals: no candidates")
        return {"escalated": 0, "skipped": 0}

    logger.info(
        "escalate_unactioned_criticals: {} CRITICAL expired approvals to process",
        len(rows),
    )

    for approval, alert, enrichment in rows:
        # FIX: dedup check queries EscalationEvent, not Alert
        if await _has_escalation_event(str(alert.id)):
            logger.debug(
                "Skipping already-escalated alert | alert_id={}", alert.id
            )
            skipped.append(str(approval.id))
            continue

        approval.actioned_by = "triageops-cron"

        try:
            event = await dispatch_escalation(
                approval=approval,
                alert=alert,
                enrichment=enrichment,
            )
            if event.status == "TRIGGERED":
                escalated.append(str(approval.id))
                logger.info(
                    "Auto-escalation triggered | alert_db_id={} provider={}",
                    alert.id, event.provider,
                )
            else:
                skipped.append(str(approval.id))
        except Exception as exc:
            sentry_sdk.capture_exception(exc)
            logger.error(
                "Auto-escalation failed | alert_db_id={} error={}",
                alert.id, exc,
            )
            skipped.append(str(approval.id))

    logger.info(
        "escalate_unactioned_criticals complete | escalated={} skipped={}",
        len(escalated), len(skipped),
    )
    return {"escalated": len(escalated), "skipped": len(skipped)}


async def _has_escalation_event(alert_db_id: str) -> bool:
    """Return True if a TRIGGERED EscalationEvent already exists for this alert."""
    try:
        alert_uuid = uuid.UUID(alert_db_id)
    except ValueError:
        return False
    async with get_session() as session:
        result = await session.execute(
            select(EscalationEvent.id)
            .where(EscalationEvent.alert_db_id == alert_uuid)
            .where(EscalationEvent.status == "TRIGGERED")
            .limit(1)
        )
        return result.first() is not None
