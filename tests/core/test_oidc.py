from datetime import UTC, datetime

import pytest
from google.auth.exceptions import TransportError

from app.core.oidc import (
    GoogleOIDCVerifier,
    InvalidOIDCTokenError,
    OIDCVerificationUnavailableError,
)
from tests.support.oidc import AUDIENCE, SERVICE_ACCOUNT, TestTokenIssuer


@pytest.fixture(scope="module")
def issuer():
    return TestTokenIssuer()


@pytest.fixture
def verifier(issuer):
    verifier = issuer.verifier()
    try:
        yield verifier
    finally:
        verifier.close()


@pytest.mark.parametrize("iss", ["accounts.google.com", "https://accounts.google.com"])
def test_accepts_signed_google_token_for_exact_audience_and_sender(
    issuer, verifier, iss
):
    verifier.verify(
        issuer.token(iss=iss), audience=AUDIENCE, service_account=SERVICE_ACCOUNT
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"iss": "https://attacker.example"},
        {"aud": AUDIENCE + "/"},
        {"aud": "https://choripan3.ing.uc.cl/api/recalls"},
        {"aud": [AUDIENCE]},
        {"exp": 0},
        {"iat": 9999999999, "exp": 99999999999},
        {"email": "other@taller-integracion-310700.iam.gserviceaccount.com"},
        {"email": None},
        {"email_verified": False},
        {"email_verified": "true"},
        {"email_verified": 1},
    ],
)
def test_rejects_invalid_claims(issuer, verifier, overrides):
    with pytest.raises(InvalidOIDCTokenError):
        verifier.verify(
            issuer.token(**overrides),
            audience=AUDIENCE,
            service_account=SERVICE_ACCOUNT,
        )


def test_rejects_a_signature_from_an_untrusted_key(verifier):
    other_issuer = TestTokenIssuer()
    with pytest.raises(InvalidOIDCTokenError):
        verifier.verify(
            other_issuer.token(), audience=AUDIENCE, service_account=SERVICE_ACCOUNT
        )


@pytest.mark.parametrize("token", ["", "not-a-jwt", "a.b.c"])
def test_rejects_malformed_tokens(verifier, token):
    with pytest.raises(InvalidOIDCTokenError):
        verifier.verify(token, audience=AUDIENCE, service_account=SERVICE_ACCOUNT)


@pytest.mark.parametrize("claim", ["iss", "iat", "exp", "aud"])
def test_rejects_missing_required_claims(issuer, verifier, claim):
    from google.auth import jwt

    claims = {
        "iss": "https://accounts.google.com",
        "aud": AUDIENCE,
        "iat": int(datetime.now(UTC).timestamp()) - 10,
        "exp": int(datetime.now(UTC).timestamp()) + 300,
        "email": SERVICE_ACCOUNT,
        "email_verified": True,
    }
    claims.pop(claim)
    token = jwt.encode(issuer.signer, claims).decode()
    with pytest.raises(InvalidOIDCTokenError):
        verifier.verify(token, audience=AUDIENCE, service_account=SERVICE_ACCOUNT)


def test_certificate_network_failure_is_retriable(issuer, verifier):
    def fail(url, **kwargs):
        raise TransportError("Google certificates unavailable")

    verifier._request = fail
    with pytest.raises(OIDCVerificationUnavailableError):
        verifier.verify(
            issuer.token(), audience=AUDIENCE, service_account=SERVICE_ACCOUNT
        )


def test_public_certificates_are_cached_with_bounded_requests(issuer, monkeypatch):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread

    from google.oauth2 import id_token

    requests_received = []

    class CertificatesHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests_received.append(self.path)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(issuer.certificates)))
            self.send_header("Cache-Control", "public, max-age=3600")
            self.end_headers()
            self.wfile.write(issuer.certificates)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), CertificatesHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    verifier = GoogleOIDCVerifier(timeout_seconds=2)
    original_request = verifier._session.request

    def bounded_request(*args, **kwargs):
        assert kwargs["timeout"] == 2
        return original_request(*args, **kwargs)

    monkeypatch.setattr(verifier._session, "request", bounded_request)
    monkeypatch.setattr(
        id_token,
        "_GOOGLE_OAUTH2_CERTS_URL",
        f"http://127.0.0.1:{server.server_port}/certs",
    )
    try:
        for _ in range(2):
            verifier.verify(
                issuer.token(), audience=AUDIENCE, service_account=SERVICE_ACCOUNT
            )
        assert requests_received == ["/certs"]
    finally:
        verifier.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
