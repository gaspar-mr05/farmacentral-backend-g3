"""add production run quantity and availability

Revision ID: 9a4cf1b208c1
Revises: ec5fd84e0d4c
Create Date: 2026-10-01 22:30:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "9a4cf1b208c1"
down_revision: str | Sequence[str] | None = "ec5fd84e0d4c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "production_runs",
        sa.Column(
            "expected_quantity",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
    )
    op.alter_column("production_runs", "expected_quantity", server_default=None)
    op.add_column(
        "production_runs",
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("production_runs", "available_at")
    op.drop_column("production_runs", "expected_quantity")
