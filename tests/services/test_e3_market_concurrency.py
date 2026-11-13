import asyncio
import multiprocessing
from contextlib import suppress
from unittest.mock import AsyncMock

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.models import BusinessPeriod, MarketOffer, MarketOfferReservation
from app.services.trades.custody import InvalidTradeStateError
from tests.services.test_recalls import recall_database as recall_database
from tests.services.test_trade_custody import (
    FarmaCentralStub,
    add_market_stock,
    make_service,
    official_offer,
)


def accept_in_process(url, period_id, dispatch, official, entered, release, calls):
    engine = create_engine(url)
    try:
        with Session(engine) as session:
            remote = AsyncMock()

            async def effect(_offer_id):
                with calls.get_lock():
                    calls.value += 1
                entered.set()
                assert release.wait(15)
                raise TimeoutError("Acceptance applied; receipt lost")

            remote.accept_offer.side_effect = effect
            service = make_service(
                session,
                farma_central=FarmaCentralStub(dispatch),
                trades=remote,
                period=session.get(BusinessPeriod, period_id),
            )
            with suppress(InvalidTradeStateError, TimeoutError):
                asyncio.run(service.accept_buy(official))
    finally:
        engine.dispose()


def test_two_processes_accept_one_offer_without_reusing_a_unit(recall_database):
    with Session(recall_database) as session:
        period, _, dispatch, units = add_market_stock(session, quantity=1)
        official = official_offer(
            sku=units[0].lot.product.sku, side="buy", publisher_group=4, quantity=1
        )
        period_id, code = period.id, dispatch.code
        schema = session.connection().exec_driver_sql("SHOW search_path").scalar_one()
        url = recall_database.url.update_query_dict(
            {"options": f"-csearch_path={schema}"}
        ).render_as_string(hide_password=False)
        session.commit()
    context = multiprocessing.get_context("fork")
    entered, release = context.Event(), context.Event()
    calls = context.Value("i", 0)
    args = (url, period_id, code, official, entered, release, calls)
    first, second = (
        context.Process(target=accept_in_process, args=args),
        context.Process(target=accept_in_process, args=args),
    )
    try:
        first.start()
        assert entered.wait(15)
        second.start()
        second.join(15)
        assert second.exitcode == 0 and calls.value == 1
        release.set()
        first.join(15)
        assert first.exitcode == 0
    finally:
        release.set()
        for process in (first, second):
            if process.pid is not None and process.is_alive():
                process.terminate()
                process.join(5)
    with Session(recall_database) as session:
        assert session.scalar(select(func.count()).select_from(MarketOffer)) == 1
        assert (
            session.scalar(
                select(func.count())
                .select_from(MarketOfferReservation)
                .where(MarketOfferReservation.released_at.is_(None))
            )
            == 1
        )
