"""Add business metric snapshots.

Revision ID: 4f2a7c91e6d3
Revises: d83f92a16b40
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "4f2a7c91e6d3"
down_revision = "d83f92a16b40"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "business_metric_snapshots",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("period_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "captured_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("commit_sha", sa.String(length=64), nullable=False),
        sa.Column(
            "is_closing",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column("metrics", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.CheckConstraint(
            "length(btrim(commit_sha)) > 0", name="ck_metric_snapshot_commit"
        ),
        sa.CheckConstraint(
            "jsonb_typeof(metrics) = 'object'", name="ck_metric_snapshot_metrics"
        ),
        sa.ForeignKeyConstraint(["period_id"], ["business_periods.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_metric_snapshots_period_time",
        "business_metric_snapshots",
        ["period_id", "captured_at"],
    )


def downgrade() -> None:
    if op.get_bind().scalar(sa.text("SELECT count(*) FROM business_metric_snapshots")):
        raise AssertionError("Business metric snapshots must be preserved")
    op.drop_index(
        "ix_metric_snapshots_period_time", table_name="business_metric_snapshots"
    )
    op.drop_table("business_metric_snapshots")
