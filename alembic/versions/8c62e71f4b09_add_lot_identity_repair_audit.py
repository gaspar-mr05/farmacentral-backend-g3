"""Audit recovery of provisional lot identities from official inventory.

Revision ID: 8c62e71f4b09
Revises: 2a17c9e603b8
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "8c62e71f4b09"
down_revision = "2a17c9e603b8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "lot_identity_repairs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("unit_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("previous_batch", sa.String(), nullable=False),
        sa.Column("official_batch", sa.String(), nullable=False),
        sa.Column(
            "repaired_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["unit_id"], ["units.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "unit_id", "official_batch", name="uq_lot_identity_repairs_unit_batch"
        ),
    )


def downgrade() -> None:
    if op.get_bind().scalar(sa.text("SELECT count(*) FROM lot_identity_repairs")):
        raise RuntimeError("Cannot drop lot identity repair audit while records exist")
    op.drop_table("lot_identity_repairs")
