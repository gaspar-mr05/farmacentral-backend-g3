"""add order dispatch

Revision ID: d2f190a6c321
Revises: c594e8f26c4a
"""

import sqlalchemy as sa

from alembic import op

revision = "d2f190a6c321"
down_revision = "c594e8f26c4a"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TYPE orderstatus ADD VALUE IF NOT EXISTS 'DISPATCHED'")
    op.execute("ALTER TYPE custodyeventtype ADD VALUE IF NOT EXISTS 'DISPATCHED'")
    op.add_column("order_units", sa.Column("dispatched_at", sa.DateTime(timezone=True)))


def downgrade() -> None:
    # Enum labels remain in PostgreSQL; removing labels requires recreating types.
    op.execute("UPDATE orders SET status = 'PAID' WHERE status::text = 'DISPATCHED'")
    op.execute("DELETE FROM custody_events WHERE event_type::text = 'DISPATCHED'")
    op.execute("UPDATE units SET status = 'reserved' WHERE status = 'dispatched'")
    op.drop_column("order_units", "dispatched_at")
