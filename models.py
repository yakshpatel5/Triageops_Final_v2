"""
SQLAlchemy ORM models — all tables are tenant-scoped.
Composite unique constraint on (alert_id, tenant_id) enforces idempotency at DB level
as a safety net; application-level dedup runs first to avoid unnecessary writes.
"""

import uuid
from datetime import datetime, timezone

from enum import Enum

from sqlalchemy import (
    Column, DateTime, Enum as SAEnum,
    Index, String, Text, UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase

from schemas import AlertSource, AlertSeverity  # re-use enums — single source of truth


class Base(DeclarativeBase):
    pass


class Alert(Base):
    __tablename__ = "alerts"

    id = Column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        nullable=False,
    )
    alert_id = Column(String(512), nullable=False, index=True)      # source system ID
    tenant_id = Column(String(256), nullable=False, index=True)
    source = Column(SAEnum(AlertSource, name="alert_source"), nullable=False)
    severity = Column(SAEnum(AlertSeverity, name="alert_severity"), nullable=False)
    host = Column(String(512), nullable=False)
    message = Column(Text, nullable=False)
    raw_payload   = Column(JSONB, nullable=False)                    # original webhook body
    is_suppressed = Column(String(1), nullable=False, default="0")    # "1" if matched a suppression rule
    received_at = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        # DB-level idempotency guard — duplicate (alert_id, tenant_id) raises IntegrityError
        UniqueConstraint("alert_id", "tenant_id", name="uq_alert_tenant"),
        # Common query patterns
        Index("ix_alerts_tenant_created", "tenant_id", "created_at"),
        Index("ix_alerts_tenant_severity", "tenant_id", "severity"),
    )

    def __repr__(self) -> str:
        return f"<Alert {self.alert_id!r} tenant={self.tenant_id!r} severity={self.severity}>"


class TriageDecision(str, Enum):
    """LLM classification outcome — stored verbatim for audit trail."""
    CRITICAL = "CRITICAL"
    NOISE = "NOISE"
    NEEDS_REVIEW = "NEEDS_REVIEW"   # LLM confidence too low to auto-classify


# Import Python enum for TriageDecision SA column
from enum import Enum as PyEnum  # noqa: E402 — keep near usage


class AlertEnrichment(Base):
    """
    LLM triage result + enrichment context for one Alert.
    One-to-one with Alert (enforced by unique constraint on alert_db_id).
    A new row is inserted on first enrichment; re-enrichment overwrites
    via upsert (see llm/pipeline.py).
    """
    __tablename__ = "alert_enrichments"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # FK to alerts.id — no ORM relationship to keep models lightweight
    alert_db_id = Column(
        UUID(as_uuid=True),
        nullable=False,
        unique=True,     # one enrichment per alert
        index=True,
    )
    tenant_id = Column(String(256), nullable=False, index=True)

    # LLM classification
    triage_decision = Column(
        SAEnum(
            "CRITICAL", "NOISE", "NEEDS_REVIEW",
            name="triage_decision",
            create_type=True,
        ),
        nullable=False,
    )
    confidence_score = Column(String(8), nullable=False)   # "0.00"–"1.00" stored as string for exact repr
    llm_reasoning = Column(Text, nullable=True)            # chain-of-thought summary

    # Enrichment context
    suggested_action = Column(Text, nullable=True)         # HALLUCINATION RISK — see pipeline.py
    runbook_refs = Column(JSONB, nullable=True)            # [{"title": ..., "url": ...}]
    similar_past_alerts = Column(JSONB, nullable=True)     # [{alert_id, resolved_at, action_taken}]

    # Prompt audit — critical for debugging hallucinations
    prompt_version = Column(String(64), nullable=False)    # e.g. "triage-v1.2"
    raw_llm_response = Column(JSONB, nullable=False)       # full OpenAI response object
    model_used = Column(String(128), nullable=False)       # e.g. "gpt-4o-2024-11-20"
    prompt_tokens = Column(String(16), nullable=True)
    completion_tokens = Column(String(16), nullable=True)
    latency_ms = Column(String(16), nullable=True)

    enriched_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        Index("ix_enrichments_tenant_decision", "tenant_id", "triage_decision"),
        Index("ix_enrichments_enriched_at", "enriched_at"),
    )

    def __repr__(self) -> str:
        return (
            f"<AlertEnrichment alert={self.alert_db_id} "
            f"decision={self.triage_decision} confidence={self.confidence_score}>"
        )


