"""Add durable market offers, unit reservations and trade custody links.

Revision ID: a91f0c2d7e34
Revises: 4f2a7c91e6d3
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "a91f0c2d7e34"
down_revision = "4f2a7c91e6d3"
branch_labels = None
depends_on = None

market_offer_side = postgresql.ENUM(
    "BUY", "SELL", name="marketofferside", create_type=False
)


def upgrade() -> None:
    market_offer_side.create(op.get_bind())
    op.create_table(
        "market_offers",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("period_id", sa.UUID(), nullable=False),
        sa.Column("external_offer_id", sa.String(length=24), nullable=True),
        sa.Column("external_order_id", sa.String(length=24), nullable=True),
        sa.Column("side", market_offer_side, nullable=False),
        sa.Column("publisher_group_id", sa.Integer(), nullable=False),
        sa.Column("counterparty_group_id", sa.Integer(), nullable=True),
        sa.Column("sku", sa.String(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("unit_price", sa.Numeric(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
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
            "counterparty_group_id IS NULL OR counterparty_group_id > 0",
            name="ck_market_offer_counterparty",
        ),
        sa.CheckConstraint(
            "unit_price > 0 AND unit_price < 'Infinity'::numeric",
            name="ck_market_offer_price",
        ),
        sa.CheckConstraint("publisher_group_id > 0", name="ck_market_offer_publisher"),
        sa.CheckConstraint("quantity > 0", name="ck_market_offer_quantity"),
        sa.CheckConstraint("length(btrim(sku)) > 0", name="ck_market_offer_sku"),
        sa.ForeignKeyConstraint(["period_id"], ["business_periods.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "period_id", "external_offer_id", name="uq_market_offer_external"
        ),
    )
    op.create_index(
        op.f("ix_market_offers_period_id"),
        "market_offers",
        ["period_id"],
        unique=False,
    )
    op.create_table(
        "market_offer_reservations",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("offer_id", sa.UUID(), nullable=False),
        sa.Column("unit_id", sa.UUID(), nullable=False),
        sa.Column("reserved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["offer_id"], ["market_offers.id"]),
        sa.ForeignKeyConstraint(["unit_id"], ["units.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("offer_id", "unit_id", name="uq_market_offer_unit"),
    )
    op.create_index(
        op.f("ix_market_offer_reservations_offer_id"),
        "market_offer_reservations",
        ["offer_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_market_offer_reservations_unit_id"),
        "market_offer_reservations",
        ["unit_id"],
        unique=False,
    )
    op.create_index(
        "uq_active_market_unit_reservation",
        "market_offer_reservations",
        ["unit_id"],
        unique=True,
        postgresql_where=sa.text("released_at IS NULL"),
    )
    op.add_column(
        "custody_events", sa.Column("market_trade_id", sa.UUID(), nullable=True)
    )
    op.create_foreign_key(
        "fk_custody_events_market_trade",
        "custody_events",
        "market_trades",
        ["market_trade_id"],
        ["id"],
    )
    op.create_unique_constraint(
        "uq_custody_market_trade_unit",
        "custody_events",
        ["market_trade_id", "unit_id"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_custody_market_trade_unit", "custody_events", type_="unique")
    op.drop_constraint(
        "fk_custody_events_market_trade", "custody_events", type_="foreignkey"
    )
    op.drop_column("custody_events", "market_trade_id")
    op.drop_index(
        "uq_active_market_unit_reservation",
        table_name="market_offer_reservations",
        postgresql_where=sa.text("released_at IS NULL"),
    )
    op.drop_index(
        op.f("ix_market_offer_reservations_unit_id"),
        table_name="market_offer_reservations",
    )
    op.drop_index(
        op.f("ix_market_offer_reservations_offer_id"),
        table_name="market_offer_reservations",
    )
    op.drop_table("market_offer_reservations")
    op.drop_index(op.f("ix_market_offers_period_id"), table_name="market_offers")
    op.drop_table("market_offers")
    market_offer_side.drop(op.get_bind())
