from pathlib import Path

from app.core.config import get_settings


def test_explicit_env_file_excludes_local_overrides(tmp_path, monkeypatch):
    for key in (
        "DATABASE_URL",
        "FARMA_CENTRAL_BASE_URL",
        "FARMA_CENTRAL_API_SECRET",
        "FARMA_CENTRAL_FTP",
        "FARMA_CENTRAL_GROUP",
    ):
        monkeypatch.delenv(key, raising=False)
    chosen = tmp_path / "selected.env"
    chosen.write_text(
        "DATABASE_URL=postgresql+psycopg://test:test@localhost/new_db\nFARMA_CENTRAL_BASE_URL=https://prod.example.test/\nFARMA_CENTRAL_API_SECRET=test-secret\nFARMA_CENTRAL_FTP=test-ftp\nFARMA_CENTRAL_GROUP=3\n"
    )
    (tmp_path / ".env.local").write_text(
        "FARMA_CENTRAL_BASE_URL=https://dev.example.test/\n"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("APP_ENV_FILE", str(chosen))
    get_settings.cache_clear()
    try:
        assert (
            str(get_settings().farma_central_base_url) == "https://prod.example.test/"
        )
    finally:
        get_settings.cache_clear()


def test_every_systemd_process_uses_the_same_single_file():
    for name in (
        "farmacentral-backend.service",
        "farmacentral-worker@.service",
        "farmacentral-inventory-sync.service",
    ):
        assert (
            "Environment=APP_ENV_FILE=/home/integracion/farmacentral-backend/.env"
            in (Path("deploy") / name).read_text()
        )
