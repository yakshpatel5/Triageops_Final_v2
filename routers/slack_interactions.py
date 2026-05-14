"""
routers/slack_interactions.py — handles Slack interactivity callbacks.

Slack POSTs a URL-encoded 'payload' field to this endpoint whenever a user
clicks a button in a Block Kit message.

Security:
  - HMAC signature verified FIRST, before any parsing (verify_slack_signature)
  - Payload is URL-decoded and JSON-parsed only after verification
  - approval_id from button value is validated against DB before any write
  - tenant_id is looked up from SlackApproval row — NOT from payload (never trust Slack payload for tenant resolution)
  - V1: read-only — action is recorded but no downstream system is mutated

Flow:
  1. Verify Slack HMAC signature
  2. Parse URL-encoded payload → JSON
  3. Extract action_id + approval_id from button
  4. Load SlackApproval from DB, validate status == PENDING
  5. Update SlackApproval → ACTIONED
  6. Call chat.update to disable buttons on the original message
  7. Return 200 immediately (Slack expects <3s response)

Slack interactivity docs:
  https://api.slack.com/interactivity/handling
"""

from __future__ import annotations

import json
import urllib.parse
import uuid
from datetime import datetime, timezone

import sentry_sdk
from fastapi import APIRouter, Request, Response, status
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.session import get_db
from fastapi import Depends
from models import SlackApproval
from slack.blocks import build_approval_update
from slack.client import SlackSignatureError, update_message, verify_slack_signature

router = APIRouter(prefix="/slack", tags=["slack"])

# Map Slack action_id → our ApprovalAction enum value
_ACTION_MAP = {
    "approval_acknowledge": "ACKNOWLEDGE",
    "approval_suppress":    "SUPPRESS",
    "approval_escalate":    "ESCALATE",
    "approval_dismiss":     "DISMISS",
}


@router.post(
    "/interactions",
    summary="Slack interactivity callback endpoint",
    status_code=status.HTTP_200_OK,
)
async def slack_interactions(
    request: Request,
    session: AsyncSession = Depends(get_db),
) -> Response:
    """
    Receives Slack interactive component payloads.

    Configure in Slack App settings → Interactivity & Shortcuts →
    Request URL: https://<your-domain>/slack/interactions

    Returns 200 with empty body on success (Slack requirement).
    Returns 200 with error text on validation failure (prevents Slack retry loop).
    """
    # ── 1. Read raw body for signature verification ──────────────────────
    body_bytes = await request.body()

    timestamp  = request.headers.get("X-Slack-Request-Timestamp", "")
    signature  = request.headers.get("X-Slack-Signature", "")

    try:
        verify_slack_signature(body_bytes, timestamp, signature)
    except SlackSignatureError as exc:
        logger.warning("Slack signature verification failed | error={}", exc)
        # Return 403 — this payload did not come from Slack
        return Response(status_code=status.HTTP_403_FORBIDDEN, content=str(exc))
    except EnvironmentError as exc:
        sentry_sdk.capture_exception(exc)
        logger.critical("SLACK_SIGNING_SECRET not configured: {}", exc)
        return Response(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)

    # ── 2. Parse URL-encoded payload field ───────────────────────────────
    try:
        form_data   = urllib.parse.parse_qs(body_bytes.decode("utf-8"))
        payload_str = form_data.get("payload", [""])[0]
        payload     = json.loads(payload_str)
    except (ValueError, KeyError, json.JSONDecodeError) as exc:
        logger.warning("Failed to parse Slack payload | error={}", exc)
        return Response(status_code=status.HTTP_200_OK, content="invalid payload")

    payload_type = payload.get("type")

    # ── 3. Route by payload type ─────────────────────────────────────────
    if payload_type == "block_actions":
        return await _handle_block_action(payload, session)

    # Acknowledge other payload types (shortcuts, view submissions) without error
    logger.debug("Unhandled Slack payload type: {}", payload_type)
    return Response(status_code=status.HTTP_200_OK)


