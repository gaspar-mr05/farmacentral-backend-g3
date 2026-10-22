"""add persistent recall model

Revision ID: 4d8a1c7e9b20
Revises: 3f7c2d91a8b4
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "4d8a1c7e9b20"
down_revision = "3f7c2d91a8b4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    recall_status = postgresql.ENUM(
        "RECEIVED",
        "INVESTIGATING",
        "RESPONDING",
        "COMPLETED",
        "FAILED",
        name="recallstatus",
        create_type=False,
    )
    notification_status = postgresql.ENUM(
        "PENDING",
        "SENT",
        "FAILED",
        name="recallnotificationstatus",
        create_type=False,
    )
    recall_status.create(op.get_bind())
    notification_status.create(op.get_bind())

    op.create_unique_constraint("uq_units_id_lot_id", "units", ["id", "lot_id"])
    op.create_unique_constraint(
        "uq_order_units_id_unit_id", "order_units", ["id", "unit_id"]
    )

    op.create_table(
        "recalls",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("recall_id", sa.String(), nullable=False),
        sa.Column("inbound_event_id", sa.UUID()),
        sa.Column("origin_lot_id", sa.UUID()),
        sa.Column("batch", sa.String(), nullable=False),
        sa.Column("sku", sa.String(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deadline", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "status",
            recall_status,
            server_default=sa.text("'RECEIVED'"),
            nullable=False,
        ),
        sa.Column("reported_at", sa.DateTime(timezone=True)),
        sa.Column("last_report", postgresql.JSONB(none_as_null=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["inbound_event_id"], ["inbound_events.id"]),
        sa.ForeignKeyConstraint(["origin_lot_id"], ["lots.id"]),
        sa.UniqueConstraint("recall_id"),
        sa.UniqueConstraint("inbound_event_id"),
        sa.CheckConstraint(
            "length(btrim(recall_id)) > 0", name="ck_recalls_recall_id_not_empty"
        ),
        sa.CheckConstraint(
            "length(btrim(batch)) > 0", name="ck_recalls_batch_not_empty"
        ),
        sa.CheckConstraint("length(btrim(sku)) > 0", name="ck_recalls_sku_not_empty"),
        sa.CheckConstraint(
            "length(btrim(reason)) > 0", name="ck_recalls_reason_not_empty"
        ),
        sa.CheckConstraint("deadline > issued_at", name="ck_recalls_deadline"),
        sa.CheckConstraint(
            "last_report IS NULL OR jsonb_typeof(last_report) = 'object'",
            name="ck_recalls_last_report_object",
        ),
        sa.CheckConstraint(
            "(reported_at IS NULL AND last_report IS NULL) OR "
            "(reported_at IS NOT NULL AND last_report IS NOT NULL)",
            name="ck_recalls_report_audit",
        ),
    )
    op.create_index("ix_recalls_origin_lot_id", "recalls", ["origin_lot_id"])
    op.create_index("ix_recalls_deadline", "recalls", ["deadline"])

    op.create_table(
        "recall_lots",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("recall_id", sa.UUID(), nullable=False),
        sa.Column("lot_id", sa.UUID(), nullable=False),
        sa.Column("parent_id", sa.UUID()),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column(
            "identified_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["recall_id"], ["recalls.id"]),
        sa.ForeignKeyConstraint(["lot_id"], ["lots.id"]),
        sa.UniqueConstraint("recall_id", "lot_id", name="uq_recall_lots_recall_lot"),
        sa.UniqueConstraint("id", "recall_id", name="uq_recall_lots_id_recall"),
        sa.CheckConstraint("generation >= 0", name="ck_recall_lots_generation"),
        sa.CheckConstraint(
            "(generation = 0 AND parent_id IS NULL) OR "
            "(generation > 0 AND parent_id IS NOT NULL)",
            name="ck_recall_lots_parent_generation",
        ),
    )
    op.create_foreign_key(
        "fk_recall_lots_parent_same_recall",
        "recall_lots",
        "recall_lots",
        ["parent_id", "recall_id"],
        ["id", "recall_id"],
    )
    op.create_index(
        "ix_recall_lots_recall_generation",
        "recall_lots",
        ["recall_id", "generation"],
    )

    op.create_table(
        "recall_units",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("recall_id", sa.UUID(), nullable=False),
        sa.Column("lot_id", sa.UUID(), nullable=False),
        sa.Column("unit_id", sa.UUID(), nullable=False),
        sa.Column("original_location_id", sa.UUID(), nullable=False),
        sa.Column(
            "identified_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("quarantined_at", sa.DateTime(timezone=True)),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["recall_id"], ["recalls.id"]),
        sa.ForeignKeyConstraint(["original_location_id"], ["locations.id"]),
        sa.ForeignKeyConstraint(
            ["recall_id", "lot_id"],
            ["recall_lots.recall_id", "recall_lots.lot_id"],
            name="fk_recall_units_affected_lot",
        ),
        sa.ForeignKeyConstraint(
            ["unit_id", "lot_id"],
            ["units.id", "units.lot_id"],
            name="fk_recall_units_unit_lot",
        ),
        sa.UniqueConstraint("recall_id", "unit_id", name="uq_recall_units_recall_unit"),
    )

    op.create_table(
        "recall_orders",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("recall_id", sa.UUID(), nullable=False),
        sa.Column("order_id", sa.UUID(), nullable=False),
        sa.Column("customer_reference", sa.String(), nullable=False),
        sa.Column(
            "identified_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["recall_id"], ["recalls.id"]),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"]),
        sa.UniqueConstraint(
            "recall_id", "order_id", name="uq_recall_orders_recall_order"
        ),
        sa.UniqueConstraint("id", "recall_id", name="uq_recall_orders_id_recall"),
        sa.CheckConstraint(
            "length(btrim(customer_reference)) > 0",
            name="ck_recall_orders_customer_reference_not_empty",
        ),
    )

    op.create_table(
        "recall_deliveries",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("recall_id", sa.UUID(), nullable=False),
        sa.Column("recall_order_id", sa.UUID(), nullable=False),
        sa.Column("lot_id", sa.UUID(), nullable=False),
        sa.Column("unit_id", sa.UUID(), nullable=False),
        sa.Column("order_unit_id", sa.UUID(), nullable=False),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["recall_id"], ["recalls.id"]),
        sa.ForeignKeyConstraint(
            ["recall_order_id", "recall_id"],
            ["recall_orders.id", "recall_orders.recall_id"],
            name="fk_recall_deliveries_order_same_recall",
        ),
        sa.ForeignKeyConstraint(
            ["recall_id", "lot_id"],
            ["recall_lots.recall_id", "recall_lots.lot_id"],
            name="fk_recall_deliveries_affected_lot",
        ),
        sa.ForeignKeyConstraint(
            ["unit_id", "lot_id"],
            ["units.id", "units.lot_id"],
            name="fk_recall_deliveries_unit_lot",
        ),
        sa.ForeignKeyConstraint(
            ["order_unit_id", "unit_id"],
            ["order_units.id", "order_units.unit_id"],
            name="fk_recall_deliveries_order_unit",
        ),
        sa.UniqueConstraint(
            "recall_id", "order_unit_id", name="uq_recall_deliveries_assignment"
        ),
        sa.UniqueConstraint("recall_id", "unit_id", name="uq_recall_deliveries_unit"),
    )

    op.create_table(
        "recall_notifications",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("recall_order_id", sa.UUID(), nullable=False),
        sa.Column(
            "status",
            notification_status,
            server_default=sa.text("'PENDING'"),
            nullable=False,
        ),
        sa.Column("payload", postgresql.JSONB(none_as_null=True), nullable=False),
        sa.Column("response", postgresql.JSONB(none_as_null=True)),
        sa.Column(
            "attempt_count", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column("sent_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.Text()),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["recall_order_id"], ["recall_orders.id"]),
        sa.UniqueConstraint("recall_order_id"),
        sa.CheckConstraint(
            "jsonb_typeof(payload) = 'object'",
            name="ck_recall_notifications_payload_object",
        ),
        sa.CheckConstraint(
            "response IS NULL OR jsonb_typeof(response) = 'object'",
            name="ck_recall_notifications_response_object",
        ),
        sa.CheckConstraint(
            "(status = 'SENT' AND sent_at IS NOT NULL) OR "
            "(status != 'SENT' AND sent_at IS NULL)",
            name="ck_recall_notifications_sent_at",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0", name="ck_recall_notifications_attempt_count"
        ),
    )


def downgrade() -> None:
    for table in (
        "recall_notifications",
        "recall_deliveries",
        "recall_units",
        "recall_orders",
        "recall_lots",
        "recalls",
    ):
        op.drop_table(table)
    op.drop_constraint("uq_order_units_id_unit_id", "order_units", type_="unique")
    op.drop_constraint("uq_units_id_lot_id", "units", type_="unique")
    postgresql.ENUM(name="recallnotificationstatus").drop(op.get_bind())
    postgresql.ENUM(name="recallstatus").drop(op.get_bind())