class ApprovalAction(str, Enum):
    """Human decision recorded when a NOC engineer acts on a Slack message."""
    ACKNOWLEDGE   = "ACKNOWLEDGE"    # alert is real, engineer taking ownership
    SUPPRESS      = "SUPPRESS"       # confirmed noise, suppress future similar
    ESCALATE      = "ESCALATE"       # critical — escalate to Tier 2 / on-call
    DISMISS       = "DISMISS"        # duplicate / stale, no action needed


class ApprovalStatus(str, Enum):
    PENDING   = "PENDING"     # Slack message sent, awaiting human action
    ACTIONED  = "ACTIONED"    # engineer clicked a button
    EXPIRED   = "EXPIRED"     # TTL passed with no action (cron job sets this)
    ERROR     = "ERROR"       # Slack delivery failed


class SlackApproval(Base):
    """
    Tracks every Slack notification sent for an alert enrichment,
    and the human approval action taken in response.

    One AlertEnrichment can have multiple SlackApprovals if the message
    is re-sent (e.g. after escalation timeout). The most recent ACTIONED
    row is the canonical decision.
    """
    __tablename__ = "slack_approvals"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    alert_db_id    = Column(UUID(as_uuid=True), nullable=False, index=True)
    enrichment_id  = Column(UUID(as_uuid=True), nullable=False, index=True)
    tenant_id      = Column(String(256), nullable=False, index=True)

    # Slack delivery
    slack_channel  = Column(String(256), nullable=False)
    slack_ts       = Column(String(64),  nullable=True, index=True)   # message timestamp — Slack's unique ID
    slack_message_blocks = Column(JSONB, nullable=True)               # Block Kit payload sent (audit)

    # Approval state
    status = Column(
        SAEnum("PENDING", "ACTIONED", "EXPIRED", "ERROR", name="approval_status", create_type=True),
        nullable=False,
        default="PENDING",
    )
    action = Column(
        SAEnum("ACKNOWLEDGE", "SUPPRESS", "ESCALATE", "DISMISS", name="approval_action", create_type=True),
        nullable=True,    # null until actioned
    )
    actioned_by    = Column(String(256), nullable=True)   # Slack user_id of the engineer
    actioned_by_name = Column(String(256), nullable=True) # display name for audit log
    action_note    = Column(Text, nullable=True)          # optional free-text from modal
    actioned_at    = Column(DateTime(timezone=True), nullable=True)

    # Timing
    sent_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    expires_at = Column(DateTime(timezone=True), nullable=True)   # set at send time

    __table_args__ = (
        Index("ix_slack_approvals_tenant_status", "tenant_id", "status"),
        #Index("ix_slack_approvals_slack_ts",      "slack_ts"),
    )

    def __repr__(self) -> str:
        return (
            f"<SlackApproval alert={self.alert_db_id} "
            f"status={self.status} action={self.action}>"
        )


class ApiKey(Base):
    """
    Simple API key → tenant mapping.
    Store hashed keys in production (bcrypt/sha256); plain for V1 dev speed.
    TODO: hash before storing in V1.1.
    """
    __tablename__ = "api_keys"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    key_hash = Column(String(256), nullable=False, unique=True, index=True)
    tenant_id = Column(String(256), nullable=False)
    description = Column(String(512), nullable=True)   # e.g. "Acme Corp production"
    is_active = Column(String(1), nullable=False, default="1")  # "1" / "0"
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    def __repr__(self) -> str:
        return f"<ApiKey tenant={self.tenant_id!r} active={self.is_active}>"


class EscalationProvider(str, Enum):
    PAGERDUTY  = "PAGERDUTY"
    OPSGENIE   = "OPSGENIE"
    WEBHOOK    = "WEBHOOK"    # generic outbound HTTP — configured per tenant


class EscalationStatus(str, Enum):
    TRIGGERED = "TRIGGERED"   # API call succeeded, incident created
    FAILED    = "FAILED"      # API call failed after retries
    SKIPPED   = "SKIPPED"     # provider not configured for tenant


