"""add messaging audit fields

Revision ID: 3f7c2d91a8b4
Revises: 8c2e7a409f31
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "3f7c2d91a8b4"
down_revision = "8c2e7a409f31"
branch_labels = None
depends_on = None


def upgrade() -> None:
    messaging_decision = postgresql.ENUM("ORDER", "DISCARDED", name="messagingdecision")
    messaging_decision.create(op.get_bind())

    op.add_column("inbound_events", sa.Column("llm_output", postgresql.JSONB()))
    op.add_column("inbound_events", sa.Column("interpreted_sku", sa.String()))
    op.add_column("inbound_events", sa.Column("interpreted_quantity", sa.Integer()))
    op.add_column(
        "inbound_events",
        sa.Column("interpreted_delivery_date", sa.DateTime(timezone=True)),
    )
    op.add_column("inbound_events", sa.Column("decision", messaging_decision))
    op.add_column("inbound_events", sa.Column("reason", sa.Text()))
    op.add_column("inbound_events", sa.Column("external_order_id", sa.String(24)))
    op.add_column(
        "inbound_events", sa.Column("responded_at", sa.DateTime(timezone=True))
    )
    op.create_check_constraint(
        "ck_inbound_events_interpreted_quantity",
        "inbound_events",
        "interpreted_quantity IS NULL OR interpreted_quantity > 0",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_inbound_events_interpreted_quantity", "inbound_events", type_="check"
    )
    for column in (
        "responded_at",
        "external_order_id",
        "reason",
        "decision",
        "interpreted_delivery_date",
        "interpreted_quantity",
        "interpreted_sku",
        "llm_output",
    ):
        op.drop_column("inbound_events", column)
    postgresql.ENUM(name="messagingdecision").drop(op.get_bind())
