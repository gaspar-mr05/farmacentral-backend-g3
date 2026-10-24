from types import SimpleNamespace

import pytest
from sqlalchemy.exc import SQLAlchemyError

from app.services.orders import SFTPPollResult
from scripts import poll_sftp_orders as module


@pytest.fixture
def runner(monkeypatch):
    state = SimpleNamespace(results=[], polls=0, closed=False, sleeps=[])

    class Context:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            state.closed = True

    class Service:
        def __init__(self, *args, **kwargs):
            pass

        async def poll(self):
            state.polls += 1
            result = state.results.pop(0)
            if isinstance(result, Exception):
                raise result
            return result

    monkeypatch.setattr(
        module, "get_settings", lambda: SimpleNamespace(sftp_poll_interval_seconds=30)
    )
    monkeypatch.setattr(module, "SessionLocal", Context)
    monkeypatch.setattr(module, "PurchaseOrdersClient", lambda **kwargs: Context())
    monkeypatch.setattr(module, "SFTPOrdersClient", lambda settings: None)
    monkeypatch.setattr(module, "SFTPOrderIngestionService", Service)
    return state


@pytest.mark.anyio
async def test_once_polls_once_and_closes_resources(runner):
    runner.results = [SFTPPollResult(received=1, processed=1)]
    await module.run(once=True)
    assert runner.polls == 1
    assert runner.closed


@pytest.mark.anyio
@pytest.mark.parametrize(
    "result",
    [
        SFTPPollResult(connection_failed=True),
        SFTPPollResult(failed=1),
        SQLAlchemyError("unavailable"),
    ],
)
async def test_once_reports_failure_with_nonzero_exit_status(runner, result):
    runner.results = [result]
    with pytest.raises(SystemExit) as error:
        await module.run(once=True)
    assert error.value.code == 1
    assert runner.closed


@pytest.mark.anyio
async def test_periodic_polling_waits_and_continues_after_database_failure(
    runner, monkeypatch
):
    runner.results = [SQLAlchemyError("temporary failure"), SFTPPollResult()]

    class StopPolling(Exception):
        pass

    async def sleep(interval):
        runner.sleeps.append(interval)
        if len(runner.sleeps) == 2:
            raise StopPolling

    monkeypatch.setattr(module.asyncio, "sleep", sleep)
    with pytest.raises(StopPolling):
        await module.run()
    assert runner.polls == 2
    assert runner.sleeps == [30, 30]
    assert runner.closed


@pytest.mark.anyio
async def test_periodic_polling_respects_server_backoff(runner, monkeypatch):
    runner.results = [SFTPPollResult(failed=1, retry_after_seconds=120)]

    class StopPolling(Exception):
        pass

    async def sleep(interval):
        assert interval == 120
        raise StopPolling

    monkeypatch.setattr(module.asyncio, "sleep", sleep)
    with pytest.raises(StopPolling):
        await module.run()
