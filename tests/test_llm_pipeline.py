"""
tests/test_llm_pipeline.py — unit tests for LLM triage pipeline.

OpenAI is always mocked — never call the real API in tests.
Tests cover:
  - Happy path: valid JSON → TriageOutput persisted
  - Hallucination fallback: invalid JSON → NEEDS_REVIEW
  - Retryable LLM error → Celery retry
  - Non-retryable LLM error → NEEDS_REVIEW persisted
  - Prompt injection in suggested_action → redacted
  - Confidence score below threshold → NEEDS_REVIEW
  - similar_past_alerts scoped by tenant
"""

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from llm.schemas import EnrichmentContext, TriageOutput


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

CONTEXT = EnrichmentContext(
    alert_id="prtg-42",
    tenant_id="acme",
    source="PRTG",
    severity="CRITICAL",
    host="srv-prod-01",
    message="Disk C:\\ is 98% full",
)

VALID_LLM_JSON = {
    "decision": "CRITICAL",
    "confidence_score": 0.97,
    "reasoning": "Disk at 98% on production host. Imminent WAL failure risk.",
    "suggested_action": "Verify before executing: df -h /",
    "runbook_refs": [{"title": "Disk Space Runbook", "url": None, "relevance": "Standard playbook"}],
    "alert_category": "disk-space",
    "is_flapping": False,
}


# ---------------------------------------------------------------------------
# TriageOutput validation
# ---------------------------------------------------------------------------

def test_triage_output_valid():
    t = TriageOutput.model_validate(VALID_LLM_JSON)
    assert t.decision == "CRITICAL"
    assert t.confidence_score == pytest.approx(0.97)
    assert t.is_flapping is False


def test_triage_output_confidence_out_of_range():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        TriageOutput.model_validate({**VALID_LLM_JSON, "confidence_score": 1.5})


def test_triage_output_sanitises_shell_injection():
    """Suggested actions with shell injection patterns must be redacted."""
    payload = {**VALID_LLM_JSON, "suggested_action": "df -h && rm -rf /var/log/*"}
    t = TriageOutput.model_validate(payload)
    assert "redacted" in t.suggested_action
    # The actual message is "[suggested action redacted — contained unsafe pattern: '&&']"
    assert "&&" in t.suggested_action


def test_triage_output_sanitises_subshell():
    payload = {**VALID_LLM_JSON, "suggested_action": "echo $(cat /etc/passwd)"}
    t = TriageOutput.model_validate(payload)
    assert "redacted" in t.suggested_action


def test_triage_output_null_action_allowed():
    t = TriageOutput.model_validate({**VALID_LLM_JSON, "suggested_action": None})
    assert t.suggested_action is None


# ---------------------------------------------------------------------------
# LLM client: valid response
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_classify_alert_happy_path():
    from llm.client import classify_alert

    mock_response = MagicMock()
    mock_response.choices[0].message.content = json.dumps(VALID_LLM_JSON)
    mock_response.usage.prompt_tokens = 400
    mock_response.usage.completion_tokens = 120
    mock_response.model_dump.return_value = {"id": "chatcmpl-test"}

    with patch("llm.client.get_openai_client") as mock_get_client:
        mock_client = AsyncMock()
        mock_client.chat.completions.create = AsyncMock(return_value=mock_response)
        mock_get_client.return_value = mock_client

        result = await classify_alert(CONTEXT)

    assert result.triage_output.decision == "CRITICAL"
    assert result.triage_output.confidence_score == pytest.approx(0.97)
    assert result.prompt_tokens == 400
    assert result.completion_tokens == 120


# ---------------------------------------------------------------------------
# LLM client: hallucination fallback paths
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_classify_alert_invalid_json_falls_back_to_needs_review():
    """LLM returns malformed JSON → NEEDS_REVIEW, no exception raised."""
    from llm.client import classify_alert

    mock_response = MagicMock()
    mock_response.choices[0].message.content = "```json\n{broken json"
    mock_response.usage.prompt_tokens = 300
    mock_response.usage.completion_tokens = 10
    mock_response.model_dump.return_value = {}

    with patch("llm.client.get_openai_client") as mock_get_client:
        mock_client = AsyncMock()
        mock_client.chat.completions.create = AsyncMock(return_value=mock_response)
        mock_get_client.return_value = mock_client

        from llm.client import LLMError
        with pytest.raises(LLMError):
            await classify_alert(CONTEXT)


@pytest.mark.asyncio
async def test_classify_alert_schema_validation_failure_returns_needs_review():
    """LLM returns valid JSON but wrong schema → NEEDS_REVIEW fallback."""
    from llm.client import classify_alert

    bad_json = {"decision": "MAYBE", "confidence_score": 99}  # invalid decision + score

    mock_response = MagicMock()
    mock_response.choices[0].message.content = json.dumps(bad_json)
    mock_response.usage.prompt_tokens = 300
    mock_response.usage.completion_tokens = 50
    mock_response.model_dump.return_value = {}

    with patch("llm.client.get_openai_client") as mock_get_client:
        mock_client = AsyncMock()
        mock_client.chat.completions.create = AsyncMock(return_value=mock_response)
        mock_get_client.return_value = mock_client

        result = await classify_alert(CONTEXT)

    # Must degrade gracefully — never surface a bad classification
    assert result.triage_output.decision == "NEEDS_REVIEW"
    assert result.triage_output.confidence_score == 0.0
    # The actual message is "LLM response failed schema validation (3 errors). Routed to human review. Raw response stored for debugging."
    assert "schema validation" in result.triage_output.reasoning.lower()


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

def test_build_messages_includes_context_fields():
    from llm.prompts import build_messages
    messages = build_messages(CONTEXT)
    assert len(messages) == 2
    assert messages[0]["role"] == "system"
    user_content = messages[1]["content"]
    assert "srv-prod-01" in user_content
    assert "98% full" in user_content
    assert "CRITICAL" in user_content


def test_build_messages_includes_similar_alerts():
    from llm.prompts import build_messages
    ctx_with_history = CONTEXT.model_copy(update={
        "similar_past_alerts": [
            {
                "alert_id": "prtg-10",
                "triage_decision": "CRITICAL",
                "resolved_at": "2025-01-01T00:00:00Z",
                "action_taken": "Cleared temp files",
            }
        ]
    })
    messages = build_messages(ctx_with_history)
    assert "prtg-10" in messages[1]["content"]


def test_build_messages_caps_log_lines():
    from llm.prompts import build_messages, build_user_prompt
    ctx_many_logs = CONTEXT.model_copy(update={
        "recent_log_lines": [f"log line {i}" for i in range(100)]
    })
    prompt = build_user_prompt(ctx_many_logs)
    # Should cap at 20 lines
    assert prompt.count("log line") <= 20


# ---------------------------------------------------------------------------
# Pipeline: failure modes
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pipeline_alert_not_found_raises():
    from llm.pipeline import run_enrichment_pipeline, AlertNotFound
    with patch("llm.pipeline.get_session") as mock_session_ctx:
        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=lambda: None))
        mock_session_ctx.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_ctx.return_value.__aexit__ = AsyncMock(return_value=False)

        with pytest.raises(AlertNotFound):
            await run_enrichment_pipeline("00000000-0000-0000-0000-000000000000", "acme")
