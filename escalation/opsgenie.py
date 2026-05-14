"""
escalation/opsgenie.py — OpsGenie Alerts API client.

API reference: https://docs.opsgenie.com/docs/alert-api

OpsGenie requires a Geo-specific API endpoint:
  - US: https://api.opsgenie.com
  - EU: https://api.eu.opsgenie.com
Set OPSGENIE_API_URL in env to override.

Deduplication:
  OpsGenie deduplicates on 'alias'. We use alert_db_id as alias so
  re-escalating an existing alert updates (adds a note to) the open incident.

HALLUCINATION RISK:
  OpsGenie API field names, priority values, and response structure are from
  their public docs. Validate with a real OpsGenie account and API key.
"""

from __future__ import annotations

import os
from typing import Any

import httpx
import sentry_sdk
from loguru import logger
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

OPSGENIE_API_URL = os.getenv("OPSGENIE_API_URL", "https://api.opsgenie.com")
OPSGENIE_ALERTS_ENDPOINT = f"{OPSGENIE_API_URL}/v2/alerts"

_OG_PRIORITY_MAP = {
    "CRITICAL":     "P1",
    "HIGH":         "P2",
    "MEDIUM":       "P3",
    "LOW":          "P4",
    "NOISE":        "P5",
    "NEEDS_REVIEW": "P3",
    "UNKNOWN":      "P3",
}


class OpsGenieError(Exception):
    def __init__(self, msg: str, retryable: bool = False):
        super().__init__(msg)
        self.retryable = retryable


@retry(
    retry=retry_if_exception_type(OpsGenieError),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=30),
    reraise=True,
)
async def trigger_alert(
    api_key: str,
    alert_db_id: str,
    alert_id: str,
    host: str,
    message: str,
    severity: str,
    triage_decision: str,
    llm_reasoning: str | None,
    suggested_action: str | None,
    triggered_by: str,
    responders: list[dict] | None = None,   # e.g. [{"name": "noc-team", "type": "team"}]
) -> dict[str, Any]:
    """
    Create an OpsGenie alert.

    Returns dict with {request, response}. Raises OpsGenieError on failure.

    responders: OpsGenie team/user routing. If None, alert goes to default responder.
    V1.2: load responders from tenant config table.
    """
    priority = _OG_PRIORITY_MAP.get(severity, "P3")

    payload: dict[str, Any] = {
        "message": f"[TriageOps {triage_decision}] {host}: {message[:130]}",   # OG 130 char limit
        "alias": f"triageops-{alert_db_id}",     # dedup key
        "description": "\n".join(filter(None, [
            f"**Alert ID:** {alert_id}",
            f"**Host:** {host}",
            f"**Triage Decision:** {triage_decision}",
            f"**Reasoning:** {(llm_reasoning or '')[:800]}",
            # HALLUCINATION RISK label required
            f"**AI Suggested Action (verify before executing):** {suggested_action or 'None'}",
            f"**Escalated by:** {triggered_by} via TriageOps",
        ])),
        "source": "TriageOps",
        "priority": priority,
        "tags": [
            f"source:triageops",
            f"decision:{triage_decision.lower()}",
            f"host:{host[:50]}",
        ],
        "details": {
            "triageops_alert_db_id": alert_db_id,
            "triage_decision": triage_decision,
            "escalated_by": triggered_by,
        },
    }

    if responders:
        payload["responders"] = responders

    logger.info(
        "OpsGenie trigger | alert_db_id={} host={} priority={}",
        alert_db_id, host, priority,
    )

    async with httpx.AsyncClient(
        timeout=10.0,
        headers={
            "Authorization": f"GenieKey {api_key}",
            "Content-Type": "application/json",
        },
    ) as client:
        try:
            resp = await client.post(OPSGENIE_ALERTS_ENDPOINT, json=payload)
        except httpx.TimeoutException as exc:
            raise OpsGenieError(f"OpsGenie timeout: {exc}", retryable=True)
        except httpx.RequestError as exc:
            raise OpsGenieError(f"OpsGenie network error: {exc}", retryable=True)

    if resp.status_code == 429:
        raise OpsGenieError("OpsGenie rate limited", retryable=True)
    if resp.status_code >= 500:
        raise OpsGenieError(f"OpsGenie server error {resp.status_code}", retryable=True)
    if resp.status_code not in (200, 201, 202):
        raise OpsGenieError(
            f"OpsGenie rejected: HTTP {resp.status_code} — {resp.text[:400]}",
            retryable=False,
        )

    response_body = resp.json()
    # OpsGenie 202 returns {"result": "Request will be processed", "requestId": "..."}
    logger.info(
        "OpsGenie alert created | alias=triageops-{} requestId={}",
        alert_db_id,
        response_body.get("requestId", "?"),
    )
    return {"request": payload, "response": response_body}
