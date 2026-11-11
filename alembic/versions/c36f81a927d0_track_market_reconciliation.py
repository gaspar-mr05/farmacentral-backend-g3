"""Track durable market reconciliation and retries.

Revision ID: c36f81a927d0
Revises: b24d6e830f19
"""

import sqlalchemy as sa

from alembic import op

revision = "c36f81a927d0"
down_revision = "b24d6e830f19"
branch_labels = None
depends_on = None


def upgrade():
    for name in ("last_checked_at", "next_retry_at", "reconciled_at"):
        op.add_column("market_offers", sa.Column(name, sa.DateTime(timezone=True)))
    op.add_column(
        "market_offers",
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("market_offers", sa.Column("last_error", sa.String()))
    op.add_column("market_offers", sa.Column("http_status", sa.Integer()))
    op.create_index(
        "ix_market_offers_next_retry_at", "market_offers", ["next_retry_at"]
    )


def downgrade():
    op.drop_index("ix_market_offers_next_retry_at", table_name="market_offers")
    for name in (
        "reconciled_at",
        "http_status",
        "last_error",
        "attempt_count",
        "next_retry_at",
        "last_checked_at",
    ):
        op.drop_column("market_offers", name)
