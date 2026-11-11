"""Audit surplus pricing, protection and original stock locations.

Revision ID: e58b03d249f2
Revises: d47a92c138e1
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "e58b03d249f2"
down_revision = "d47a92c138e1"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("market_offers", sa.Column("strategy", sa.String()))
    op.add_column("market_offers", sa.Column("estimated_unit_cost", sa.Numeric()))
    op.add_column("market_offers", sa.Column("protected_quantity", sa.Integer()))
    op.add_column(
        "market_offer_reservations",
        sa.Column(
            "source_location_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("locations.id"),
        ),
    )


def downgrade():
    op.drop_column("market_offer_reservations", "source_location_id")
    for name in ("protected_quantity", "estimated_unit_cost", "strategy"):
        op.drop_column("market_offers", name)
