"""add order decision audit

Revision ID: f65d4ad17b2e
Revises: a6d8e2c49130
"""

import sqlalchemy as sa

from alembic import op

revision = "f65d4ad17b2e"
down_revision = "a6d8e2c49130"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("orders", sa.Column("decision_at", sa.DateTime(timezone=True)))
    op.add_column("orders", sa.Column("decision_reason", sa.Text()))


def downgrade() -> None:
    op.drop_column("orders", "decision_reason")
    op.drop_column("orders", "decision_at")
