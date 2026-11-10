"""Add E3 accounting scope, durable billing operations and market references.

Revision ID: c7b4e2a91d60
Revises: 8c62e71f4b09
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "c7b4e2a91d60"
down_revision = "8c62e71f4b09"
branch_labels = None
depends_on = None

# Freeze types here; historical migrations must not import application models.
ENUMS = {
    "marketrole": postgresql.ENUM(
        "BUYER", "SELLER", name="marketrole", create_type=False
    ),
    "invoiceside": postgresql.ENUM(
        "CLIENT", "SUPPLIER", name="invoiceside", create_type=False
    ),
    "invoicestatus": postgresql.ENUM(
        "PENDING", "PAID", name="invoicestatus", create_type=False
    ),
    "billingtaskkind": postgresql.ENUM(
        "EMIT_INVOICE", "PAY_INVOICE", name="billingtaskkind", create_type=False
    ),
    "billingoperationstatus": postgresql.ENUM(
        "READY",
        "IN_FLIGHT",
        "CONFIRMED",
        "UNCERTAIN",
        "FAILED",
        name="billingoperationstatus",
        create_type=False,
    ),
}


def upgrade() -> None:
    for enum in ENUMS.values():
        enum.create(op.get_bind())
    op.execute("ALTER TYPE ordersource ADD VALUE 'MARKET'")
    op.create_table(
        "business_periods",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("environment", sa.String(), nullable=False),
        sa.Column("code", sa.String(), nullable=False),
        sa.Column("group_id", sa.Integer(), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "ends_at IS NULL OR ends_at > starts_at", name="ck_period_dates"
        ),
        sa.CheckConstraint("group_id > 0", name="ck_period_group"),
        sa.CheckConstraint("length(btrim(code)) > 0", name="ck_period_code"),
        sa.CheckConstraint(
            "length(btrim(environment)) > 0", name="ck_period_environment"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "environment", "code", "group_id", name="uq_business_period"
        ),
    )
    op.create_table(
        "market_trades",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("period_id", sa.UUID(), nullable=False),
        sa.Column("external_offer_id", sa.String(), nullable=False),
        sa.Column("external_order_id", sa.String(), nullable=False),
        sa.Column("role", ENUMS["marketrole"], nullable=False),
        sa.Column("counterparty_id", sa.String(), nullable=False),
        sa.Column("sku", sa.String(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("unit_price", sa.Numeric(), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sale_order_id", sa.UUID(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "role = 'SELLER' OR sale_order_id IS NULL", name="ck_trade_sale_role"
        ),
        sa.CheckConstraint(
            "unit_price >= 0 AND unit_price < 'Infinity'::numeric",
            name="ck_trade_price",
        ),
        sa.CheckConstraint(
            "length(btrim(counterparty_id)) > 0", name="ck_trade_counterparty"
        ),
        sa.CheckConstraint(
            "length(btrim(external_offer_id)) > 0", name="ck_trade_offer"
        ),
        sa.CheckConstraint(
            "length(btrim(external_order_id)) > 0", name="ck_trade_order"
        ),
        sa.CheckConstraint("length(btrim(sku)) > 0", name="ck_trade_sku"),
        sa.CheckConstraint("quantity > 0", name="ck_trade_quantity"),
        sa.ForeignKeyConstraint(
            ["period_id"],
            ["business_periods.id"],
        ),
        sa.ForeignKeyConstraint(
            ["sale_order_id"],
            ["orders.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "period_id", name="uq_market_trades_id_period"),
        sa.UniqueConstraint(
            "period_id", "external_offer_id", name="uq_market_trade_offer"
        ),
        sa.UniqueConstraint(
            "period_id", "external_order_id", name="uq_market_trade_order"
        ),
        sa.UniqueConstraint("period_id", "sale_order_id", name="uq_market_trade_sale"),
    )
    op.create_table(
        "invoices",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("period_id", sa.UUID(), nullable=False),
        sa.Column("external_invoice_id", sa.String(), nullable=False),
        sa.Column("side", ENUMS["invoiceside"], nullable=False),
        sa.Column("counterparty_id", sa.String(), nullable=False),
        sa.Column("channel", sa.String(), nullable=False),
        sa.Column("status", ENUMS["invoicestatus"], nullable=False),
        sa.Column("principal", sa.Numeric(), nullable=False),
        sa.Column(
            "interest", sa.Numeric(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column("total", sa.Numeric(), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("external_updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "observed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("external_order_id", sa.String(), nullable=True),
        sa.Column("sale_order_id", sa.UUID(), nullable=True),
        sa.Column("market_trade_id", sa.UUID(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "interest >= 0 AND interest < 'Infinity'::numeric",
            name="ck_invoice_interest",
        ),
        sa.CheckConstraint(
            "principal >= 0 AND principal < 'Infinity'::numeric",
            name="ck_invoice_principal",
        ),
        sa.CheckConstraint(
            "sale_order_id IS NULL OR "
            "(side = 'SUPPLIER' AND external_order_id IS NOT NULL)",
            name="ck_invoice_sale_role",
        ),
        sa.CheckConstraint(
            "total >= 0 AND total < 'Infinity'::numeric", name="ck_invoice_total"
        ),
        sa.CheckConstraint(
            "external_order_id IS NULL OR length(btrim(external_order_id)) > 0",
            name="ck_invoice_external_order",
        ),
        sa.CheckConstraint("length(btrim(channel)) > 0", name="ck_invoice_channel"),
        sa.CheckConstraint(
            "length(btrim(counterparty_id)) > 0", name="ck_invoice_counterparty"
        ),
        sa.CheckConstraint(
            "length(btrim(external_invoice_id)) > 0", name="ck_invoice_external_id"
        ),
        sa.ForeignKeyConstraint(
            ["market_trade_id", "period_id"],
            ["market_trades.id", "market_trades.period_id"],
            name="fk_invoice_trade_period",
        ),
        sa.ForeignKeyConstraint(
            ["period_id"],
            ["business_periods.id"],
        ),
        sa.ForeignKeyConstraint(
            ["sale_order_id"],
            ["orders.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "period_id", name="uq_invoices_id_period"),
        sa.UniqueConstraint(
            "period_id", "external_invoice_id", name="uq_invoice_external_id"
        ),
        sa.UniqueConstraint(
            "period_id", "external_order_id", name="uq_invoice_external_order"
        ),
        sa.UniqueConstraint("period_id", "sale_order_id", name="uq_invoice_sale_order"),
    )
    op.create_index(
        "ix_invoices_pending",
        "invoices",
        ["period_id", "side", "status", "issued_at"],
        unique=False,
    )
    op.create_table(
        "billing_tasks",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("period_id", sa.UUID(), nullable=False),
        sa.Column("kind", ENUMS["billingtaskkind"], nullable=False),
        sa.Column("external_order_id", sa.String(), nullable=True),
        sa.Column("invoice_id", sa.UUID(), nullable=True),
        sa.Column(
            "status",
            ENUMS["billingoperationstatus"],
            server_default=sa.text("'READY'"),
            nullable=False,
        ),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(kind = 'EMIT_INVOICE' AND external_order_id IS NOT NULL "
            "AND length(btrim(external_order_id)) > 0) OR "
            "(kind = 'PAY_INVOICE' AND invoice_id IS NOT NULL "
            "AND external_order_id IS NULL)",
            name="ck_billing_task_target",
        ),
        sa.CheckConstraint(
            "(status = 'CONFIRMED' AND confirmed_at IS NOT NULL "
            "AND invoice_id IS NOT NULL) OR "
            "(status != 'CONFIRMED' AND confirmed_at IS NULL)",
            name="ck_billing_task_confirmation",
        ),
        sa.ForeignKeyConstraint(
            ["invoice_id", "period_id"],
            ["invoices.id", "invoices.period_id"],
            name="fk_billing_task_invoice_period",
        ),
        sa.ForeignKeyConstraint(
            ["period_id"],
            ["business_periods.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "period_id", "kind", "external_order_id", name="uq_billing_task_order"
        ),
        sa.UniqueConstraint(
            "period_id", "kind", "invoice_id", name="uq_billing_task_invoice"
        ),
    )
    op.create_index(
        "ix_billing_tasks_ready",
        "billing_tasks",
        ["period_id", "status", "next_retry_at"],
        unique=False,
    )
    op.create_table(
        "billing_attempts",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("task_id", sa.UUID(), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            ENUMS["billingoperationstatus"],
            server_default=sa.text("'IN_FLIGHT'"),
            nullable=False,
        ),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_type", sa.String(), nullable=True),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("response", postgresql.JSONB(none_as_null=True), nullable=True),
        sa.CheckConstraint(
            "(status = 'IN_FLIGHT' AND finished_at IS NULL) OR "
            "(status IN ('CONFIRMED', 'UNCERTAIN', 'FAILED') "
            "AND finished_at IS NOT NULL)",
            name="ck_billing_attempt_result",
        ),
        sa.CheckConstraint(
            "response IS NULL OR jsonb_typeof(response) = 'object'",
            name="ck_billing_attempt_response",
        ),
        sa.CheckConstraint(
            "finished_at IS NULL OR finished_at >= started_at",
            name="ck_billing_attempt_dates",
        ),
        sa.CheckConstraint(
            "http_status IS NULL OR http_status BETWEEN 100 AND 599",
            name="ck_billing_attempt_http_status",
        ),
        sa.CheckConstraint("number > 0", name="ck_billing_attempt_number"),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["billing_tasks.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("task_id", "number", name="uq_billing_attempt_number"),
    )
    op.create_index(
        op.f("ix_billing_attempts_task_id"),
        "billing_attempts",
        ["task_id"],
        unique=False,
    )
    op.create_index(
        "uq_billing_attempt_in_flight",
        "billing_attempts",
        ["task_id"],
        unique=True,
        postgresql_where=sa.text("status = 'IN_FLIGHT'"),
    )
    op.alter_column(
        "order_items",
        "unit_price",
        existing_type=sa.INTEGER(),
        type_=sa.Numeric(),
        existing_nullable=False,
    )
    op.alter_column(
        "orders",
        "total",
        existing_type=sa.INTEGER(),
        type_=sa.Numeric(),
        existing_nullable=False,
    )

    op.create_check_constraint(
        "ck_orders_total", "orders", "total >= 0 AND total < 'Infinity'::numeric"
    )
    op.create_check_constraint(
        "ck_order_items_price",
        "order_items",
        "unit_price >= 0 AND unit_price < 'Infinity'::numeric",
    )


def downgrade() -> None:
    connection = op.get_bind()
    for table in (
        "billing_attempts",
        "billing_tasks",
        "invoices",
        "market_trades",
        "business_periods",
    ):
        if connection.scalar(sa.text(f"SELECT EXISTS (SELECT 1 FROM {table})")):
            raise RuntimeError(
                "Cannot downgrade while Delivery 3 financial/trade data exists"
            )
    if connection.scalar(
        sa.text("SELECT EXISTS (SELECT 1 FROM orders WHERE source::text = 'MARKET')")
    ):
        raise RuntimeError("Cannot downgrade while market orders exist")
    for table, column in (("orders", "total"), ("order_items", "unit_price")):
        lossy = connection.scalar(
            sa.text(
                f"SELECT EXISTS (SELECT 1 FROM {table} WHERE "
                f"{column} <> trunc({column}) OR "
                f"{column} NOT BETWEEN -2147483648 AND 2147483647)"
            )
        )
        if lossy:
            raise RuntimeError(
                "Cannot downgrade fractional or out-of-range order amounts"
            )

    for table in (
        "billing_attempts",
        "billing_tasks",
        "invoices",
        "market_trades",
        "business_periods",
    ):
        op.drop_table(table)
    for enum in reversed(tuple(ENUMS.values())):
        enum.drop(connection)
    op.drop_constraint("ck_orders_total", "orders", type_="check")
    op.drop_constraint("ck_order_items_price", "order_items", type_="check")
    op.alter_column(
        "orders",
        "total",
        type_=sa.Integer(),
        existing_type=sa.Numeric(),
        postgresql_using="total::integer",
    )
    op.alter_column(
        "order_items",
        "unit_price",
        type_=sa.Integer(),
        existing_type=sa.Numeric(),
        postgresql_using="unit_price::integer",
    )
    # PostgreSQL cannot DROP an enum value. Recreate the previous type only after
    # proving no MARKET rows exist; drop/recreate its dependent check explicitly.
    op.drop_constraint("ck_orders_web_buyer_email", "orders", type_="check")
    op.execute("ALTER TYPE ordersource RENAME TO ordersource_e3")
    previous_source = postgresql.ENUM(
        "WEB", "SFTP", "PUBSUB", "MESSAGING", name="ordersource", create_type=False
    )
    previous_source.create(connection)
    op.alter_column(
        "orders",
        "source",
        type_=previous_source,
        postgresql_using="source::text::ordersource",
    )
    op.execute("DROP TYPE ordersource_e3")
    op.create_check_constraint(
        "ck_orders_web_buyer_email",
        "orders",
        "source != 'WEB' OR buyer_email IS NOT NULL",
    )
