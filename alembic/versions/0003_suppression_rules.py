"""Add suppression_rules table and is_suppressed flag on alerts.

Revision ID: 0003_suppression_rules
Revises: 0002_dashboard_indexes
Create Date: 2025-02-01 09:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from alembic import op

revision = "0003_suppression_rules"
down_revision = "0002_dashboard_indexes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # New enum types
    for name, values in [
        ("match_field",   ["HOST", "ALERT_ID", "MESSAGE", "HOST_MESSAGE"]),
        ("pattern_type",  ["EXACT", "PREFIX", "CONTAINS", "REGEX"]),
    ]:
        postgresql.ENUM(*values, name=name, create_type=False).create(
            op.get_bind(), checkfirst=True
        )

    # suppression_rules table
    op.create_table(
        "suppression_rules",
        sa.Column("id",           postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id",    sa.String(256),  nullable=False),
        sa.Column("match_field",  sa.Enum("HOST","ALERT_ID","MESSAGE","HOST_MESSAGE",
                                          name="match_field"), nullable=False),
        sa.Column("pattern_type", sa.Enum("EXACT","PREFIX","CONTAINS","REGEX",
                                          name="pattern_type"), nullable=False,
                  server_default="EXACT"),
        sa.Column("host_pattern",    sa.String(512),  nullable=True),
        sa.Column("message_pattern", sa.String(1024), nullable=True),
        sa.Column("alert_id_prefix", sa.String(512),  nullable=True),
        sa.Column("reason",          sa.Text,         nullable=True),
        sa.Column("is_active",       sa.String(1),    nullable=False, server_default="1"),
        sa.Column("hit_count",       sa.String(16),   nullable=False, server_default="0"),
        sa.Column("last_hit_at",     sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_approval_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_by",      sa.String(256),  nullable=True),
        sa.Column("created_at",      sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at",      sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_suppression_tenant_active",      "suppression_rules", ["tenant_id", "is_active"])
    op.create_index("ix_suppression_source_approval",    "suppression_rules", ["source_approval_id"])
    op.create_index("ix_suppression_tenant_match_field", "suppression_rules", ["tenant_id", "match_field"])

    # Add is_suppressed flag to alerts — default "0", backfill not needed
    op.add_column(
        "alerts",
        sa.Column("is_suppressed", sa.String(1), nullable=False, server_default="0"),
    )
    op.create_index(
        "ix_alerts_tenant_suppressed",
        "alerts",
        ["tenant_id", "is_suppressed"],
    )


def downgrade() -> None:
    op.drop_index("ix_alerts_tenant_suppressed", "alerts")
    op.drop_column("alerts", "is_suppressed")

    op.drop_table("suppression_rules")

    for t in ("match_field", "pattern_type"):
        op.execute(f"DROP TYPE IF EXISTS {t}")
