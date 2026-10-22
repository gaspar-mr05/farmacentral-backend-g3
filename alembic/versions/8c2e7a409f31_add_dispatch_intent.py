"""Persist the expected official count before dispatching a unit.

Revision ID: 8c2e7a409f31
Revises: f65d4ad17b2e
"""

import sqlalchemy as sa

from alembic import op

revision = "8c2e7a409f31"
down_revision = "f65d4ad17b2e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "order_units",
        sa.Column("dispatch_expected_quantity", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("order_units", "dispatch_expected_quantity")
