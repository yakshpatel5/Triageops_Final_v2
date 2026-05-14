"""
slack/notifier.py — Send alert notification to Slack and/or Microsoft Teams.

Called from tasks.py after enrichment pipeline completes.

Routing logic (both can fire simultaneously):
  - SLACK_BOT_TOKEN set   → send Slack Block Kit message + persist SlackApproval
  - TEAMS_WEBHOOK_URL set → send Teams Adaptive Card (notification-only, no approval row)
  - Both set              → send both
  - Neither set           → log warning, return None (non-fatal)

V1: channel/URL resolved from env vars.
V1.2: per-tenant routing via DB config table.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import sentry_sdk
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.session import get_session
from models import Alert, AlertEnrichment, SlackApproval
from slack.blocks import build_alert_notification
from slack.client import SlackError, post_message
from teams.cards import build_teams_card
from teams.client import TeamsError, post_card

# How long a PENDING approval stays valid before being marked EXPIRED by cron
APPROVAL_TTL_HOURS = int(os.getenv("APPROVAL_TTL_HOURS", "24"))


async def send_alert_notification(
    db_alert_id: str,
    tenant_id: str,
) -> str | None:
    """
    Load enriched alert, route to Slack and/or Teams, persist SlackApproval if Slack.

    Returns slack_ts if Slack succeeded, None otherwise.
    Non-fatal — enrichment is complete regardless of notification outcome.

    Teams is fire-and-forget: its success/failure does not affect the return value
    or the SlackApproval row. Teams v1 has no interactive callbacks.
    """
    slack_token = os.getenv("SLACK_BOT_TOKEN", "").strip()
    teams_url   = os.getenv("TEAMS_WEBHOOK_URL", "").strip()

    if not slack_token and not teams_url:
        logger.warning(
            "No notification channel configured (SLACK_BOT_TOKEN and "
            "TEAMS_WEBHOOK_URL both unset) | db_id={} tenant={}",
            db_alert_id, tenant_id,
        )
        return None

    async with get_session() as session:
        alert, enrichment = await _load_alert_and_enrichment(
            db_alert_id, tenant_id, session
        )
        if alert is None or enrichment is None:
            logger.error(
                "send_alert_notification: alert or enrichment missing | "
                "db_id={} tenant={}",
                db_alert_id, tenant_id,
            )
            return None

        # ── Teams notification (non-fatal, no approval row) ───────────────
        if teams_url:
            try:
                card = build_teams_card(alert=alert, enrichment=enrichment)
                await post_card(card)
                logger.info(
                    "Teams card sent | tenant={} db_id={} decision={}",
                    tenant_id, db_alert_id, enrichment.triage_decision,
                )
            except TeamsError as exc:
                sentry_sdk.capture_exception(exc)
                logger.error(
                    "Teams delivery failed (non-fatal) | tenant={} db_id={} error={}",
                    tenant_id, db_alert_id, exc,
                )

        # ── Slack notification (persists SlackApproval for HITL buttons) ─
        if not slack_token:
            return None

        channel = _resolve_channel(tenant_id, enrichment.triage_decision)

        approval = SlackApproval(
            id=uuid.uuid4(),
            alert_db_id=uuid.UUID(db_alert_id),
            enrichment_id=enrichment.id,
            tenant_id=tenant_id,
            slack_channel=channel,
            slack_ts=None,
            status="PENDING",
            sent_at=datetime.now(timezone.utc),
            expires_at=datetime.now(timezone.utc) + timedelta(hours=APPROVAL_TTL_HOURS),
        )

        payload = build_alert_notification(
            alert=alert,
            enrichment=enrichment,
            approval_id=str(approval.id),
            channel=channel,
        )
        approval.slack_message_blocks = payload.get("blocks")

        try:
            ts = await post_message(payload)
        except SlackError as exc:
            approval.status = "ERROR"
            session.add(approval)
            logger.error(
                "Slack delivery failed | tenant={} db_id={} error={}",
                tenant_id, db_alert_id, exc,
            )
            return None

        approval.slack_ts = ts
        session.add(approval)

    logger.info(
        "Slack notification sent | tenant={} db_id={} channel={} ts={} decision={}",
        tenant_id, db_alert_id, channel, ts, enrichment.triage_decision,
    )
    return ts


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _load_alert_and_enrichment(
    db_alert_id: str,
    tenant_id: str,
    session: AsyncSession,
) -> tuple[Alert | None, AlertEnrichment | None]:
    alert_uuid = uuid.UUID(db_alert_id)

    alert_result = await session.execute(
        select(Alert)
        .where(Alert.id == alert_uuid)
        .where(Alert.tenant_id == tenant_id)
    )
    alert = alert_result.scalar_one_or_none()
    if alert is None:
        return None, None

    enrichment_result = await session.execute(
        select(AlertEnrichment)
        .where(AlertEnrichment.alert_db_id == alert_uuid)
    )
    enrichment = enrichment_result.scalar_one_or_none()
    return alert, enrichment


def _resolve_channel(tenant_id: str, decision: str) -> str:
    """
    Resolve the Slack channel for this alert.

    Priority:
      1. Decision-specific env var:  SLACK_CHANNEL_CRITICAL / SLACK_CHANNEL_NOISE
      2. Tenant-specific env var:    SLACK_CHANNEL_{TENANT_ID_UPPER}
      3. Global default:             SLACK_ALERT_CHANNEL

    V1.2: replace with DB lookup against tenant config table.
    """
    # Decision-specific routing (e.g. CRITICAL goes to #noc-critical)
    decision_channel = os.getenv(f"SLACK_CHANNEL_{decision.upper()}")
    if decision_channel:
        return decision_channel

    # Tenant-specific channel (sanitise tenant_id → valid env var name)
    safe_tenant = tenant_id.upper().replace("-", "_").replace(".", "_")
    tenant_channel = os.getenv(f"SLACK_CHANNEL_{safe_tenant}")
    if tenant_channel:
        return tenant_channel

    # Global fallback
    return os.getenv("SLACK_ALERT_CHANNEL", "#noc-triage")
