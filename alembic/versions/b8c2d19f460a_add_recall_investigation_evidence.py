"""Store recall ownership checks and market delivery snapshots.

Revision ID: b8c2d19f460a
Revises: 91c4e7a2d5f0
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "b8c2d19f460a"
down_revision = "91c4e7a2d5f0"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "recalls",
        sa.Column("investigation_evidence", postgresql.JSONB(none_as_null=True)),
    )


def downgrade():
    op.drop_column("recalls", "investigation_evidence")
