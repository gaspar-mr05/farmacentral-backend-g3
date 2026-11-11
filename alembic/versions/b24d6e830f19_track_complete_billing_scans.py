"""Track actual full billing scans rather than individual successful queries."""

import sqlalchemy as sa

from alembic import op

revision = "b24d6e830f19"
down_revision = "a91f0c2d7e34"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "billing_sync_states", sa.Column("full_started_at", sa.DateTime(timezone=True))
    )
    op.add_column(
        "billing_sync_states",
        sa.Column("full_completed_at", sa.DateTime(timezone=True)),
    )


def downgrade():
    op.drop_column("billing_sync_states", "full_completed_at")
    op.drop_column("billing_sync_states", "full_started_at")
