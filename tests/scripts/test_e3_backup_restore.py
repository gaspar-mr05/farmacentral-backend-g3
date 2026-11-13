import os
import subprocess

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.models import BusinessPeriod
from scripts.test_e3 import isolated_postgres, postgres_bin
from tests.models.test_e2_migration import run_migration
from tests.support.billing import make_period


def test_backup_restore_and_epoch_preservation_on_disposable_databases(tmp_path):
    # Neither source nor target can reference an operational URL: the cluster
    # itself is created by the isolated E3 test harness.
    with isolated_postgres() as address:
        source = create_engine(address)
        target = create_engine(source.url.set(database="restore_rehearsal"))
        try:
            run_migration(source, "upgrade", "head")
            with Session(source) as session:
                period = make_period(code="backup-rehearsal")
                session.add(period)
                session.commit()
                identity = period.id
            dump = tmp_path / "backup.dump"
            env = {
                **os.environ,
                "PGHOST": source.url.host,
                "PGPORT": str(source.url.port),
                "PGUSER": source.url.username,
                "PGPASSWORD": "",
            }
            subprocess.run(
                [
                    postgres_bin("pg_dump"),
                    "--format=custom",
                    "--dbname=postgres",
                    "--file",
                    str(dump),
                ],
                env=env,
                check=True,
                capture_output=True,
                timeout=30,
            )
            with source.connect().execution_options(
                isolation_level="AUTOCOMMIT"
            ) as connection:
                connection.execute(text("CREATE DATABASE restore_rehearsal"))
            subprocess.run(
                [
                    postgres_bin("pg_restore"),
                    "--exit-on-error",
                    "--no-owner",
                    "--dbname=restore_rehearsal",
                    str(dump),
                ],
                env=env,
                check=True,
                capture_output=True,
                timeout=30,
            )
            with Session(target) as session:
                assert session.get(BusinessPeriod, identity).code == "backup-rehearsal"
                assert session.execute(
                    text("SELECT version_num FROM alembic_version")
                ).scalar_one()
            with Session(source) as session:
                assert session.get(BusinessPeriod, identity) is not None
        finally:
            source.dispose()
            target.dispose()
