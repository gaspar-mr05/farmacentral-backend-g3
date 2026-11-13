import pytest

from scripts.check_e3_readiness import check
from tests.support.farma_central import make_settings


def test_preflight_detects_mixed_environment_and_redacts_secrets(tmp_path, monkeypatch):
    selected = tmp_path / "prod.env"
    selected.write_text("placeholder")
    monkeypatch.setenv("APP_ENV_FILE", str(selected))
    settings = make_settings(
        app_env="prod",
        farma_central_base_url="https://prod.example.test/",
        checkout_base_url="https://dev.example.test/",
        sftp_username="grupo3_desarrollo",
        pubsub_topic_id="dev.proyecto.2026-2.grupo3",
        pubsub_subscription_id="grupo3-prod",
    )
    result = check(settings, environment="prod", expected_api_host="prod.example.test")
    assert not result["configuration_valid"]
    assert not result["checks"]["endpoint:checkout"]
    assert not result["checks"]["sftp_environment"]
    assert not result["checks"]["pubsub_environment"]
    assert "test-secret" not in str(result)
    assert not result["checks"]["pubsub_credentials_valid"]


@pytest.mark.parametrize("address_matches", [False, True])
def test_preflight_checks_advertised_soap_address(tmp_path, address_matches):
    settings = make_settings(
        farma_central_base_url="https://dev.proyecto.2026-2.tallerdeintegracion.cl/"
    )
    wsdl = tmp_path / "service.wsdl"
    address = (
        settings.effective_billing_url if address_matches else "https://wrong.test/soap"
    )
    wsdl.write_text(
        '<definitions xmlns:soap="http://schemas.xmlsoap.org/wsdl/soap/">'
        f'<soap:address location="{address}"/></definitions>'
    )
    result = check(
        settings,
        environment="dev",
        expected_api_host="dev.example.test",
        wsdl_file=wsdl,
    )
    assert result["checks"]["wsdl_address_verified"] is address_matches


def test_production_preflight_rejects_development_host():
    with pytest.raises(ValueError, match="development host"):
        check(make_settings(), environment="prod", expected_api_host="dev.example.test")
