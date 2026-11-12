"""Persist per-kit scheduling, sourcing waits and production correlation.

Revision ID: f69c14e350a3
Revises: e58b03d249f2
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "f69c14e350a3"
down_revision = "e58b03d249f2"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("procurement_requests", sa.Column("http_status", sa.Integer()))
    op.add_column(
        "production_runs", sa.Column("operation_key", postgresql.UUID(as_uuid=True))
    )
    op.create_unique_constraint(
        "uq_production_operation_key", "production_runs", ["operation_key"]
    )
    op.create_table(
        "replenishment_states",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "period_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("business_periods.id"),
            nullable=False,
        ),
        sa.Column("sku", sa.String(), nullable=False),
        sa.Column("minimum", sa.Integer(), nullable=False),
        sa.Column("target", sa.Integer(), nullable=False),
        sa.Column("goal", postgresql.JSONB()),
        sa.Column("pending_operation", postgresql.JSONB()),
        sa.Column(
            "procurement_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("procurement_requests.id"),
        ),
        sa.Column("estimated_spend", sa.Numeric(), nullable=False, server_default="0"),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True)),
        sa.Column("next_retry_at", sa.DateTime(timezone=True)),
        sa.Column("http_status", sa.Integer()),
        sa.Column("last_error", sa.String()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("period_id", "sku", name="uq_replenishment_sku"),
    )
    op.create_index(
        "ix_replenishment_states_next_retry_at",
        "replenishment_states",
        ["next_retry_at"],
    )


def downgrade():
    op.drop_column("procurement_requests", "http_status")
    op.drop_table("replenishment_states")
    op.drop_constraint("uq_production_operation_key", "production_runs", type_="unique")
    op.drop_column("production_runs", "operation_key")
