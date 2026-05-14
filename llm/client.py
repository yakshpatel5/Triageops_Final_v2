"""
llm/client.py — OpenAI API client wrapper.

Responsibilities:
  - Initialise the async OpenAI client once (module-level singleton)
  - Call gpt-4o with response_format=json_object + schema hint
  - Retry on transient API errors with exponential backoff
  - Parse and validate JSON response into TriageOutput
  - Track token usage and latency for every call
  - Never raise outside of LLMError — callers always get a typed exception

HALLUCINATION RISKS (explicitly documented):
  1. response_format={"type":"json_object"} forces valid JSON but does NOT
     guarantee the fields match our schema — Pydantic validation is the gate.
  2. The model may produce a syntactically valid JSON that fails semantic
     validation (e.g. confidence_score=1.0 for a low-evidence classification).
     We log raw responses to alert_enrichments.raw_llm_response for audit.
  3. runbook_refs URLs are frequently confabulated — treat as title hints only.
  4. If the model returns NEEDS_REVIEW but we expect CRITICAL, the Celery task
     persists the result and routes to human review — never silently promote.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

import sentry_sdk
from loguru import logger
from openai import AsyncOpenAI, APIError, APITimeoutError, RateLimitError
from pydantic import ValidationError
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
    before_sleep_log,
)
import logging  # tenacity uses stdlib logging

from llm.prompts import PROMPT_VERSION, build_messages
from llm.schemas import EnrichmentContext, EnrichmentResult, TriageOutput

# ---------------------------------------------------------------------------
# Client singleton
# ---------------------------------------------------------------------------

_client: AsyncOpenAI | None = None


def get_openai_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise EnvironmentError("OPENAI_API_KEY environment variable not set")
        _client = AsyncOpenAI(
            api_key=api_key,
            timeout=30.0,          # 30s total — gpt-4o p99 latency is ~8s
            max_retries=0,         # we handle retries via tenacity for full control
        )
    return _client


# ---------------------------------------------------------------------------
# Typed exception
# ---------------------------------------------------------------------------

class LLMError(Exception):
    """Wraps all LLM client failures with context for caller handling."""
    def __init__(self, message: str, retryable: bool = False, raw: Any = None):
        super().__init__(message)
        self.retryable = retryable
        self.raw = raw


# ---------------------------------------------------------------------------
# Retry decorator — applied to the inner API call only, not JSON parsing
# ---------------------------------------------------------------------------

_RETRYABLE = (APITimeoutError, RateLimitError)

_retry_policy = retry(
    retry=retry_if_exception_type(_RETRYABLE),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=30),
    before_sleep=before_sleep_log(logging.getLogger("triageops.llm"), logging.WARNING),
    reraise=True,
)


async def _call_openai(messages: list[dict]) -> tuple[dict[str, Any], int, int]:
    """
    Raw OpenAI call wrapped with retry.
    Returns (parsed_json_dict, prompt_tokens, completion_tokens).
    Raises LLMError on failure.
    """
    client = get_openai_client()
    model = os.getenv("OPENAI_MODEL", "gpt-4o-2024-11-20")

    @_retry_policy
    async def _inner():
        return await client.chat.completions.create(
            model=model,
            messages=messages,
            response_format={"type": "json_object"},
            temperature=0.1,       # low temp → consistent classification
            max_tokens=800,        # TriageOutput is bounded — never need 4k tokens
            seed=42,               # improves reproducibility for same input
        )

    try:
        response = await _inner()
    except RateLimitError as exc:
        raise LLMError(f"OpenAI rate limit exceeded: {exc}", retryable=True)
    except APITimeoutError as exc:
        raise LLMError(f"OpenAI timeout: {exc}", retryable=True)
    except APIError as exc:
        raise LLMError(f"OpenAI API error: {exc}", retryable=False, raw=str(exc))

    raw_content = response.choices[0].message.content
    if not raw_content:
        raise LLMError("OpenAI returned empty content", retryable=False)

    try:
        parsed = json.loads(raw_content)
    except json.JSONDecodeError as exc:
        raise LLMError(
            f"OpenAI response was not valid JSON: {exc}",
            retryable=False,
            raw=raw_content,
        )

    prompt_tokens = response.usage.prompt_tokens if response.usage else 0
    completion_tokens = response.usage.completion_tokens if response.usage else 0

    return parsed, prompt_tokens, completion_tokens, model, response.model_dump()


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

async def classify_alert(ctx: EnrichmentContext) -> EnrichmentResult:
    """
    Full pipeline: build prompt → call OpenAI → validate → return EnrichmentResult.

    HALLUCINATION MITIGATION:
    - We validate every field via TriageOutput (Pydantic).
    - Unsafe suggested_action strings are redacted in TriageOutput.sanitise_action.
    - raw_llm_response is always stored for post-hoc audit.
    - On ValidationError we fall back to NEEDS_REVIEW rather than surfacing
      a malformed classification to the Slack approval flow.

    Raises LLMError on unrecoverable failure (caller should handle + alert human).
    """
    messages = build_messages(ctx)
    model_used = os.getenv("OPENAI_MODEL", "gpt-4o-2024-11-20")

    logger.info(
        "LLM classify | tenant={} alert_id={} host={} prompt_version={}",
        ctx.tenant_id,
        ctx.alert_id,
        ctx.host,
        PROMPT_VERSION,
    )

    t0 = time.monotonic()
    try:
        parsed, prompt_tokens, completion_tokens, model_used, raw_response = (
            await _call_openai(messages)
        )
    except LLMError:
        raise
    except Exception as exc:
        sentry_sdk.capture_exception(exc)
        raise LLMError(f"Unexpected error calling OpenAI: {exc}", retryable=False)

    latency_ms = int((time.monotonic() - t0) * 1000)

    logger.debug(
        "LLM raw response | tenant={} alert_id={} latency_ms={} tokens={}/{}",
        ctx.tenant_id,
        ctx.alert_id,
        latency_ms,
        prompt_tokens,
        completion_tokens,
    )

    # Validate parsed JSON → TriageOutput
    try:
        triage_output = TriageOutput.model_validate(parsed)
    except ValidationError as exc:
        # ⚠️ HALLUCINATION FALLBACK: LLM returned structurally invalid response.
        # Degrade to NEEDS_REVIEW so a human reviews rather than a bad
        # classification propagating into the approval flow.
        logger.warning(
            "TriageOutput validation failed — falling back to NEEDS_REVIEW | "
            "tenant={} alert_id={} errors={} raw={}",
            ctx.tenant_id,
            ctx.alert_id,
            exc.error_count(),
            parsed,
        )
        sentry_sdk.capture_exception(exc)
        triage_output = TriageOutput(
            decision="NEEDS_REVIEW",
            confidence_score=0.0,
            reasoning=(
                f"LLM response failed schema validation ({exc.error_count()} errors). "
                "Routed to human review. Raw response stored for debugging."
            ),
            suggested_action=None,
            runbook_refs=[],
            alert_category="unknown",
            is_flapping=False,
        )

    logger.info(
        "LLM result | tenant={} alert_id={} decision={} confidence={} latency_ms={}",
        ctx.tenant_id,
        ctx.alert_id,
        triage_output.decision,
        triage_output.confidence_score,
        latency_ms,
    )

    return EnrichmentResult(
        triage_output=triage_output,
        prompt_version=PROMPT_VERSION,
        model_used=model_used,
        raw_llm_response=raw_response,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        latency_ms=latency_ms,
    )
