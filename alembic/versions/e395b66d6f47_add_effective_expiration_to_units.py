"""add effective expiration to units

Revision ID: e395b66d6f47
Revises: 6b036fdee2c1
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "e395b66d6f47"
down_revision: str | Sequence[str] | None = "6b036fdee2c1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "units",
        sa.Column(
            "effective_expires_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )

    op.execute(
        """
        UPDATE units
        SET effective_expires_at = lots.expires_at
        FROM lots
        WHERE units.lot_id = lots.id
        """
    )

    op.alter_column(
        "units",
        "effective_expires_at",
        existing_type=sa.DateTime(timezone=True),
        nullable=False,
    )


def downgrade() -> None:
    op.drop_column("units", "effective_expires_at")
