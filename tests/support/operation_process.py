"""Local subprocess harness; all external sockets and remote effects are disabled."""

import asyncio
import os
import signal
import socket
import sys
from datetime import UTC, datetime, timedelta

import uvicorn
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.operation_locks import operation_lock
from app.models import InboundEventStatus, MessagingDecision
from app.services.messaging import MessagingProcessingService
from scripts.test_e2 import local_connections_only
from tests.services.test_messaging_processing import (
    FakeInterpreter,
    FakeMessaging,
    FakeOrders,
    add_event,
)
from tests.support.farma_central import make_settings


def main():
    with local_connections_only():
        if sys.argv[1] == "api":
            from app.main import create_app

            listener = socket.socket(fileno=int(sys.argv[2]))
            server = uvicorn.Server(uvicorn.Config(create_app(), log_level="error"))
            server.run(sockets=[listener])
            return

        engine = create_engine(os.environ["DATABASE_URL"])
        try:
            with Session(engine) as session:
                if sys.argv[1] == "worker-stop":
                    event = add_event(
                        session,
                        decision=MessagingDecision.DISCARDED,
                        received_at=datetime.now(UTC),
                    )
                    event.reason = "Persisted interpretation"
                    event.status = InboundEventStatus.PROCESSING
                    event.last_attempt_at = datetime.now(UTC) - timedelta(minutes=2)
                    event.attempt_count = 1
                    session.commit()
                    with operation_lock(session, f"message:{event.external_event_id}"):
                        print("persisted", flush=True)
                        signal.pause()
                else:
                    interpreter = FakeInterpreter()
                    messaging = FakeMessaging()
                    result = asyncio.run(
                        MessagingProcessingService(
                            messaging,
                            interpreter,
                            FakeOrders(),
                            session,
                            settings=make_settings(),
                        ).process_pending()
                    )
                    assert result.processed == 1 and result.failed == 0
                    assert interpreter.calls == 0
                    assert len(messaging.discards) == 1
                    print("recovered", flush=True)
        finally:
            engine.dispose()


if __name__ == "__main__":
    main()
