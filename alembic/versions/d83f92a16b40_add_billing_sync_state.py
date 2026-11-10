"""Add durable billing reconciliation progress.

Revision ID: d83f92a16b40
Revises: c7b4e2a91d60
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "d83f92a16b40"
down_revision = "c7b4e2a91d60"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "billing_sync_states",
        sa.Column("period_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("stream", sa.String(), nullable=False),
        *[
            sa.Column(name, sa.DateTime(timezone=True), nullable=True)
            for name in (
                "cursor",
                "full_cursor",
                "full_target",
                "next_full_at",
                "pending_cursor",
                "last_attempt_at",
                "last_success_at",
                "next_retry_at",
            )
        ],
        sa.Column("last_error", sa.String(), nullable=True),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("balance", sa.Numeric(), nullable=True),
        sa.ForeignKeyConstraint(["period_id"], ["business_periods.id"]),
        sa.PrimaryKeyConstraint("period_id", "stream"),
    )


def downgrade() -> None:
    if op.get_bind().scalar(sa.text("SELECT count(*) FROM billing_sync_states")):
        raise AssertionError(
            "Billing reconciliation progress exists; preserve it before downgrade"
        )
    op.drop_table("billing_sync_states")
