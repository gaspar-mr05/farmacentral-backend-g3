from pathlib import Path

import pytest
from pydantic import ValidationError

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

    settings = Settings()

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


@pytest.mark.parametrize(
    ("host", "username"),
    [
        ("dev.proyecto.2026-2.tallerdeintegracion.cl", "grupo3_desarrollo"),
        ("proyecto.2026-2.tallerdeintegracion.cl", "grupo3_produccion"),
    ],
)
def test_sftp_username_follows_the_group_and_api_environment(host, username):
    settings = make_settings(farma_central_base_url=f"https://{host}/farma-central/")
    assert settings.effective_sftp_username == username


def test_sftp_credentials_can_be_overridden_without_changing_the_api():
    settings = make_settings(sftp_username="custom", sftp_password="separate-password")
    assert settings.effective_sftp_username == "custom"
    assert settings.effective_sftp_password.get_secret_value() == "separate-password"
    assert settings.farma_central_ftp == "test-ftp"


def test_sftp_legacy_password_is_supported_and_unknown_hosts_require_username():
    settings = make_settings()
    assert settings.effective_sftp_password.get_secret_value() == "test-ftp"
    with pytest.raises(ValueError, match="SFTP_USERNAME"):
        _ = settings.effective_sftp_username


@pytest.mark.parametrize(
    ("host", "environment"),
    [
        ("dev.proyecto.2026-2.tallerdeintegracion.cl", "dev"),
        ("proyecto.2026-2.tallerdeintegracion.cl", "prod"),
    ],
)
def test_pubsub_names_follow_the_group_and_api_environment(host, environment):
    settings = make_settings(farma_central_base_url=f"https://{host}/farma-central/")
    assert settings.effective_pubsub_topic_id == (
        f"{environment}.proyecto.2026-2.grupo3"
    )
    assert settings.effective_pubsub_subscription_id == f"grupo3-{environment}"


def test_pubsub_names_can_be_configured_for_a_custom_host():
    settings = make_settings(
        pubsub_topic_id="custom-topic", pubsub_subscription_id="custom-subscription"
    )
    assert settings.effective_pubsub_topic_id == "custom-topic"
    assert settings.effective_pubsub_subscription_id == "custom-subscription"


def test_messaging_audience_uses_the_configured_public_endpoint():
    settings = make_settings(app_public_url="https://choripan3.ing.uc.cl/")
    assert settings.effective_messaging_oidc_audience == (
        "https://choripan3.ing.uc.cl/api/messaging"
    )
    assert settings.messaging_oidc_service_account == (
        "mensajeria@taller-integracion-310700.iam.gserviceaccount.com"
    )


def test_messaging_audience_override_preserves_the_exact_path():
    settings = make_settings(
        messaging_oidc_audience="https://public.test/custom/webhook/",
    )
    assert settings.effective_messaging_oidc_audience == (
        "https://public.test/custom/webhook/"
    )


def test_messaging_api_defaults_to_the_farma_central_environment():
    settings = make_settings(
        farma_central_base_url="https://dev.example.test/farma-central/"
    )
    assert (
        settings.effective_messaging_base_url == "https://dev.example.test/messaging/"
    )


def test_messaging_api_url_can_be_configured_independently():
    settings = make_settings(messaging_base_url="https://messages.test/api/")
    assert settings.effective_messaging_base_url == "https://messages.test/api/"


def test_llm_gateway_is_optional_and_key_is_redacted():
    assert make_settings().llm_gateway_base_url is None
    settings = make_settings(llm_gateway_api_key="private-llm-key")
    assert settings.llm_gateway_api_key.get_secret_value() == "private-llm-key"
    assert "private-llm-key" not in repr(settings)


@pytest.mark.parametrize(
    "overrides",
    [
        {"llm_gateway_base_url": "not-a-url"},
        {"llm_gateway_api_key": ""},
        {"llm_gateway_model": "  "},
        {"llm_gateway_timeout_seconds": 0},
        {"llm_gateway_max_tokens": 0},
    ],
)
def test_invalid_llm_gateway_configuration_is_rejected(overrides):
    with pytest.raises(ValidationError):
        make_settings(**overrides)
