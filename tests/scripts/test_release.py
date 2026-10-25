"""Exercise deployment control flow without sudo, services, pip or a database."""

import os
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    "failed_unit",
    [None, "farmacentral-backend.service", "farmacentral-workers.target"],
)
def test_deploy_reports_exact_failed_restart_and_checks_health_only_after_both(
    tmp_path, failed_unit
):
    app_dir = tmp_path / "app"
    bin_dir = tmp_path / "bin"
    venv_bin = app_dir / ".venv/bin"
    bin_dir.mkdir()
    venv_bin.mkdir(parents=True)
    command_log = tmp_path / "commands"

    def executable(path, body):
        path.write_text("#!/bin/bash\nset -eu\n" + body)
        path.chmod(0o755)

    # Listing permissions succeeds even when executing a command requires a password.
    executable(
        bin_dir / "sudo",
        'printf "sudo %s\\n" "$*" >> "$COMMAND_LOG"\n'
        'if [[ " $* " == *" -l "* ]]; then exit 0; fi\n'
        'if [[ "${!#}" == "$FAILED_UNIT" ]]; then\n'
        '  echo "sudo: a password is required" >&2; exit 1\nfi\n',
    )
    for name in ("python", "alembic"):
        executable(
            venv_bin / name,
            f'printf "{name} %s\\n" "$*" >> "$COMMAND_LOG"\n',
        )
    executable(bin_dir / "curl", 'echo "health" >> "$COMMAND_LOG"\n')
    script = (
        Path("deploy/release.sh")
        .read_text()
        .replace(
            'readonly APP_DIR="/home/integracion/farmacentral-backend"',
            f'readonly APP_DIR="{app_dir}"',
        )
    )
    release = tmp_path / "release.sh"
    release.write_text(script)
    completed = subprocess.run(
        ["/bin/bash", str(release)],
        env={
            **os.environ,
            "PATH": f"{bin_dir}:/usr/bin:/bin",
            "COMMAND_LOG": str(command_log),
            "FAILED_UNIT": failed_unit or "",
        },
        capture_output=True,
        text=True,
        timeout=5,
    )
    commands = command_log.read_text()
    assert "python -m pip install" in commands
    assert "alembic upgrade head" in commands
    if failed_unit:
        assert completed.returncode != 0
        assert failed_unit in completed.stderr
        assert "NOPASSWD" in completed.stderr
        assert "Deployment completed successfully" not in completed.stdout
        assert "health" not in commands
        if failed_unit == "farmacentral-backend.service":
            assert "restart farmacentral-workers.target" not in commands
    else:
        assert completed.returncode == 0
        assert "Deployment completed successfully" in completed.stdout
        assert (
            commands.index("restart farmacentral-backend.service")
            < commands.index("restart farmacentral-workers.target")
            < commands.index("health")
        )
