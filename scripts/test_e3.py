"""Run E1/E2/E3 regressions against a disposable PostgreSQL cluster."""

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from scripts.test_e2 import local_connections_only


def postgres_bin(name):
    executable = shutil.which(name)
    if executable:
        return executable
    candidates = sorted(Path("/usr/lib/postgresql").glob(f"*/bin/{name}"))
    if not candidates:
        raise RuntimeError("Install PostgreSQL server binaries (initdb and pg_ctl)")
    return str(candidates[-1])


def native_postgres_tools_available(*names):
    try:
        for name in names:
            postgres_bin(name)
    except RuntimeError:
        return False
    return True


@contextmanager
def docker_postgres():
    image = os.environ.get("E3_POSTGRES_IMAGE", "postgres:16-alpine")
    command = [
        "docker",
        "run",
        "--rm",
        "--detach",
        "--pull=missing",
        "--env",
        "POSTGRES_HOST_AUTH_METHOD=trust",
        "--env",
        "POSTGRES_USER=e3_test",
        "--publish",
        "127.0.0.1::5432",
        image,
    ]
    container = subprocess.run(
        command, check=True, capture_output=True, text=True, timeout=30
    ).stdout.strip()
    try:
        published = subprocess.run(
            ["docker", "port", container, "5432/tcp"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()
        port = int(published.rsplit(":", 1)[-1])
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            ready = subprocess.run(
                ["docker", "exec", container, "pg_isready", "-U", "e3_test"],
                check=False,
                capture_output=True,
                timeout=5,
            )
            if ready.returncode == 0:
                break
            time.sleep(0.25)
        else:
            raise RuntimeError("Disposable Docker PostgreSQL did not start")
        yield f"postgresql+psycopg://e3_test@127.0.0.1:{port}/postgres"
    finally:
        subprocess.run(
            ["docker", "stop", "--time", "5", container],
            check=False,
            capture_output=True,
            timeout=15,
        )


@contextmanager
def isolated_postgres():
    if not native_postgres_tools_available("initdb", "pg_ctl"):
        with docker_postgres() as url:
            yield url
        return
    initdb, pg_ctl = postgres_bin("initdb"), postgres_bin("pg_ctl")
    with TemporaryDirectory(prefix="farmacentral-e3-tests-") as directory:
        root = Path(directory)
        data = str(root / "data")
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]

        def run(*args):
            subprocess.run(args, check=True, capture_output=True, timeout=30)

        started = False
        try:
            run(
                initdb,
                "-D",
                data,
                "-A",
                "trust",
                "-U",
                "e3_test",
                "--no-locale",
                "-E",
                "UTF8",
            )
            run(
                pg_ctl,
                "-D",
                data,
                "-l",
                str(root / "postgres.log"),
                "-o",
                f"-h 127.0.0.1 -p {port} -k {root}",
                "-w",
                "start",
            )
            started = True
            yield f"postgresql+psycopg://e3_test@127.0.0.1:{port}/postgres"
        finally:
            if started or (root / "data" / "postmaster.pid").exists():
                run(pg_ctl, "-D", data, "-m", "fast", "-w", "stop")


def summarize_corpus(rows):
    summary = {}
    for row in rows:
        category = summary.setdefault(row["split"], {}).setdefault(row["category"], {})
        for field, correct in row["checks"].items():
            metric = category.setdefault(field, {"correct": 0, "total": 0})
            metric["correct"] += int(correct)
            metric["total"] += 1
    for categories in summary.values():
        for fields in categories.values():
            for metric in fields.values():
                metric["accuracy"] = metric["correct"] / metric["total"]
    return summary


class CorpusReport:
    def __init__(self):
        self.rows = []
        self.collected = 0

    def pytest_collection_finish(self, session):
        self.collected = sum(
            "test_messaging_corpus.py::test_corpus[" in item.nodeid
            for item in session.items
        )

    def pytest_sessionfinish(self, session, exitstatus):
        self.rows = getattr(session.config, "e3_corpus_results", [])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus-only", action="store_true")
    parser.add_argument(
        "--report", type=Path, help="Write test exit code and corpus metrics as JSON"
    )
    args = parser.parse_args(argv)
    import pytest

    report = CorpusReport()
    try:
        with (
            isolated_postgres() as url,
            patch.dict(
                os.environ,
                {
                    "DATABASE_URL": url,
                    "TEST_DATABASE_URL": url,
                },
            ),
            local_connections_only(),
        ):
            paths = (
                ["tests/services/test_messaging_corpus.py"]
                if args.corpus_only
                else ["tests"]
            )
            code = int(pytest.main(["-q", *paths], plugins=[report]))
    except (RuntimeError, subprocess.SubprocessError, OSError):
        print(
            "Could not run isolated PostgreSQL. Check server binaries and use "
            "a non-root user; no operational DB fallback.",
            file=sys.stderr,
        )
        return 2
    result = {
        "test_exit_code": code,
        "corpus_mode": "mocked_proposals",
        "corpus_cases": len(report.rows),
        "corpus_cases_collected": report.collected,
        "corpus_metrics": summarize_corpus(report.rows),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.report:
        args.report.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    return code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
