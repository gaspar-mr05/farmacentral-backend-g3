"""add order unit assignments

Revision ID: c594e8f26c4a
Revises: b73b09fe5c21
Create Date: 2026-10-02 16:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c594e8f26c4a"
down_revision: str | Sequence[str] | None = "b73b09fe5c21"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "order_units",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("order_item_id", sa.UUID(), nullable=False),
        sa.Column("unit_id", sa.UUID(), nullable=False),
        sa.Column(
            "assigned_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["order_item_id"], ["order_items.id"]),
        sa.ForeignKeyConstraint(["unit_id"], ["units.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("unit_id", name="uq_order_units_unit_id"),
    )
    op.create_index(
        op.f("ix_order_units_order_item_id"),
        "order_units",
        ["order_item_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_order_units_order_item_id"), table_name="order_units")
    op.drop_table("order_units")
