"""Add durable worker supervision evidence.

Revision ID: 2b1f6a7c8d90
Revises: b8c2d19f460a
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "2b1f6a7c8d90"
down_revision = "b8c2d19f460a"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "worker_heartbeats",
        sa.Column("period_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("worker_name", sa.String(), nullable=False),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_success_at", sa.DateTime(timezone=True)),
        sa.Column("last_failure_at", sa.DateTime(timezone=True)),
        sa.Column("last_error_type", sa.String()),
        sa.Column(
            "consecutive_failures", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column("queue_depth", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("oldest_queued_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(["period_id"], ["business_periods.id"]),
        sa.PrimaryKeyConstraint("period_id", "worker_name"),
    )


def downgrade():
    op.drop_table("worker_heartbeats")
