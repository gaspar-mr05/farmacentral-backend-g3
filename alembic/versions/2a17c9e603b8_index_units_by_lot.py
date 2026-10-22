"""Index units by lot for recall investigation.

Revision ID: 2a17c9e603b8
Revises: 4d8a1c7e9b20
"""

from alembic import op

revision = "2a17c9e603b8"
down_revision = "4d8a1c7e9b20"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_units_lot_id", "units", ["lot_id"])


def downgrade() -> None:
    op.drop_index("ix_units_lot_id", table_name="units")
