"""
teams/client.py — Microsoft Teams incoming webhook client.

Sends Adaptive Card payloads to a Teams channel via an Incoming Webhook connector.

Teams incoming webhooks docs:
  https://learn.microsoft.com/en-us/microsoftteams/platform/webhooks-and-connectors/how-to/add-incoming-webhook

V1 constraints:
  - Incoming webhooks do NOT support interactive buttons (Action.Submit).
    That requires Bot Framework — out of scope for v1.
  - Action.OpenUrl buttons only (e.g. "View in Dashboard").
  - No approval row created for Teams — notification-only.

TEAMS_WEBHOOK_URL format:
  https://TENANT.webhook.office.com/webhookb2/...
"""

from __future__ import annotations

import os

import httpx
import sentry_sdk
from loguru import logger
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)


class TeamsError(Exception):
    def __init__(self, msg: str, retryable: bool = False):
        super().__init__(msg)
        self.retryable = retryable


@retry(
    retry=retry_if_exception_type(TeamsError),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=30),
    reraise=True,
)
async def post_card(payload: dict) -> None:
    """
    POST Adaptive Card payload to Teams incoming webhook URL.

    Teams returns HTTP 200 with body "1" on success.
    Raises TeamsError on failure.
    """
    webhook_url = os.environ.get("TEAMS_WEBHOOK_URL", "")
    if not webhook_url:
        raise TeamsError("TEAMS_WEBHOOK_URL not set", retryable=False)

    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            resp = await client.post(
                webhook_url,
                json=payload,
                headers={"Content-Type": "application/json"},
            )
        except httpx.TimeoutException as exc:
            raise TeamsError(f"Teams webhook timeout: {exc}", retryable=True)
        except httpx.RequestError as exc:
            raise TeamsError(f"Teams network error: {exc}", retryable=True)

    if resp.status_code == 429:
        raise TeamsError("Teams rate limited", retryable=True)
    if resp.status_code >= 500:
        raise TeamsError(f"Teams server error {resp.status_code}", retryable=True)
    # Teams returns 200 + body "1" on success; any other 4xx is a bad payload
    if resp.status_code != 200:
        raise TeamsError(
            f"Teams rejected payload: HTTP {resp.status_code} — {resp.text[:300]}",
            retryable=False,
        )

    logger.info("Teams card delivered | status={}", resp.status_code)
