"""
routers/ops_schemas.py — Response schemas for the /ops/* read-only API.

These are API-facing (serialised to JSON) so they are separate from the
internal llm/schemas.py models. All fields are explicitly typed — no
model_dump() passthrough that could accidentally expose raw_payload or
raw_llm_response to the API consumer.

Pagination: all list endpoints return a Page[T] envelope.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Generic, TypeVar
from uuid import UUID

from pydantic import BaseModel, Field

T = TypeVar("T")


# ---------------------------------------------------------------------------
# Pagination envelope
# ---------------------------------------------------------------------------

class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int
    page: int
    page_size: int
    has_next: bool


# ---------------------------------------------------------------------------
# Alert
# ---------------------------------------------------------------------------

class AlertSummary(BaseModel):
    """Lightweight alert row — used in list endpoints."""
    id: UUID
    alert_id: str
    tenant_id: str
    source: str
    severity: str
    host: str
    message: str
    received_at: datetime
    created_at: datetime
    # Denormalised from enrichment join — null if not yet enriched
    triage_decision: str | None = None
    confidence_score: str | None = None

    model_config = {"from_attributes": True}


class AlertDetail(AlertSummary):
    """Full alert row including enrichment context."""
    enrichment: EnrichmentDetail | None = None
    latest_approval: ApprovalSummary | None = None

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Enrichment
# ---------------------------------------------------------------------------

class RunbookRefOut(BaseModel):
    title: str
    url: str | None = None
    relevance: str | None = None


class EnrichmentDetail(BaseModel):
    id: UUID
    triage_decision: str
    confidence_score: str
    llm_reasoning: str | None
    # HALLUCINATION RISK: always render with caveat in UI
    suggested_action: str | None
    runbook_refs: list[RunbookRefOut]
    alert_category: str | None = None
    is_flapping: bool = False
    prompt_version: str
    model_used: str
    prompt_tokens: str | None
    completion_tokens: str | None
    latency_ms: str | None
    enriched_at: datetime

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Approval
# ---------------------------------------------------------------------------

class ApprovalSummary(BaseModel):
    id: UUID
    status: str
    action: str | None
    actioned_by_name: str | None
    actioned_at: datetime | None
    slack_channel: str
    sent_at: datetime
    expires_at: datetime | None

    model_config = {"from_attributes": True}


class ApprovalDetail(ApprovalSummary):
    enrichment_id: UUID
    slack_ts: str | None
    action_note: str | None
    actioned_by: str | None    # Slack user_id

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Escalation
# ---------------------------------------------------------------------------

class EscalationSummary(BaseModel):
    id: UUID
    provider: str
    status: str
    provider_incident_id: str | None
    provider_incident_url: str | None
    triggered_by: str | None
    triggered_at: datetime
    error_detail: str | None

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Stats / dashboard
# ---------------------------------------------------------------------------

class TenantStats(BaseModel):
    """Aggregated metrics for the tenant dashboard — last 24h and all time."""
    tenant_id: str
    # Last 24h
    alerts_24h: int
    critical_24h: int
    noise_24h: int
    needs_review_24h: int
    pending_approvals: int
    escalations_24h: int
    # All time
    total_alerts: int
    total_escalations: int
    # LLM quality
    avg_confidence_24h: float | None    # mean confidence score over last 24h
    avg_latency_ms_24h: float | None    # mean LLM latency ms over last 24h
    # Noise ratio (useful for tuning alert thresholds)
    noise_ratio_24h: float | None       # noise / (critical + noise) last 24h


class HostHotspot(BaseModel):
    """Hosts with the most alerts in the given window."""
    host: str
    alert_count: int
    critical_count: int
    last_seen: datetime


class DecisionDistribution(BaseModel):
    decision: str
    count: int
    percentage: float
