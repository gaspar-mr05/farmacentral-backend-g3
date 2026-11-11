import re
import subprocess
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
GIT_SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


def resolve_commit_sha(configured: str, *, repository: Path | None = None) -> str:
    """Resolve ``auto`` from Git while preserving an explicit deployment value."""
    configured = configured.strip()
    if configured.lower() != "auto":
        return configured
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repository or REPOSITORY_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    commit_sha = result.stdout.strip().lower()
    return (
        commit_sha
        if result.returncode == 0 and GIT_SHA.fullmatch(commit_sha)
        else "unknown"
    )
