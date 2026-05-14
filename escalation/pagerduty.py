"""
escalation/pagerduty.py — PagerDuty Events API v2 client.

Uses the Events API v2 (not the REST API) — this is the correct endpoint
for triggering incidents from monitoring integrations.

API reference: https://developer.pagerduty.com/docs/ZG9jOjExMDI5NTgx-send-an-alert-event

IMPORTANT: Each PagerDuty integration key (routing_key) maps to a Service.
The routing_key is set per-tenant via env var PAGERDUTY_ROUTING_KEY
(or per-tenant DB config in V1.2).

Deduplication:
  PagerDuty deduplicates on dedup_key. We use alert_db_id as the dedup_key
  so that re-escalation of the same alert updates the existing incident rather
  than creating a new one.

HALLUCINATION RISK:
  The payload field names and severity values here are from PagerDuty docs.
  Validate with a real PagerDuty account before production use.
  The Events API v2 endpoint is: https://events.pagerduty.com/v2/enqueue
  Do NOT use the older v1 endpoint (events.pagerduty.com/generic/2010-04-15/create_event.json).
"""

from __future__ import annotations

import os
from typing import Any

import httpx
import sentry_sdk
from loguru import logger
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

PD_EVENTS_URL = "https://events.pagerduty.com/v2/enqueue"

# PagerDuty severity mapping from our triage decision
_PD_SEVERITY_MAP = {
    "CRITICAL":     "critical",
    "HIGH":         "error",
    "MEDIUM":       "warning",
    "LOW":          "info",
    "NOISE":        "info",
    "NEEDS_REVIEW": "warning",
    "UNKNOWN":      "warning",
}


class PagerDutyError(Exception):
    def __init__(self, msg: str, retryable: bool = False):
        super().__init__(msg)
        self.retryable = retryable


@retry(
    retry=retry_if_exception_type(PagerDutyError),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=30),
    reraise=True,
)
async def trigger_incident(
    routing_key: str,
    alert_db_id: str,
    alert_id: str,
    host: str,
    message: str,
    severity: str,
    triage_decision: str,
    llm_reasoning: str | None,
    suggested_action: str | None,
    triggered_by: str,
    source_name: str = "TriageOps",
) -> dict[str, Any]:
    """
    Trigger a PagerDuty incident via Events API v2.

    Returns the PagerDuty response body (contains dedup_key for future updates).
    Raises PagerDutyError on failure.
    """
    pd_severity = _PD_SEVERITY_MAP.get(severity, "warning")

    payload: dict[str, Any] = {
        "routing_key": routing_key,
        "event_action": "trigger",
        "dedup_key": f"triageops-{alert_db_id}",   # idempotent — re-trigger updates existing
        "payload": {
            "summary": f"[TriageOps {triage_decision}] {host}: {message[:512]}",
            "severity": pd_severity,
            "source": host,
            "component": source_name,
            "group": triage_decision,
            "custom_details": {
                "alert_id": alert_id,
                "triage_decision": triage_decision,
                "llm_reasoning": (llm_reasoning or "")[:800],
                # HALLUCINATION RISK: always label AI suggestions as unverified
                "suggested_action": (
                    f"[AI suggestion — verify before executing] {suggested_action}"
                    if suggested_action else "None"
                ),
                "escalated_by": triggered_by,
                "triageops_alert_db_id": alert_db_id,
            },
        },
        "client": "TriageOps",
    }

    logger.info(
        "PagerDuty trigger | alert_db_id={} host={} severity={}",
        alert_db_id, host, pd_severity,
    )

    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            resp = await client.post(
                PD_EVENTS_URL,
                json=payload,
                headers={"Content-Type": "application/json"},
            )
        except httpx.TimeoutException as exc:
            raise PagerDutyError(f"PagerDuty timeout: {exc}", retryable=True)
        except httpx.RequestError as exc:
            raise PagerDutyError(f"PagerDuty network error: {exc}", retryable=True)

    if resp.status_code == 429:
        raise PagerDutyError("PagerDuty rate limited", retryable=True)
    if resp.status_code >= 500:
        raise PagerDutyError(f"PagerDuty server error {resp.status_code}", retryable=True)
    if resp.status_code not in (200, 202):
        body = resp.text[:500]
        raise PagerDutyError(
            f"PagerDuty rejected event: HTTP {resp.status_code} — {body}",
            retryable=False,
        )

    response_body = resp.json()
    logger.info(
        "PagerDuty incident triggered | dedup_key=triageops-{} status={}",
        alert_db_id,
        response_body.get("status"),
    )
    return {"request": payload, "response": response_body}
