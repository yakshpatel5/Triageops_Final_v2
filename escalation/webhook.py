"""
escalation/webhook.py — Generic outbound webhook escalation.

Used when neither PagerDuty nor OpsGenie is configured.
Posts a signed JSON payload to a configurable URL — compatible with
n8n webhooks, custom incident handlers, or any HTTP endpoint.

Signing: HMAC-SHA256 of request body with ESCALATION_WEBHOOK_SECRET.
Receiver should verify the X-TriageOps-Signature header.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import socket
import time
from typing import Any
from urllib.parse import urlparse

import httpx
from loguru import logger
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential


class WebhookError(Exception):
    def __init__(self, msg: str, retryable: bool = False):
        super().__init__(msg)
        self.retryable = retryable


BLOCKED_RANGES = [
    ipaddress.ip_network("169.254.0.0/16"),  # AWS/Cloud metadata
    ipaddress.ip_network("10.0.0.0/8"),      # Private RFC1918
    ipaddress.ip_network("172.16.0.0/12"),   # Private RFC1918
    ipaddress.ip_network("192.168.0.0/16"),  # Private RFC1918
    ipaddress.ip_network("127.0.0.0/8"),     # Loopback
    ipaddress.ip_network("::1/128"),         # IPv6 Loopback
    ipaddress.ip_network("fe80::/10"),       # IPv6 Link-local
]


def is_safe_url(url: str) -> bool:
    """
    Check if a URL is safe from SSRF by verifying its hostname resolves to a public IP.
    """
    try:
        parsed = urlparse(url)
        if not parsed.hostname:
            return False
        
        # Resolve hostname to IP
        # Note: This is vulnerable to DNS rebinding if the client doesn't pin the IP.
        # For a production system, we should resolve once and use the IP in the request.
        ip_str = socket.gethostbyname(parsed.hostname)
        ip = ipaddress.ip_address(ip_str)
        
        return not any(ip in net for net in BLOCKED_RANGES)
    except Exception as exc:
        logger.warning("SSRF check failed for URL {}: {}", url, exc)
        return False


@retry(
    retry=retry_if_exception_type(WebhookError),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=30),
    reraise=True,
)
async def trigger_webhook(
    url: str,
    secret: str | None,
    alert_db_id: str,
    alert_id: str,
    host: str,
    message: str,
    severity: str,
    triage_decision: str,
    llm_reasoning: str | None,
    suggested_action: str | None,
    triggered_by: str,
    **_,   # absorb extra kwargs from dispatcher's common_kwargs
) -> dict[str, Any]:
    """POST a signed escalation payload to the configured webhook URL."""
    
    if not is_safe_url(url):
        logger.error("SSRF protection blocked webhook escalation to unsafe URL: {}", url)
        raise WebhookError(f"Unsafe webhook URL: {url}", retryable=False)

    payload = {
        "event": "triageops.escalation",
        "timestamp": int(time.time()),
        "alert": {
            "alert_db_id":     alert_db_id,
            "alert_id":        alert_id,
            "host":            host,
            "message":         message,
            "severity":        severity,
            "triage_decision": triage_decision,
            "llm_reasoning":   llm_reasoning,
            # HALLUCINATION RISK label preserved in outbound payload
            "suggested_action": (
                f"[AI suggestion — verify before executing] {suggested_action}"
                if suggested_action else None
            ),
        },
        "triggered_by": triggered_by,
        "source": "TriageOps",
    }

    body_bytes = json.dumps(payload, separators=(",", ":")).encode()

    headers: dict[str, str] = {"Content-Type": "application/json"}
    if secret:
        sig = hmac.new(
            secret.encode(), body_bytes, hashlib.sha256
        ).hexdigest()
        headers["X-TriageOps-Signature"] = f"sha256={sig}"

    logger.info("Webhook escalation | url={} alert_db_id={}", url, alert_db_id)

    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            resp = await client.post(url, content=body_bytes, headers=headers)
        except httpx.TimeoutException as exc:
            raise WebhookError(f"Webhook timeout: {exc}", retryable=True)
        except httpx.RequestError as exc:
            raise WebhookError(f"Webhook network error: {exc}", retryable=True)

    if resp.status_code == 429:
        raise WebhookError("Webhook rate limited", retryable=True)
    if resp.status_code >= 500:
        raise WebhookError(f"Webhook server error {resp.status_code}", retryable=True)
    if resp.status_code >= 400:
        raise WebhookError(
            f"Webhook rejected: {resp.status_code} — {resp.text[:300]}",
            retryable=False,
        )

    logger.info("Webhook escalation delivered | url={} status={}", url, resp.status_code)
    return {"request": payload, "response": {"status_code": resp.status_code}}
