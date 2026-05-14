"""
escalation/dispatcher.py — Routes an ESCALATE approval action to the
configured provider (PagerDuty, OpsGenie, or generic webhook) and persists
an EscalationEvent audit row.

Called from:
  1. routers/slack_interactions.py — when engineer clicks ESCALATE
  2. tasks.py cron — when a CRITICAL approval expires without action

Provider resolution order (per tenant):
  1. PAGERDUTY  — if PAGERDUTY_ROUTING_KEY is set
  2. OPSGENIE   — if OPSGENIE_API_KEY is set
  3. WEBHOOK    — if ESCALATION_WEBHOOK_URL is set
  4. SKIPPED    — no provider configured (log warning, no exception)

V1.2: replace env-var resolution with per-tenant DB config table.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from typing import Any

import sentry_sdk
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from db.session import get_session
from escalation.opsgenie import OpsGenieError, trigger_alert as og_trigger
from escalation.pagerduty import PagerDutyError, trigger_incident as pd_trigger
from escalation.webhook import WebhookError, trigger_webhook
from models import Alert, AlertEnrichment, EscalationEvent, SlackApproval


async def dispatch_escalation(
    approval: SlackApproval,
    alert: Alert,
    enrichment: AlertEnrichment,
) -> EscalationEvent:
    """
    Determine provider, call it, persist EscalationEvent.

    Always returns an EscalationEvent (TRIGGERED, FAILED, or SKIPPED).
    Never raises — all exceptions are caught, logged, and recorded on the row.
    """
    tenant_id    = approval.tenant_id
    alert_db_id  = str(alert.id)
    triggered_by = approval.actioned_by or "unknown"

    provider, provider_cfg = _resolve_provider(tenant_id)

    logger.info(
        "Escalation dispatch | tenant={} alert_db_id={} provider={} triggered_by={}",
        tenant_id, alert_db_id, provider, triggered_by,
    )

    event = EscalationEvent(
        id=uuid.uuid4(),
        approval_id=approval.id,
        alert_db_id=alert.id,
        tenant_id=tenant_id,
        provider=provider,
        status="TRIGGERED",    # optimistic — updated to FAILED on error
        triggered_by=triggered_by,
        triggered_at=datetime.now(timezone.utc),
    )

    if provider == "SKIPPED":
        event.status       = "SKIPPED"
        event.error_detail = (
            "No escalation provider configured. "
            "Set PAGERDUTY_ROUTING_KEY or OPSGENIE_API_KEY."
        )
        logger.warning(
            "No escalation provider configured | tenant={} alert_db_id={}",
            tenant_id, alert_db_id,
        )
        async with get_session() as session:
            session.add(event)
        return event

    common_kwargs = dict(
        alert_db_id      = alert_db_id,
        alert_id         = alert.alert_id,
        host             = alert.host,
        message          = alert.message,
        severity         = alert.severity.value if hasattr(alert.severity, "value") else str(alert.severity),
        triage_decision  = enrichment.triage_decision,
        llm_reasoning    = enrichment.llm_reasoning,
        suggested_action = enrichment.suggested_action,
        triggered_by     = triggered_by,
    )

    try:
        if provider == "PAGERDUTY":
            result = await pd_trigger(
                routing_key=provider_cfg["routing_key"],
                **common_kwargs,
            )
            event.provider_incident_id  = f"triageops-{alert_db_id}"   # dedup_key
            event.provider_incident_url = _pd_incident_url(result)

        elif provider == "OPSGENIE":
            result = await og_trigger(
                api_key=provider_cfg["api_key"],
                responders=provider_cfg.get("responders"),
                **common_kwargs,
            )
            event.provider_incident_id  = f"triageops-{alert_db_id}"   # alias
            event.provider_incident_url = None   # OpsGenie doesn't return URL in v2 response

        elif provider == "WEBHOOK":
            result = await trigger_webhook(
                url=provider_cfg["url"],
                secret=provider_cfg.get("secret"),
                **common_kwargs,
            )
            event.provider_incident_id = None

        event.request_payload  = result.get("request")
        event.response_payload = result.get("response")
        event.status = "TRIGGERED"

        logger.info(
            "Escalation triggered | tenant={} provider={} alert_db_id={}",
            tenant_id, provider, alert_db_id,
        )

    except (PagerDutyError, OpsGenieError, WebhookError) as exc:
        sentry_sdk.capture_exception(exc)
        event.status       = "FAILED"
        event.error_detail = str(exc)
        logger.error(
            "Escalation failed | tenant={} provider={} alert_db_id={} error={}",
            tenant_id, provider, alert_db_id, exc,
        )

    except Exception as exc:
        sentry_sdk.capture_exception(exc)
        event.status       = "FAILED"
        event.error_detail = f"Unexpected error: {exc}"
        logger.exception(
            "Unexpected escalation error | tenant={} provider={} alert_db_id={}",
            tenant_id, provider, alert_db_id,
        )

    async with get_session() as session:
        session.add(event)

    return event


# ---------------------------------------------------------------------------
# Provider resolution
# ---------------------------------------------------------------------------

def _resolve_provider(tenant_id: str) -> tuple[str, dict[str, Any]]:
    """
    Resolve which escalation provider to use for this tenant.
    Returns (provider_name, config_dict).

    V1: env-var based. Priority: PagerDuty > OpsGenie > Webhook > Skip.
    V1.2: replace with DB lookup against tenant_escalation_config table.
    """
    # Tenant-scoped env var first (e.g. PAGERDUTY_ROUTING_KEY_ACME_CORP)
    safe = tenant_id.upper().replace("-", "_").replace(".", "_")

    pd_key = (
        os.getenv(f"PAGERDUTY_ROUTING_KEY_{safe}")
        or os.getenv("PAGERDUTY_ROUTING_KEY")
    )
    if pd_key:
        return "PAGERDUTY", {"routing_key": pd_key}

    og_key = (
        os.getenv(f"OPSGENIE_API_KEY_{safe}")
        or os.getenv("OPSGENIE_API_KEY")
    )
    if og_key:
        responders_raw = os.getenv("OPSGENIE_RESPONDERS")  # JSON array or None
        responders = None
        if responders_raw:
            import json
            try:
                responders = json.loads(responders_raw)
            except Exception:
                logger.warning("OPSGENIE_RESPONDERS is not valid JSON — ignoring")
        return "OPSGENIE", {"api_key": og_key, "responders": responders}

    webhook_url = (
        os.getenv(f"ESCALATION_WEBHOOK_URL_{safe}")
        or os.getenv("ESCALATION_WEBHOOK_URL")
    )
    if webhook_url:
        return "WEBHOOK", {
            "url": webhook_url,
            "secret": os.getenv("ESCALATION_WEBHOOK_SECRET"),
        }

    return "SKIPPED", {}


def _pd_incident_url(result: dict) -> str | None:
    """Extract PagerDuty incident URL from trigger response, if present."""
    # PD Events API v2 trigger response does not include a URL directly.
    # The dedup_key can be used to look up the incident via REST API in V1.2.
    return None
