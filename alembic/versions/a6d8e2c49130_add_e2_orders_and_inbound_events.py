"""extend orders and add durable inbound events for Delivery 2

Revision ID: a6d8e2c49130
Revises: 7e3a8b91f2c4
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "a6d8e2c49130"
down_revision = "7e3a8b91f2c4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for source in ("SFTP", "PUBSUB", "MESSAGING"):
        op.execute(f"ALTER TYPE ordersource ADD VALUE IF NOT EXISTS '{source}'")
    for status in ("RECEIVED", "ACCEPTED", "REJECTED", "EXPIRED"):
        op.execute(f"ALTER TYPE orderstatus ADD VALUE IF NOT EXISTS '{status}'")

    external_status = postgresql.ENUM(
        "CREATED",
        "ACCEPTED",
        "REJECTED",
        "CANCELLED",
        "EXPIRED",
        "FULFILLED",
        name="externalorderstatus",
        create_type=False,
    )
    event_source = postgresql.ENUM(
        "SFTP",
        "PUBSUB",
        "MESSAGING",
        "RECALL",
        name="inboundeventsource",
        create_type=False,
    )
    event_status = postgresql.ENUM(
        "PENDING",
        "PROCESSING",
        "PROCESSED",
        "FAILED",
        name="inboundeventstatus",
        create_type=False,
    )
    for enum in (external_status, event_source, event_status):
        enum.create(op.get_bind())

    op.alter_column("orders", "buyer_email", existing_type=sa.String(), nullable=True)
    op.add_column("orders", sa.Column("external_order_id", sa.String(24)))
    op.add_column("orders", sa.Column("external_customer_id", sa.String()))
    op.add_column("orders", sa.Column("delivery_deadline", sa.DateTime(timezone=True)))
    op.add_column("orders", sa.Column("external_status", external_status))
    op.create_unique_constraint(
        "uq_orders_external_order_id", "orders", ["external_order_id"]
    )
    op.create_check_constraint(
        "ck_orders_external_order_id",
        "orders",
        "external_order_id IS NULL OR external_order_id ~ '^[0-9a-f]{24}$'",
    )
    op.create_check_constraint(
        "ck_orders_web_buyer_email",
        "orders",
        "source != 'WEB' OR buyer_email IS NOT NULL",
    )
    op.create_index("ix_orders_delivery_deadline", "orders", ["delivery_deadline"])

    op.create_table(
        "inbound_events",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("source", event_source, nullable=False),
        sa.Column("external_event_id", sa.String(), nullable=False),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("processed_at", sa.DateTime(timezone=True)),
        sa.Column(
            "status", event_status, server_default=sa.text("'PENDING'"), nullable=False
        ),
        sa.Column("payload", postgresql.JSONB(none_as_null=True), nullable=False),
        sa.Column("order_id", sa.UUID()),
        sa.Column(
            "attempt_count", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True)),
        sa.Column("next_retry_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.Text()),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"]),
        sa.UniqueConstraint(
            "source", "external_event_id", name="uq_inbound_events_source_external_id"
        ),
        sa.CheckConstraint(
            "length(btrim(external_event_id)) > 0",
            name="ck_inbound_events_external_id_not_empty",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0", name="ck_inbound_events_attempt_count"
        ),
        sa.CheckConstraint(
            "jsonb_typeof(payload) = 'object'", name="ck_inbound_events_payload_object"
        ),
    )
    op.create_index("ix_inbound_events_order_id", "inbound_events", ["order_id"])
    op.create_index(
        "ix_inbound_events_pending",
        "inbound_events",
        ["status", "next_retry_at", "received_at"],
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM inbound_events) OR EXISTS (
                SELECT 1 FROM orders
                WHERE source::text != 'WEB'
                   OR status::text IN ('RECEIVED', 'ACCEPTED', 'REJECTED', 'EXPIRED')
                   OR external_order_id IS NOT NULL
            ) THEN
                RAISE EXCEPTION 'Cannot downgrade while Delivery 2 data exists';
            END IF;
        END $$
        """
    )
    op.drop_index("ix_inbound_events_pending", table_name="inbound_events")
    op.drop_index("ix_inbound_events_order_id", table_name="inbound_events")
    op.drop_table("inbound_events")
    op.drop_index("ix_orders_delivery_deadline", table_name="orders")
    op.drop_constraint("ck_orders_web_buyer_email", "orders", type_="check")
    op.drop_constraint("ck_orders_external_order_id", "orders", type_="check")
    op.drop_constraint("uq_orders_external_order_id", "orders", type_="unique")
    for column in (
        "external_status",
        "delivery_deadline",
        "external_customer_id",
        "external_order_id",
    ):
        op.drop_column("orders", column)
    op.alter_column("orders", "buyer_email", existing_type=sa.String(), nullable=False)
    for name in ("inboundeventstatus", "inboundeventsource", "externalorderstatus"):
        postgresql.ENUM(name=name).drop(op.get_bind())
