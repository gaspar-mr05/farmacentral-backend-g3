"""Persist scarcity procurement decisions and pending market operations.

Revision ID: d47a92c138e1
Revises: c36f81a927d0
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "d47a92c138e1"
down_revision = "c36f81a927d0"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "procurement_requests",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "period_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("business_periods.id"),
            nullable=False,
        ),
        sa.Column("request_key", sa.String(), nullable=False),
        sa.Column("demand", postgresql.JSONB(), nullable=False),
        sa.Column("decision", postgresql.JSONB()),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column(
            "market_offer_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("market_offers.id"),
        ),
        sa.Column("next_retry_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.String()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("period_id", "request_key", name="uq_procurement_request"),
    )
    op.create_index(
        "ix_procurement_requests_next_retry_at",
        "procurement_requests",
        ["next_retry_at"],
    )


def downgrade():
    op.drop_table("procurement_requests")
