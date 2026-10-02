"""add sellable flag to locations

Revision ID: 4293e9d4776a
Revises: 9a4cf1b208c1
Create Date: 2026-10-02 00:30:58.474608
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "4293e9d4776a"
down_revision: str | Sequence[str] | None = "9a4cf1b208c1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "locations",
        sa.Column(
            "is_sellable",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
    )

    op.execute(
        sa.text(
            """
            UPDATE locations
            SET is_sellable = false
            WHERE name LIKE '%Recepción%'
               OR name LIKE '%Despacho%'
               OR name LIKE '%Acondicionamiento%'
               OR name LIKE '%Cuarentena%'
            """
        )
    )

    op.alter_column(
        "locations",
        "is_sellable",
        server_default=None,
    )


def downgrade() -> None:
    op.drop_column("locations", "is_sellable")
