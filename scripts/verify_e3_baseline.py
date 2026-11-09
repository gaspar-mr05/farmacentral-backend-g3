"""Read-only E3 baseline inventory. Does not certify full E3 readiness."""

import argparse
import asyncio
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from pydantic import TypeAdapter, ValidationError
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import NullPool

from app.clients.farma_central import FarmaCentralClient, FarmaCentralError
from app.core.config import Settings, get_settings
from app.schemas.farma_central import (
    FarmaCentralInventoryItem,
    FarmaCentralProduct,
    FarmaCentralSpace,
)
from scripts.check_operation import check_http, check_systemd, public_url
from scripts.e2_checks import check_workers, result

ROOT = Path(__file__).resolve().parents[1]


def endpoint_summary(value: str) -> dict:
    url = urlsplit(value)
    return {"scheme": url.scheme, "host": url.hostname, "path": url.path}


def configuration_summary(settings: Settings) -> dict:
    database = make_url(settings.database_url)
    return {
        "app_env": settings.app_env,
        "group": settings.farma_central_group,
        "database": {
            "host": database.host,
            "port": database.port or 5432,
            "name": database.database,
        },
        "endpoints": {
            "farma_central": endpoint_summary(str(settings.farma_central_base_url)),
            "purchase_orders": endpoint_summary(
                settings.effective_purchase_orders_base_url
            ),
            "messaging": endpoint_summary(settings.effective_messaging_base_url),
            "checkout": endpoint_summary(str(settings.checkout_base_url)),
            "public_api": endpoint_summary(str(settings.app_public_url)),
        },
        "settings_env_files": [".env", ".env.local"],
        "note": "El entorno del proceso tiene precedencia; no inspecciona otros PID",
    }


def database_snapshot(connection: Connection) -> dict:
    # A read-only transaction also protects invocation against an operational DB.
    connection.execute(text("SET TRANSACTION READ ONLY"))
    connection.execute(text("SET LOCAL statement_timeout = '5000ms'"))
    current = sorted(MigrationContext.configure(connection).get_current_heads())
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    expected = sorted(ScriptDirectory.from_config(config).get_heads())
    snapshot = result(
        "passed" if current == expected else "failed",
        "Conexión y revisión de migraciones; sin aplicar cambios",
        current=current,
        expected=expected,
        read_only=connection.scalar(text("SHOW transaction_read_only")) == "on",
    )
    if current != expected:
        return snapshot
    snapshot["counts"] = {
        table: connection.scalar(text(f"SELECT count(*) FROM {table}"))
        for table in ("products", "lots", "units", "production_runs", "recalls")
    }
    for table, column in (("orders", "created_at"), ("inbound_events", "received_at")):
        rows = connection.execute(
            text(
                f"SELECT source, status, count(*) AS count, max({column}) AS latest "
                f"FROM {table} GROUP BY source, status ORDER BY source, status"
            )
        ).mappings()
        snapshot[table] = [dict(row) for row in rows]
    return snapshot


def check_database(settings: Settings) -> dict:
    engine = create_engine(
        settings.database_url,
        poolclass=NullPool,
        connect_args={"connect_timeout": 3},
    )
    try:
        with engine.connect() as connection:
            return database_snapshot(connection)
    except SQLAlchemyError as exc:
        # SQLAlchemy errors can contain passwords, SQL parameters or a DSN.
        return result(
            "failed", "Base de datos no verificable", error_type=type(exc).__name__
        )
    finally:
        engine.dispose()


async def inventory_snapshot(client: FarmaCentralClient) -> dict:
    products = TypeAdapter(list[FarmaCentralProduct]).validate_python(
        await client.get_available_products()
    )
    spaces = TypeAdapter(list[FarmaCentralSpace]).validate_python(
        await client.get_spaces()
    )
    observed = []
    for space in spaces:
        counts = TypeAdapter(list[FarmaCentralInventoryItem]).validate_python(
            await client.get_space_inventory(space.external_id)
        )
        observed.append(
            {
                "space_id": space.external_id,
                "quantities": {item.sku: item.quantity for item in counts},
            }
        )
    return result(
        "passed",
        "Conteos oficiales observados; no certifica identidad, linaje o conservación",
        catalog_products=len(products),
        spaces=observed,
        observed_at=datetime.now(UTC).isoformat(),
    )


async def check_inventory(settings: Settings) -> dict:
    try:
        async with FarmaCentralClient(settings=settings) as client:
            return await inventory_snapshot(client)
    except (FarmaCentralError, ValidationError) as exc:
        return result(
            "failed",
            "No se pudo observar el inventario oficial",
            error_type=type(exc).__name__,
            http_status=getattr(exc, "status_code", None),
        )


def git_revision() -> str | None:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT,
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=3,
        ).strip()
    except (OSError, subprocess.SubprocessError):
        return None


async def run(args, settings: Settings) -> dict:
    checks = {"database": await asyncio.to_thread(check_database, settings)}
    if args.inventory:
        checks["official_inventory"] = await check_inventory(settings)
    if args.api_url:
        async with httpx.AsyncClient(timeout=9, follow_redirects=False) as client:
            checks["api_readiness"] = await check_http(
                client,
                f"{args.api_url}/api/health/ready",
                expected={"status": "ok", "database": "ok"},
            )
    if args.check_services:
        checks["api_and_timer"] = await check_systemd()
        checks["workers"] = await check_workers(include_replenishment=True)
    return {
        "checked_at": datetime.now(UTC).isoformat(),
        "scope": "e3_step1_observation_only",
        "commit": git_revision(),
        "configuration": configuration_summary(settings),
        "llm_calls": 0,
        "business_mutations": 0,
        "checks": checks,
        "not_verified": [
            "Correctitud de E2: requiere suite aislada y evidencia de flujos reales",
            "Conservación, FEFO, expiraciones, pulmón y custodia de unidades reales",
            "Configuración efectiva de otros procesos o de un servidor remoto",
            "Contratos E3 pendientes y comportamiento de emisión/cobro/pago SOAP",
            "Disponibilidad continua y recuperación después del reset oficial",
            *([] if args.inventory else ["Inventario oficial: falta --inventory"]),
            *([] if args.api_url else ["API operativa: falta --api-url"]),
            *([] if args.check_services else ["Servicios: falta --check-services"]),
        ],
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", action="store_true")
    parser.add_argument("--check-services", action="store_true")
    parser.add_argument("--api-url", type=public_url)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    report = asyncio.run(run(args, get_settings()))
    serialized = json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n"
    if args.output:
        args.output.write_text(serialized, encoding="utf-8")
    print(serialized, end="")
    return int(any(check["status"] == "failed" for check in report["checks"].values()))


if __name__ == "__main__":
    raise SystemExit(main())
