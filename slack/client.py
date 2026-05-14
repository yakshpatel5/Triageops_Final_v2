"""
slack/client.py — Slack Web API client wrapper.

Uses httpx (async) rather than the slack_sdk to keep the dependency footprint
small and give us full control over retry/timeout behaviour.

Responsibilities:
  - POST chat.postMessage (send alert notification)
  - POST chat.update (update message after approval action)
  - Verify Slack request signatures on incoming interactivity payloads
  - Retry on Slack's rate limit (429) with Retry-After header respect

Slack API docs:
  https://api.slack.com/methods/chat.postMessage
  https://api.slack.com/authentication/verifying-requests-from-slack
"""

from __future__ import annotations

import hashlib
import hmac
import os
import time
from typing import Any

import httpx
import sentry_sdk
from loguru import logger
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

SLACK_API_BASE = "https://slack.com/api"

# ---------------------------------------------------------------------------
# Typed exceptions
# ---------------------------------------------------------------------------

class SlackError(Exception):
    """Wraps Slack API errors. retryable=True for rate limits / transient 5xx."""
    def __init__(self, message: str, retryable: bool = False, slack_error: str | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.slack_error = slack_error  # Slack's own error code e.g. "channel_not_found"


class SlackSignatureError(Exception):
    """Raised when HMAC signature verification fails on incoming payload."""


# ---------------------------------------------------------------------------
# Singleton async client
# ---------------------------------------------------------------------------

_http_client: httpx.AsyncClient | None = None


def _get_http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None:
        token = os.environ.get("SLACK_BOT_TOKEN", "")
        if not token:
            raise EnvironmentError("SLACK_BOT_TOKEN environment variable not set")
        _http_client = httpx.AsyncClient(
            base_url=SLACK_API_BASE,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json; charset=utf-8",
            },
            timeout=httpx.Timeout(10.0, connect=5.0),
        )
    return _http_client


async def close_slack_client() -> None:
    global _http_client
    if _http_client:
        await _http_client.aclose()
        _http_client = None


# ---------------------------------------------------------------------------
# Core API call with retry
# ---------------------------------------------------------------------------

@retry(
    retry=retry_if_exception_type(SlackError),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=60),
    reraise=True,
)
async def _post(endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
    """
    POST to a Slack API endpoint. Handles rate limits and Slack error codes.
    Retries only on retryable SlackError.
    """
    client = _get_http_client()
    try:
        resp = await client.post(f"/{endpoint}", json=payload)
    except httpx.TimeoutException as exc:
        raise SlackError(f"Slack API timeout: {exc}", retryable=True)
    except httpx.RequestError as exc:
        raise SlackError(f"Slack network error: {exc}", retryable=True)

    if resp.status_code == 429:
        # Respect Retry-After — tenacity will retry after its own wait
        retry_after = int(resp.headers.get("Retry-After", "5"))
        logger.warning("Slack rate limited — Retry-After: {}s", retry_after)
        raise SlackError(
            f"Slack rate limited (retry after {retry_after}s)",
            retryable=True,
        )

    if resp.status_code >= 500:
        raise SlackError(
            f"Slack server error {resp.status_code}",
            retryable=True,
        )

    body = resp.json()
    if not body.get("ok"):
        slack_err = body.get("error", "unknown")
        # Non-retryable Slack errors (bad channel, missing scope, etc.)
        retryable = slack_err in ("timeout", "request_timeout", "service_unavailable")
        raise SlackError(
            f"Slack API error: {slack_err}",
            retryable=retryable,
            slack_error=slack_err,
        )

    return body


# ---------------------------------------------------------------------------
# Public API surface
# ---------------------------------------------------------------------------

async def post_message(payload: dict[str, Any]) -> str:
    """
    Send a Block Kit message to Slack.
    Returns the message timestamp (ts) — Slack's unique message identifier.

    The ts is stored in SlackApproval.slack_ts and used to:
      1. Update the message after a human takes action
      2. Correlate interactive callbacks
    """
    try:
        body = await _post("chat.postMessage", payload)
    except SlackError as exc:
        sentry_sdk.capture_exception(exc)
        logger.error(
            "Failed to post Slack message | channel={} error={}",
            payload.get("channel"),
            exc,
        )
        raise

    ts = body.get("ts") or body.get("message", {}).get("ts")
    logger.info(
        "Slack message sent | channel={} ts={}",
        payload.get("channel"),
        ts,
    )
    return ts


async def update_message(channel: str, ts: str, blocks: list[dict]) -> None:
    """
    Update an existing Slack message (e.g. after human approval action).
    Replaces blocks in-place — used to disable buttons after actioning.
    """
    try:
        await _post("chat.update", {
            "channel": channel,
            "ts": ts,
            "blocks": blocks,
            "text": "Alert actioned",   # fallback text
        })
        logger.info("Slack message updated | channel={} ts={}", channel, ts)
    except SlackError as exc:
        # Update failure is non-fatal — log but don't re-raise
        # The approval is already persisted to DB; UI staleness is acceptable
        sentry_sdk.capture_exception(exc)
        logger.error(
            "Failed to update Slack message | channel={} ts={} error={}",
            channel, ts, exc,
        )


# ---------------------------------------------------------------------------
# Signature verification — MUST be called on every incoming Slack payload
# ---------------------------------------------------------------------------

def verify_slack_signature(
    body_bytes: bytes,
    timestamp: str,
    signature: str,
) -> None:
    """
    Verify Slack's HMAC-SHA256 request signature.
    Raises SlackSignatureError if invalid or replayed (>5 min old).

    Call this at the top of every Slack interactivity endpoint BEFORE
    parsing the payload. Never process an unverified payload.

    Docs: https://api.slack.com/authentication/verifying-requests-from-slack
    """
    signing_secret = os.environ.get("SLACK_SIGNING_SECRET", "")
    if not signing_secret:
        raise EnvironmentError("SLACK_SIGNING_SECRET not set")

    # Replay attack guard — reject payloads older than 5 minutes
    try:
        ts_int = int(timestamp)
    except (ValueError, TypeError):
        raise SlackSignatureError("Invalid X-Slack-Request-Timestamp")

    if abs(time.time() - ts_int) > 300:
        raise SlackSignatureError(
            f"Request timestamp too old ({ts_int}) — possible replay attack"
        )

    # Compute expected signature
    sig_basestring = f"v0:{timestamp}:{body_bytes.decode('utf-8')}".encode()
    computed = "v0=" + hmac.new(
        signing_secret.encode(),
        sig_basestring,
        hashlib.sha256,
    ).hexdigest()

    # Constant-time comparison to prevent timing attacks
    if not hmac.compare_digest(computed, signature):
        raise SlackSignatureError("Slack signature mismatch — request not from Slack")
