from pathlib import Path

import pytest

from app.core.config import Settings
from tests.support.farma_central import make_settings


def test_env_local_overrides_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for variable in (
        "DATABASE_URL",
        "FARMA_CENTRAL_BASE_URL",
        "FARMA_CENTRAL_API_SECRET",
        "FARMA_CENTRAL_FTP",
        "FARMA_CENTRAL_GROUP",
    ):
        monkeypatch.delenv(variable, raising=False)

    (tmp_path / ".env").write_text(
        "\n".join(
            (
                "DATABASE_URL=postgresql+psycopg://unused:unused@localhost/unused",
                "FARMA_CENTRAL_BASE_URL=https://prod.example.test/api/",
                "FARMA_CENTRAL_API_SECRET=test-secret",
                "FARMA_CENTRAL_FTP=test-ftp",
                "FARMA_CENTRAL_GROUP=3",
            )
        ),
        encoding="utf-8",
    )
    (tmp_path / ".env.local").write_text(
        "FARMA_CENTRAL_BASE_URL=https://dev.example.test/api/\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    settings = Settings()  # type: ignore[call-arg]

    assert str(settings.farma_central_base_url) == "https://dev.example.test/api/"
    assert settings.farma_central_group == 3


def test_cors_origins_are_parsed() -> None:
    settings = Settings(
        _env_file=None,
        database_url="postgresql+psycopg://unused:unused@localhost/unused",
        farma_central_base_url="https://example.test/api/",
        farma_central_api_secret="test-secret",
        farma_central_ftp="test-ftp",
        farma_central_group=3,
        cors_origins="https://one.test, https://two.test",
    )

    assert settings.allowed_origins == ["https://one.test", "https://two.test"]


@pytest.mark.parametrize("environment", ["dev", "prod"])
def test_purchase_orders_default_to_the_farma_central_environment(environment):
    settings = make_settings(
        farma_central_base_url=f"https://{environment}.example.test/farma-central/"
    )

    assert settings.effective_purchase_orders_base_url == (
        f"https://{environment}.example.test/ordenes-compra/"
    )


def test_purchase_orders_url_can_be_configured_independently():
    settings = make_settings(purchase_orders_base_url="https://orders.test/service/")

    assert settings.effective_purchase_orders_base_url == "https://orders.test/service/"
