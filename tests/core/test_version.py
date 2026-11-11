import subprocess

from app.core.version import resolve_commit_sha


def run_git(repository, *args):
    return subprocess.run(
        ["git", *args],
        cwd=repository,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def test_resolves_current_git_commit_when_configured_as_auto(tmp_path):
    run_git(tmp_path, "init")
    run_git(tmp_path, "config", "user.email", "test@example.test")
    run_git(tmp_path, "config", "user.name", "Test User")
    (tmp_path / "tracked.txt").write_text("content")
    run_git(tmp_path, "add", "tracked.txt")
    run_git(tmp_path, "commit", "-m", "test commit")

    assert resolve_commit_sha("auto", repository=tmp_path) == run_git(
        tmp_path, "rev-parse", "HEAD"
    )


def test_returns_unknown_when_auto_is_outside_a_git_repository(tmp_path):
    assert resolve_commit_sha("auto", repository=tmp_path) == "unknown"


def test_preserves_an_explicit_deployment_commit(tmp_path):
    explicit = "a" * 40

    assert resolve_commit_sha(explicit, repository=tmp_path) == explicit
