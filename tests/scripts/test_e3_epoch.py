from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import BusinessPeriod, InboundEvent, InboundEventSource
from scripts.prepare_e3_epoch import prepare
from tests.models.test_e2_migration import migration_database as migration_database
from tests.models.test_e2_migration import run_migration
from tests.support.farma_central import make_settings

START = datetime(2026, 11, 18, 13, tzinfo=UTC)
END = START + timedelta(hours=60)


def arguments(**changes):
    return dict(
        environment="dev",
        code="confirmed-rehearsal",
        starts_at=START,
        ends_at=END,
        **changes,
    )


def settings():
    return make_settings(
        app_env="dev",
        farma_central_base_url="https://dev.proyecto.2026-2.tallerdeintegracion.cl/",
    )


def test_check_only_does_not_create_period(migration_database):
    run_migration(migration_database, "upgrade", "head")
    with Session(migration_database) as session:
        result = prepare(session, settings(), **arguments())
        assert not result["created"]
        assert session.scalar(select(BusinessPeriod)) is None


@pytest.mark.parametrize("confirmed", [False, True])
def test_creation_requires_explicit_reset_evidence(migration_database, confirmed):
    run_migration(migration_database, "upgrade", "head")
    with Session(migration_database) as session:
        if not confirmed:
            with pytest.raises(ValueError, match="reset reference"):
                prepare(session, settings(), **arguments(create=True))
        else:
            result = prepare(
                session,
                settings(),
                **arguments(
                    create=True, reset_reference="local rehearsal - synthetic reset"
                ),
            )
            session.commit()
            row = session.scalar(select(BusinessPeriod))
            assert result["period_id"] == str(row.id)
            assert row.starts_at == START and row.ends_at == END


def test_empty_inventory_does_not_erase_historical_work(migration_database):
    run_migration(migration_database, "upgrade", "head")
    with Session(migration_database) as session:
        session.add(
            InboundEvent(
                source=InboundEventSource.MESSAGING,
                external_event_id="old-message",
                payload={},
            )
        )
        session.commit()
        with pytest.raises(ValueError, match="empty application database"):
            prepare(
                session,
                settings(),
                **arguments(create=True, reset_reference="operator-confirmed"),
            )
        session.rollback()
        assert session.scalar(select(InboundEvent.external_event_id)) == "old-message"


def test_migrations_and_environment_must_match_before_epoch_creation(
    migration_database,
):
    with Session(migration_database) as session:
        with pytest.raises(ValueError, match="migrations"):
            prepare(session, settings(), **arguments())
        with pytest.raises(ValueError, match="environment"):
            prepare(
                session,
                make_settings(
                    app_env="prod",
                    farma_central_base_url="https://prod.proyecto.2026-2.tallerdeintegracion.cl/",
                ),
                **arguments(),
            )
