"""restore own-production lot origins

Revision ID: 7e3a8b91f2c4
Revises: d2f190a6c321
"""

from alembic import op

revision = "7e3a8b91f2c4"
down_revision = "d2f190a6c321"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE lots
        SET origin = 'OWN_PRODUCTION'
        WHERE id IN (
            SELECT output_lot_id
            FROM production_runs
            WHERE output_lot_id IS NOT NULL
        )
        """
    )


def downgrade() -> None:
    # The previous origin cannot be reconstructed safely, so keep the corrected
    # value when downgrading the application schema.
    pass
