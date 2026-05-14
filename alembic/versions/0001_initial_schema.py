"""Initial schema — all TriageOps tables.

Revision ID: 0001_initial_schema
Revises:
Create Date: 2025-01-15 12:00:00.000000

Creates:
  - alerts
  - alert_enrichments
  - slack_approvals
  - escalation_events
  - api_keys

All enum types are created explicitly so they can be managed by subsequent
migrations (renamed, extended) without surprises from autogenerate.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from alembic import op

revision = "0001_initial_schema"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── Enum types ────────────────────────────────────────────────────────
    alert_source_enum = postgresql.ENUM(
        "PRTG", "DATADOG", name="alert_source", create_type=False
    )
    alert_severity_enum = postgresql.ENUM(
        "CRITICAL", "HIGH", "MEDIUM", "LOW", "UNKNOWN",
        name="alert_severity", create_type=False,
    )
    triage_decision_enum = postgresql.ENUM(
        "CRITICAL", "NOISE", "NEEDS_REVIEW",
        name="triage_decision", create_type=False,
    )
    approval_status_enum = postgresql.ENUM(
        "PENDING", "ACTIONED", "EXPIRED", "ERROR",
        name="approval_status", create_type=False,
    )
    approval_action_enum = postgresql.ENUM(
        "ACKNOWLEDGE", "SUPPRESS", "ESCALATE", "DISMISS",
        name="approval_action", create_type=False,
    )
    escalation_provider_enum = postgresql.ENUM(
        "PAGERDUTY", "OPSGENIE", "WEBHOOK",
        name="escalation_provider", create_type=False,
    )
    escalation_status_enum = postgresql.ENUM(
        "TRIGGERED", "FAILED", "SKIPPED",
        name="escalation_status", create_type=False,
    )

    for enum in (
        alert_source_enum, alert_severity_enum, triage_decision_enum,
        approval_status_enum, approval_action_enum,
        escalation_provider_enum, escalation_status_enum,
    ):
        enum.create(op.get_bind(), checkfirst=True)

    # ── alerts ────────────────────────────────────────────────────────────
    op.create_table(
        "alerts",
        sa.Column("id",          postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("alert_id",    sa.String(512),  nullable=False),
        sa.Column("tenant_id",   sa.String(256),  nullable=False),
        sa.Column("source",      sa.Enum("PRTG", "DATADOG",    name="alert_source"),    nullable=False),
        sa.Column("severity",    sa.Enum("CRITICAL","HIGH","MEDIUM","LOW","UNKNOWN", name="alert_severity"), nullable=False),
        sa.Column("host",        sa.String(512),  nullable=False),
        sa.Column("message",     sa.Text,         nullable=False),
        sa.Column("raw_payload", postgresql.JSONB, nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at",  sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("alert_id", "tenant_id", name="uq_alert_tenant"),
    )
    op.create_index("ix_alerts_alert_id",        "alerts", ["alert_id"])
    op.create_index("ix_alerts_tenant_id",        "alerts", ["tenant_id"])
    op.create_index("ix_alerts_tenant_created",   "alerts", ["tenant_id", "created_at"])
    op.create_index("ix_alerts_tenant_severity",  "alerts", ["tenant_id", "severity"])

    # ── alert_enrichments ─────────────────────────────────────────────────
    op.create_table(
        "alert_enrichments",
        sa.Column("id",               postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("alert_db_id",      postgresql.UUID(as_uuid=True), nullable=False, unique=True),
        sa.Column("tenant_id",        sa.String(256),  nullable=False),
        sa.Column("triage_decision",  sa.Enum("CRITICAL","NOISE","NEEDS_REVIEW", name="triage_decision"), nullable=False),
        sa.Column("confidence_score", sa.String(8),    nullable=False),
        sa.Column("llm_reasoning",    sa.Text,         nullable=True),
        sa.Column("suggested_action", sa.Text,         nullable=True),
        sa.Column("runbook_refs",     postgresql.JSONB, nullable=True),
        sa.Column("similar_past_alerts", postgresql.JSONB, nullable=True),
        sa.Column("prompt_version",   sa.String(64),   nullable=False),
        sa.Column("raw_llm_response", postgresql.JSONB, nullable=False),
        sa.Column("model_used",       sa.String(128),  nullable=False),
        sa.Column("prompt_tokens",    sa.String(16),   nullable=True),
        sa.Column("completion_tokens",sa.String(16),   nullable=True),
        sa.Column("latency_ms",       sa.String(16),   nullable=True),
        sa.Column("enriched_at",      sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["alert_db_id"], ["alerts.id"], name="fk_enrichment_alert"),
    )
    op.create_index("ix_enrichments_alert_db_id",        "alert_enrichments", ["alert_db_id"])
    op.create_index("ix_enrichments_tenant_id",          "alert_enrichments", ["tenant_id"])
    op.create_index("ix_enrichments_tenant_decision",    "alert_enrichments", ["tenant_id", "triage_decision"])
    op.create_index("ix_enrichments_enriched_at",        "alert_enrichments", ["enriched_at"])

    # ── slack_approvals ───────────────────────────────────────────────────
    op.create_table(
        "slack_approvals",
        sa.Column("id",                   postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("alert_db_id",          postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("enrichment_id",        postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id",            sa.String(256),  nullable=False),
        sa.Column("slack_channel",        sa.String(256),  nullable=False),
        sa.Column("slack_ts",             sa.String(64),   nullable=True),
        sa.Column("slack_message_blocks", postgresql.JSONB, nullable=True),
        sa.Column("status",   sa.Enum("PENDING","ACTIONED","EXPIRED","ERROR", name="approval_status"), nullable=False, server_default="PENDING"),
        sa.Column("action",   sa.Enum("ACKNOWLEDGE","SUPPRESS","ESCALATE","DISMISS", name="approval_action"), nullable=True),
        sa.Column("actioned_by",       sa.String(256), nullable=True),
        sa.Column("actioned_by_name",  sa.String(256), nullable=True),
        sa.Column("action_note",       sa.Text,        nullable=True),
        sa.Column("actioned_at",       sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_at",           sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at",        sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["alert_db_id"],   ["alerts.id"],            name="fk_approval_alert"),
        sa.ForeignKeyConstraint(["enrichment_id"], ["alert_enrichments.id"], name="fk_approval_enrichment"),
    )
    op.create_index("ix_slack_approvals_alert_db_id",     "slack_approvals", ["alert_db_id"])
    op.create_index("ix_slack_approvals_enrichment_id",   "slack_approvals", ["enrichment_id"])
    op.create_index("ix_slack_approvals_tenant_id",       "slack_approvals", ["tenant_id"])
    op.create_index("ix_slack_approvals_tenant_status",   "slack_approvals", ["tenant_id", "status"])
    op.create_index("ix_slack_approvals_slack_ts",        "slack_approvals", ["slack_ts"])

    # ── escalation_events ─────────────────────────────────────────────────
    op.create_table(
        "escalation_events",
        sa.Column("id",                    postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("approval_id",           postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("alert_db_id",           postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id",             sa.String(256),  nullable=False),
        sa.Column("provider",  sa.Enum("PAGERDUTY","OPSGENIE","WEBHOOK", name="escalation_provider"), nullable=False),
        sa.Column("status",    sa.Enum("TRIGGERED","FAILED","SKIPPED",   name="escalation_status"),   nullable=False),
        sa.Column("provider_incident_id",  sa.String(512),  nullable=True),
        sa.Column("provider_incident_url", sa.String(1024), nullable=True),
        sa.Column("request_payload",       postgresql.JSONB, nullable=True),
        sa.Column("response_payload",      postgresql.JSONB, nullable=True),
        sa.Column("error_detail",          sa.Text,         nullable=True),
        sa.Column("triggered_by",          sa.String(256),  nullable=True),
        sa.Column("triggered_at",          sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["approval_id"], ["slack_approvals.id"],   name="fk_escalation_approval"),
        sa.ForeignKeyConstraint(["alert_db_id"], ["alerts.id"],            name="fk_escalation_alert"),
    )
    op.create_index("ix_escalation_approval_id",      "escalation_events", ["approval_id"])
    op.create_index("ix_escalation_alert_db_id",      "escalation_events", ["alert_db_id"])
    op.create_index("ix_escalation_tenant_id",        "escalation_events", ["tenant_id"])
    op.create_index("ix_escalation_tenant_status",    "escalation_events", ["tenant_id", "status"])

    # ── api_keys ──────────────────────────────────────────────────────────
    op.create_table(
        "api_keys",
        sa.Column("id",          postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("key_hash",    sa.String(256),  nullable=False, unique=True),
        sa.Column("tenant_id",   sa.String(256),  nullable=False),
        sa.Column("description", sa.String(512),  nullable=True),
        sa.Column("is_active",   sa.String(1),    nullable=False, server_default="1"),
        sa.Column("created_at",  sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_api_keys_key_hash",   "api_keys", ["key_hash"])
    op.create_index("ix_api_keys_tenant_id",  "api_keys", ["tenant_id"])


def downgrade() -> None:
    op.drop_table("escalation_events")
    op.drop_table("slack_approvals")
    op.drop_table("alert_enrichments")
    op.drop_table("alerts")
    op.drop_table("api_keys")

    for enum_name in (
        "alert_source", "alert_severity", "triage_decision",
        "approval_status", "approval_action",
        "escalation_provider", "escalation_status",
    ):
        op.execute(f"DROP TYPE IF EXISTS {enum_name}")
