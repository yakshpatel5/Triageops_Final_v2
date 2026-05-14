"""Add dashboard query indexes.

Revision ID: 0002_dashboard_indexes
Revises: 0001_initial_schema
Create Date: 2025-01-22 09:00:00.000000

Adds partial and composite indexes optimising the /ops/stats and
/ops/alerts queries introduced in Week 5. Profiling showed seq-scans
on large tenants without these.
"""

from __future__ import annotations

from alembic import op

revision = "0002_dashboard_indexes"
down_revision = "0001_initial_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Partial index: only PENDING approvals — expire cron scans this heavily
    op.create_index(
        "ix_slack_approvals_pending_expires",
        "slack_approvals",
        ["expires_at"],
        postgresql_where="status = 'PENDING'",
    )

    # Partial index: only TRIGGERED escalations — dedup check in cron
    op.create_index(
        "ix_escalation_triggered_alert",
        "escalation_events",
        ["alert_db_id"],
        postgresql_where="status = 'TRIGGERED'",
    )

    # Covering index for stats/distribution query (tenant + decision + received_at join)
    op.create_index(
        "ix_alerts_tenant_received",
        "alerts",
        ["tenant_id", "received_at"],
    )

    # Covering index for enrichment confidence/latency avg (stats query)
    op.create_index(
        "ix_enrichments_alert_db_id_scores",
        "alert_enrichments",
        ["alert_db_id", "confidence_score", "latency_ms"],
    )


def downgrade() -> None:
    op.drop_index("ix_slack_approvals_pending_expires", "slack_approvals")
    op.drop_index("ix_escalation_triggered_alert",     "escalation_events")
    op.drop_index("ix_alerts_tenant_received",         "alerts")
    op.drop_index("ix_enrichments_alert_db_id_scores", "alert_enrichments")
