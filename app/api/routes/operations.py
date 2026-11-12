"""Private diagnostics from persisted data; no remote calls or commands."""

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.dependencies import require_operations_key
from app.core.config import get_settings
from app.db.session import get_session
from app.models import (
    BusinessMetricSnapshot,
    BusinessPeriod,
    Invoice,
    MarketOffer,
    Order,
    OrderStatus,
)
from app.schemas.operations import (
    OperationalInvoiceResponse,
    OperationalOfferResponse,
    OperationsMetricsResponse,
    OperationsPage,
    WaitingOrderResponse,
)
from app.services.billing.periods import billing_period

router = APIRouter(
    prefix="/operations",
    tags=["operations"],
    dependencies=[Depends(require_operations_key)],
)
Database = Annotated[Session, Depends(get_session)]


def get_period(period_id: UUID, session: Database) -> BusinessPeriod:
    try:
        return billing_period(session, get_settings(), period_id)
    except ValueError as exc:
        raise HTTPException(404, "Operations period not found") from exc


Period = Annotated[BusinessPeriod, Depends(get_period)]
Limit = Annotated[int, Query(ge=1, le=100)]
Offset = Annotated[int, Query(ge=0)]


def page(session, period, statement, schema, limit, offset):
    rows = session.scalars(statement.limit(limit + 1).offset(offset)).all()
    items = []
    for row in rows[:limit]:
        if isinstance(row, MarketOffer):
            item = schema.model_validate(
                {
                    **{
                        field: getattr(row, field)
                        for field in schema.model_fields
                        if field != "has_error"
                    },
                    "has_error": row.last_error is not None,
                }
            )
        else:
            item = schema.model_validate(row)
        items.append(item)
    return OperationsPage(
        period_id=period.id,
        read_at=datetime.now(UTC),
        items=items,
        limit=limit,
        offset=offset,
        has_more=len(rows) > limit,
    )


@router.get("/waiting-orders", response_model=OperationsPage[WaitingOrderResponse])
def waiting_orders(
    session: Database, period: Period, limit: Limit = 50, offset: Offset = 0
):
    statement = select(Order).where(
        Order.external_order_id.is_not(None),
        Order.status == OrderStatus.RECEIVED,
        Order.created_at >= period.starts_at,
    )
    if period.ends_at is not None:
        statement = statement.where(Order.created_at < period.ends_at)
    return page(
        session,
        period,
        statement.order_by(Order.delivery_deadline.asc().nullslast(), Order.id),
        WaitingOrderResponse,
        limit,
        offset,
    )


@router.get("/offers", response_model=OperationsPage[OperationalOfferResponse])
def offers(session: Database, period: Period, limit: Limit = 50, offset: Offset = 0):
    return page(
        session,
        period,
        select(MarketOffer)
        .where(MarketOffer.period_id == period.id)
        .order_by(MarketOffer.updated_at.desc(), MarketOffer.id),
        OperationalOfferResponse,
        limit,
        offset,
    )


@router.get("/invoices", response_model=OperationsPage[OperationalInvoiceResponse])
def invoices(session: Database, period: Period, limit: Limit = 50, offset: Offset = 0):
    return page(
        session,
        period,
        select(Invoice)
        .where(Invoice.period_id == period.id)
        .order_by(Invoice.issued_at.desc(), Invoice.id),
        OperationalInvoiceResponse,
        limit,
        offset,
    )


@router.get("/metrics", response_model=OperationsMetricsResponse)
def metrics(session: Database, period: Period):
    snapshot = session.scalar(
        select(BusinessMetricSnapshot)
        .where(
            BusinessMetricSnapshot.period_id == period.id,
        )
        .order_by(BusinessMetricSnapshot.captured_at.desc(), BusinessMetricSnapshot.id)
        .limit(1)
    )
    return OperationsMetricsResponse(
        period_id=period.id,
        read_at=datetime.now(UTC),
        state="captured" if snapshot else "not_captured",
        captured_at=snapshot.captured_at if snapshot else None,
        commit_sha=snapshot.commit_sha if snapshot else None,
        metrics=snapshot.metrics if snapshot else None,
    )
