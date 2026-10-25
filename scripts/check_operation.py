"""Read-only operation checks. Does not start workers or call an LLM."""

import argparse
import asyncio
import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from urllib.parse import quote, urlsplit

import httpx

from scripts.e2_checks import check_workers, result


def public_url(value: str) -> str:
    url = urlsplit(value)
    if (
        url.scheme not in {"http", "https"}
        or not url.hostname
        or url.username is not None
        or url.password is not None
        or url.query
        or url.fragment
    ):
        raise argparse.ArgumentTypeError(
            "Usa una URL HTTP(S) sin credenciales ni query"
        )
    return value.rstrip("/")


async def check_http(client, url: str, *, expected: dict | None = None) -> dict:
    started = perf_counter()
    try:
        async with asyncio.timeout(10):
            response = await client.get(url)
        elapsed_ms = round((perf_counter() - started) * 1000, 2)
        if response.status_code != 200:
            return result(
                "failed", "HTTP no disponible", http_status=response.status_code
            )
        if expected is not None:
            try:
                valid = response.json() == expected
            except ValueError:
                valid = False
            if not valid:
                return result("failed", "Respuesta de salud inesperada")
        return result(
            "passed" if elapsed_ms < 10000 else "failed",
            "HTTP disponible en menos de 10 segundos"
            if elapsed_ms < 10000
            else "HTTP excedió 10 segundos",
            elapsed_ms=elapsed_ms,
        )
    except (httpx.HTTPError, TimeoutError) as exc:
        return result("failed", "HTTP no respondió", error_type=type(exc).__name__)


def check_disk(path: Path, minimum_free_gib: float, minimum_free_percent: float):
    try:
        usage = shutil.disk_usage(path)
    except OSError:
        return result("failed", "No se pudo consultar el volumen", path=str(path))
    free_gib = usage.free / 1024**3
    free_percent = usage.free * 100 / usage.total
    return result(
        "passed"
        if free_gib >= minimum_free_gib and free_percent >= minimum_free_percent
        else "failed",
        "Espacio libre del volumen",
        path=str(path),
        free_gib=round(free_gib, 2),
        free_percent=round(free_percent, 2),
    )


async def check_systemd() -> dict:
    """Check API supervision and the periodic inventory timer without sudo."""
    units = ("farmacentral-backend.service", "farmacentral-inventory-sync.timer")
    try:
        process = await asyncio.create_subprocess_exec(
            "systemctl",
            "show",
            "--no-pager",
            "--property=Id,LoadState,ActiveState,SubState,Restart",
            *units,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        return result("pending", "systemctl no está disponible en este equipo")
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=5)
    except TimeoutError:
        process.kill()
        await process.communicate()
        return result("failed", "systemctl excedió 5 segundos")
    if process.returncode:
        return result("pending", "Consultar systemd desde el servidor")
    observed = {}
    for block in stdout.decode(errors="replace").strip().split("\n\n"):
        fields = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
        if fields.get("Id") in units:
            observed[fields.pop("Id")] = fields
    api, timer = (observed.get(unit, {}) for unit in units)
    valid = (
        api.get("LoadState") == timer.get("LoadState") == "loaded"
        and api.get("ActiveState") == timer.get("ActiveState") == "active"
        and api.get("SubState") == "running"
        and api.get("Restart") in {"always", "on-failure"}
        and timer.get("SubState") in {"waiting", "running", "elapsed"}
    )
    return result(
        "passed" if valid else "failed", "Supervisión de API y timer", units=observed
    )


async def run(args) -> dict:
    async with httpx.AsyncClient(timeout=9, follow_redirects=True) as client:
        probes = {
            "liveness": check_http(
                client, f"{args.api_url}/api/health", expected={"status": "ok"}
            ),
            "database": check_http(
                client,
                f"{args.api_url}/api/health/ready",
                expected={"status": "ok", "database": "ok"},
            ),
        }
        if args.traceability_lot:
            identifier = quote(args.traceability_lot, safe="")
            probes["traceability"] = check_http(
                client, f"{args.api_url}/api/traceability/{identifier}"
            )
        if args.portal_url:
            probes["portal"] = check_http(client, args.portal_url)
        if args.check_services:
            probes["services"] = check_systemd()
            probes["workers"] = check_workers(
                include_replenishment=args.include_replenishment
            )
        values = await asyncio.gather(*probes.values())
    checks = dict(zip(probes, values, strict=True))
    for index, path in enumerate(args.disk_path or [Path("/")]):
        checks[f"disk:{index}"] = check_disk(
            path, args.minimum_free_gib, args.minimum_free_percent
        )
    return {
        "checked_at": datetime.now(UTC).isoformat(),
        "llm_calls": 0,
        "checks": checks,
        "not_verified": [
            "Disponibilidad continua durante la evaluación",
            "Reinicio y recuperación en el servidor con eventos reales",
            "Contenido y rotación de logs (revisar journalctl)",
            *([] if args.check_services else ["Supervisión de servicios en systemd"]),
            *(
                []
                if args.traceability_lot
                else ["Trazabilidad: falta --traceability-lot"]
            ),
            "Portal: falta --portal-url"
            if not args.portal_url
            else "Interacción visual del portal en el navegador",
        ],
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", type=public_url, default="http://127.0.0.1:8000")
    parser.add_argument("--portal-url", type=public_url)
    parser.add_argument("--traceability-lot")
    parser.add_argument("--check-services", action="store_true")
    parser.add_argument("--include-replenishment", action="store_true")
    parser.add_argument("--disk-path", type=Path, action="append")
    parser.add_argument("--minimum-free-gib", type=float, default=1)
    parser.add_argument("--minimum-free-percent", type=float, default=10)
    args = parser.parse_args(argv)
    if not (0 <= args.minimum_free_gib < float("inf")) or not (
        0 <= args.minimum_free_percent <= 100
    ):
        parser.error("Umbrales de disco inválidos")
    if args.include_replenishment:
        args.check_services = True
    report = asyncio.run(run(args))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return int(any(check["status"] != "passed" for check in report["checks"].values()))


if __name__ == "__main__":
    raise SystemExit(main())
