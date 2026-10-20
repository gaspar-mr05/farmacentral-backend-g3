import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.models import ExternalOrderStatus, Order, OrderSource, OrderStatus
from tests.support.orders import EXTERNAL_ORDER_ID, make_external_order


@pytest.mark.parametrize(
    "source", [OrderSource.SFTP, OrderSource.PUBSUB, OrderSource.MESSAGING]
)
def test_external_order_preserves_customer_and_deadline_without_email(
    db_session, source
):
    order = make_external_order(source=source)
    db_session.add(order)
    db_session.flush()
    db_session.expire_all()

    saved = db_session.get(Order, order.id)
    assert saved.external_order_id == EXTERNAL_ORDER_ID
    assert saved.external_customer_id == "farmacia-externa"
    assert saved.buyer_email is None
    assert saved.source is source
    assert saved.status is OrderStatus.RECEIVED
    assert saved.external_status is ExternalOrderStatus.CREATED
    assert saved.delivery_deadline.utcoffset().total_seconds() == 0
    assert saved.items[0].line_total == saved.total


def test_same_external_order_cannot_be_duplicated_by_another_channel(db_session):
    db_session.add(make_external_order())
    db_session.flush()

    with pytest.raises(IntegrityError) as error, db_session.begin_nested():
        db_session.add(make_external_order(source=OrderSource.PUBSUB))
        db_session.flush()

    assert error.value.orig.diag.constraint_name == "uq_orders_external_order_id"
    assert db_session.scalar(select(func.count()).select_from(Order)) == 1


@pytest.mark.parametrize("external_id", ["", "invalid", EXTERNAL_ORDER_ID.upper()])
def test_external_order_identity_must_be_canonical(db_session, external_id):
    with pytest.raises(IntegrityError) as error, db_session.begin_nested():
        db_session.add(make_external_order(external_order_id=external_id))
        db_session.flush()

    assert error.value.orig.diag.constraint_name == "ck_orders_external_order_id"


def test_web_orders_can_have_multiple_null_external_ids(db_session):
    orders = [
        Order(
            buyer_name="Cliente web",
            buyer_email=f"cliente{index}@example.com",
            source=OrderSource.WEB,
            status=OrderStatus.PENDING_PAYMENT,
            total=1500,
        )
        for index in range(2)
    ]
    db_session.add_all(orders)
    db_session.flush()

    assert orders[0].id != orders[1].id
    assert all(order.external_order_id is None for order in orders)
    assert all(order.external_status is None for order in orders)
    assert all(order.delivery_deadline is None for order in orders)


def test_web_order_still_requires_email_in_database(db_session):
    with pytest.raises(IntegrityError) as error, db_session.begin_nested():
        db_session.add(
            Order(
                buyer_name="Cliente web",
                buyer_email=None,
                source=OrderSource.WEB,
                status=OrderStatus.PENDING_PAYMENT,
                total=1500,
            )
        )
        db_session.flush()

    assert error.value.orig.diag.constraint_name == "ck_orders_web_buyer_email"