class EscalationEvent(Base):
    """
    Audit record for every outbound escalation triggered by a human ESCALATE action.
    One SlackApproval(action=ESCALATE) -> one EscalationEvent row.
    Re-escalation creates a new row — never overwrites.
    """
    __tablename__ = "escalation_events"

    id              = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    approval_id     = Column(UUID(as_uuid=True), nullable=False, index=True)
    alert_db_id     = Column(UUID(as_uuid=True), nullable=False, index=True)
    tenant_id       = Column(String(256), nullable=False, index=True)

    provider = Column(
        SAEnum("PAGERDUTY", "OPSGENIE", "WEBHOOK", name="escalation_provider", create_type=True),
        nullable=False,
    )
    status = Column(
        SAEnum("TRIGGERED", "FAILED", "SKIPPED", name="escalation_status", create_type=True),
        nullable=False,
    )

    provider_incident_id  = Column(String(512), nullable=True)   # PD dedup_key / OG alias
    provider_incident_url = Column(String(1024), nullable=True)  # link posted back to Slack

    request_payload   = Column(JSONB, nullable=True)
    response_payload  = Column(JSONB, nullable=True)
    error_detail      = Column(Text, nullable=True)

    triggered_by  = Column(String(256), nullable=True)   # Slack user_id
    triggered_at  = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        Index("ix_escalation_tenant_status", "tenant_id", "status"),
        Index("ix_escalation_alert_db_id",   "alert_db_id"),
    )

    def __repr__(self) -> str:
        return (
            f"<EscalationEvent provider={self.provider} "
            f"status={self.status} alert={self.alert_db_id}>"
        )


# ===========================================================================
# V1.1 — Suppression Rules
# ===========================================================================

class MatchField(str, Enum):
    """Which alert field the suppression rule pattern is matched against."""
    HOST         = "HOST"          # exact or glob on alert.host
    ALERT_ID     = "ALERT_ID"      # exact match on source alert_id prefix
    MESSAGE      = "MESSAGE"       # substring or regex on alert.message
    HOST_MESSAGE = "HOST_MESSAGE"  # both host AND message must match (AND logic)


class SuppressionRule(Base):
    """
    A rule that auto-suppresses matching alerts at ingest time — before
    they reach the LLM pipeline or generate a Slack notification.

    Created automatically when a NOC engineer clicks SUPPRESS on a Slack
    approval, or manually via the /ops/suppression API.

    Matching is evaluated in _check_suppression() in routers/webhook.py.
    All pattern matching is case-insensitive.

    V1.1 pattern types:
      EXACT   — alert field must equal pattern exactly
      PREFIX  — alert field must start with pattern
      CONTAINS — alert field must contain pattern as substring
      REGEX   — alert field must match Python re.search(pattern, field)
                ⚠️ Regex rules are only created by API, never auto-generated
                   from Slack SUPPRESS (too risky without human review)
    """
    __tablename__ = "suppression_rules"

    id          = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id   = Column(String(256), nullable=False, index=True)

    # What to match
    match_field = Column(
        SAEnum("HOST", "ALERT_ID", "MESSAGE", "HOST_MESSAGE",
               name="match_field", create_type=True),
        nullable=False,
    )
    pattern_type = Column(
        SAEnum("EXACT", "PREFIX", "CONTAINS", "REGEX",
               name="pattern_type", create_type=True),
        nullable=False,
        default="EXACT",
    )
    host_pattern    = Column(String(512), nullable=True)     # used when match_field involves HOST
    message_pattern = Column(String(1024), nullable=True)    # used when match_field involves MESSAGE
    alert_id_prefix = Column(String(512), nullable=True)     # used for ALERT_ID match

    # Metadata
    reason          = Column(Text, nullable=True)            # human note
    is_active       = Column(String(1), nullable=False, default="1")
    hit_count       = Column(String(16), nullable=False, default="0")   # incremented on each match
    last_hit_at     = Column(DateTime(timezone=True), nullable=True)

    # Source — which approval triggered this rule (nullable for manually created)
    source_approval_id = Column(UUID(as_uuid=True), nullable=True, index=True)
    created_by         = Column(String(256), nullable=True)   # Slack user_id or "api"

    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    expires_at = Column(DateTime(timezone=True), nullable=True)   # null = never expires

    __table_args__ = (
        Index("ix_suppression_tenant_active",     "tenant_id", "is_active"),
        Index("ix_suppression_source_approval",   "source_approval_id"),
        Index("ix_suppression_tenant_match_field","tenant_id", "match_field"),
    )

    def __repr__(self) -> str:
        return (
            f"<SuppressionRule tenant={self.tenant_id!r} "
            f"field={self.match_field} type={self.pattern_type} "
            f"host={self.host_pattern!r} hits={self.hit_count}>"
        )
