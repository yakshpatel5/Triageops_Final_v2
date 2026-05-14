"""
llm/schemas.py — Pydantic v2 models for LLM pipeline input/output.

These are INTERNAL schemas (not API-facing). They define the contract
between the prompt layer and the rest of the system.

HALLUCINATION RISK sections are explicitly marked throughout.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


# ---------------------------------------------------------------------------
# LLM structured output — what we instruct gpt-4o to return as JSON
# ---------------------------------------------------------------------------

class RunbookRef(BaseModel):
    """A vendor doc or internal runbook reference suggested by the LLM."""
    title: str
    url: str | None = None          # LLM may hallucinate URLs — NEVER display as trusted
    relevance: str | None = None    # one-line reason this runbook applies


class TriageOutput(BaseModel):
    """
    Strict schema for gpt-4o JSON response.

    HALLUCINATION RISKS:
    - suggested_action: LLM may invent plausible-sounding but wrong commands.
      Always display with "AI suggestion — verify before executing" caveat.
    - runbook_refs: URLs are frequently hallucinated. Treat as title hints only;
      resolve URLs server-side against a trusted vendor doc index in V1.2.
    - confidence_score: LLM self-reported confidence is not calibrated probability.
      It correlates with certainty but should not be treated as P(correct).
    - reasoning: may contain confident-sounding incorrect assertions.
    """

    decision: Literal["CRITICAL", "NOISE", "NEEDS_REVIEW"]
    confidence_score: float = Field(..., ge=0.0, le=1.0)
    reasoning: str = Field(..., min_length=10, max_length=2000)
    suggested_action: str | None = Field(None, max_length=1000)
    runbook_refs: list[RunbookRef] = Field(default_factory=list, max_length=5)
    alert_category: str | None = Field(
        None,
        max_length=128,
        description="e.g. 'disk-space', 'cpu-spike', 'network-timeout', 'flapping'",
    )
    is_flapping: bool = False   # LLM detected repeated state changes in message

    @field_validator("confidence_score")
    @classmethod
    def round_confidence(cls, v: float) -> float:
        return round(v, 4)

    @field_validator("suggested_action", mode="before")
    @classmethod
    def sanitise_action(cls, v: Any) -> str | None:
        """Strip suggested actions that contain shell injection characters."""
        if v is None:
            return None
        dangerous = ["$(", "`", "&&", "||", ";", "|", ">", "<", "rm -", "dd if"]
        s = str(v)
        for pattern in dangerous:
            if pattern in s:
                # Log and discard rather than block — we never auto-execute anyway
                return f"[suggested action redacted — contained unsafe pattern: {pattern!r}]"
        return s


class EnrichmentContext(BaseModel):
    """
    Everything fed INTO the LLM prompt. Stored for reproducibility + debugging.
    """
    alert_id: str
    tenant_id: str
    source: str
    severity: str
    host: str
    message: str
    # Optional context window — empty in V1, populated by log fetcher in V1.2
    recent_log_lines: list[str] = Field(default_factory=list, max_length=50)
    similar_past_alerts: list[dict[str, Any]] = Field(default_factory=list, max_length=5)
    host_metadata: dict[str, Any] = Field(default_factory=dict)


class EnrichmentResult(BaseModel):
    """Full pipeline result persisted to alert_enrichments table."""
    triage_output: TriageOutput
    prompt_version: str
    model_used: str
    raw_llm_response: dict[str, Any]
    prompt_tokens: int
    completion_tokens: int
    latency_ms: int