async def _handle_block_action(
    payload: dict,
    session: AsyncSession,
) -> Response:
    """Handle a button click from a Block Kit message."""
    actions = payload.get("actions", [])
    if not actions:
        return Response(status_code=status.HTTP_200_OK)

    action     = actions[0]           # process first action only
    action_id  = action.get("action_id", "")
    approval_id_str = action.get("value", "")

    # Slack user info
    user       = payload.get("user", {})
    slack_user_id   = user.get("id", "unknown")
    slack_user_name = user.get("name") or user.get("username") or slack_user_id

    logger.info(
        "Slack action received | action_id={} approval_id={} user={}",
        action_id,
        approval_id_str,
        slack_user_id,
    )

    # ── 4. Validate action_id ────────────────────────────────────────────
    mapped_action = _ACTION_MAP.get(action_id)
    if not mapped_action:
        logger.warning("Unknown action_id: {}", action_id)
        return Response(status_code=status.HTTP_200_OK, content="unknown action")

    # ── 5. Load SlackApproval — validate approval_id + status ────────────
    try:
        approval_uuid = uuid.UUID(approval_id_str)
    except ValueError:
        logger.warning("Invalid approval_id in button value: {}", approval_id_str)
        return Response(status_code=status.HTTP_200_OK, content="invalid approval_id")

    result = await session.execute(
        select(SlackApproval).where(SlackApproval.id == approval_uuid)
    )
    approval = result.scalar_one_or_none()

    if approval is None:
        logger.warning("SlackApproval not found | id={}", approval_id_str)
        return Response(status_code=status.HTTP_200_OK, content="approval not found")

    if approval.status != "PENDING":
        # Already actioned — idempotent: return 200 without re-writing
        logger.info(
            "Approval already actioned | id={} status={} action={}",
            approval_id_str,
            approval.status,
            approval.action,
        )
        return Response(status_code=status.HTTP_200_OK)

    if approval.expires_at and approval.expires_at < datetime.now(timezone.utc):
        logger.warning("Approval expired | id={}", approval_id_str)
        approval.status = "EXPIRED"
        await session.flush()
        return Response(status_code=status.HTTP_200_OK, content="approval expired")

    # ── 6. Record approval action ─────────────────────────────────────────
    now = datetime.now(timezone.utc)
    approval.status          = "ACTIONED"
    approval.action          = mapped_action
    approval.actioned_by     = slack_user_id
    approval.actioned_by_name = slack_user_name
    approval.actioned_at     = now
    # Note: action_note from modal not implemented in V1 (no modal shown)
    await session.flush()

    logger.info(
        "Approval recorded | id={} action={} tenant={} alert_db_id={} user={}",
        approval_id_str,
        mapped_action,
        approval.tenant_id,
        approval.alert_db_id,
        slack_user_id,
    )

    # ── 7. Dispatch escalation task if action is ESCALATE ───────────────
    # Fire-and-forget Celery task — must not block the <3s Slack response.
    # Cron fallback (escalate_unactioned_criticals) catches any queue failure.
    if mapped_action == "ESCALATE":
        try:
            from tasks import enqueue_escalation
            enqueue_escalation(approval_id_str)
            logger.info(
                "Escalation task enqueued | approval_id={} tenant={}",
                approval_id_str, approval.tenant_id,
            )
        except Exception as exc:
            sentry_sdk.capture_exception(exc)
            logger.error(
                "Failed to enqueue escalation (cron fallback active) | error={}", exc
            )

    # ── 8. Auto-create suppression rule when action is SUPPRESS ────────────
    # Fire-and-forget — approval is already recorded. Rule creation failure
    # does not affect the approval or the Slack message update.
    if mapped_action == "SUPPRESS":
        try:
            # Load the alert to build the rule from its host + message
            from db.session import AsyncSessionLocal
            from models import Alert
            from sqlalchemy import select as _select
            async def _create_suppression():
                async with AsyncSessionLocal() as _sess:
                    _res = await _sess.execute(
                        _select(Alert).where(Alert.id == approval.alert_db_id)
                    )
                    _alert = _res.scalar_one_or_none()
                    if _alert:
                        await build_rule_from_suppression(
                            approval_id=approval_id_str,
                            alert=_alert,
                            actioned_by=slack_user_name,
                        )
            import asyncio
            asyncio.create_task(_create_suppression())
            logger.info(
                "Suppression rule creation queued | approval_id={} tenant={}",
                approval_id_str, approval.tenant_id,
            )
        except Exception as exc:
            sentry_sdk.capture_exception(exc)
            logger.error(
                "Failed to queue suppression rule creation | error={}", exc
            )

    # ── 9. Update the Slack message to disable buttons ────────────────────
    # Do this after DB write — message update failure is non-fatal
    original_blocks = approval.slack_message_blocks or []
    updated_blocks  = build_approval_update(
        original_blocks=original_blocks,
        action=mapped_action,
        actioned_by_name=slack_user_name,
        actioned_at=now,
    )

    # Fire-and-forget update — don't await inside the DB session
    # (update_message has its own retry logic)
    if approval.slack_ts and approval.slack_channel:
        import asyncio
        asyncio.create_task(
            update_message(
                channel=approval.slack_channel,
                ts=approval.slack_ts,
                blocks=updated_blocks,
            )
        )

    # ── 8. Respond immediately ────────────────────────────────────────────
    # Slack requires <3s response. Heavy work (e.g. escalation paging) goes
    # in a Celery task triggered here in V1.2.
    return Response(
        status_code=status.HTTP_200_OK,
        content="",   # empty body = Slack clears the loading state silently
    )
