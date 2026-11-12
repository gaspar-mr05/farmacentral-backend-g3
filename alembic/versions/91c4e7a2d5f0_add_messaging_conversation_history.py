"""Add scoped messaging conversation history.

Revision ID: 91c4e7a2d5f0
Revises: f69c14e350a3
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "91c4e7a2d5f0"
down_revision = "f69c14e350a3"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TYPE messagingdecision ADD VALUE IF NOT EXISTS 'INQUIRY'")
    op.add_column(
        "inbound_events",
        sa.Column("classified_at", sa.DateTime(timezone=True)),
    )
    op.add_column(
        "inbound_events",
        sa.Column("consulted_external_order_id", sa.String(length=24)),
    )
    op.add_column(
        "inbound_events", sa.Column("resolution_evidence", postgresql.JSONB())
    )
    op.add_column(
        "inbound_events", sa.Column("order_status_snapshot", postgresql.JSONB())
    )

    direction = postgresql.ENUM("INBOUND", "OUTBOUND", name="messagedirection")
    direction.create(op.get_bind())
    op.create_table(
        "messaging_messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "inbound_event_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("inbound_events.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "direction",
            postgresql.ENUM(name="messagedirection", create_type=False),
            nullable=False,
        ),
        sa.Column("external_message_id", sa.String()),
        sa.Column("phone_normalized", sa.String(length=16), nullable=False),
        sa.Column("sender", postgresql.JSONB()),
        sa.Column("text", sa.Text()),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("group_id", sa.Integer(), nullable=False),
        sa.Column("environment", sa.String(), nullable=False),
        sa.Column(
            "period_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("business_periods.id"),
        ),
        sa.UniqueConstraint(
            "direction",
            "external_message_id",
            name="uq_messaging_message_external_id",
        ),
        sa.CheckConstraint(
            "length(btrim(phone_normalized)) > 0",
            name="ck_messaging_message_phone",
        ),
        sa.CheckConstraint("group_id > 0", name="ck_messaging_message_group"),
        sa.CheckConstraint(
            "length(btrim(environment)) > 0",
            name="ck_messaging_message_environment",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(payload) = 'object'",
            name="ck_messaging_message_payload",
        ),
    )
    op.create_index(
        "ix_messaging_messages_inbound_event_id",
        "messaging_messages",
        ["inbound_event_id"],
    )
    op.create_index(
        "ix_messaging_history_scope",
        "messaging_messages",
        ["group_id", "environment", "period_id", "phone_normalized", "occurred_at"],
    )
    op.create_index(
        "ix_messaging_phone_time",
        "messaging_messages",
        ["phone_normalized", "occurred_at"],
    )


def downgrade():
    connection = op.get_bind()
    if connection.scalar(
        sa.text("SELECT EXISTS (SELECT 1 FROM inbound_events WHERE decision='INQUIRY')")
    ):
        raise AssertionError("Cannot downgrade while inquiry decisions exist")

    op.drop_table("messaging_messages")
    for column in (
        "order_status_snapshot",
        "resolution_evidence",
        "consulted_external_order_id",
        "classified_at",
    ):
        op.drop_column("inbound_events", column)
    postgresql.ENUM(name="messagedirection").drop(connection)

    op.execute(
        "ALTER TABLE inbound_events ALTER COLUMN decision TYPE text "
        "USING decision::text"
    )
    op.execute("DROP TYPE messagingdecision")
    postgresql.ENUM("ORDER", "DISCARDED", name="messagingdecision").create(connection)
    op.execute(
        "ALTER TABLE inbound_events ALTER COLUMN decision TYPE messagingdecision "
        "USING decision::messagingdecision"
    )
