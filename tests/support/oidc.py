import json
from datetime import UTC, datetime
from types import SimpleNamespace

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from google.auth import crypt, jwt

from app.core.oidc import GoogleOIDCVerifier

AUDIENCE = "https://choripan3.ing.uc.cl/api/messaging"
SERVICE_ACCOUNT = "mensajeria@taller-integracion-310700.iam.gserviceaccount.com"


class TestTokenIssuer:
    __test__ = False

    def __init__(self):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.signer = crypt.RSASigner.from_string(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            ),
            key_id="test-google-key",
        )
        self.certificates = json.dumps(
            {
                "test-google-key": key.public_key()
                .public_bytes(
                    serialization.Encoding.PEM,
                    serialization.PublicFormat.SubjectPublicKeyInfo,
                )
                .decode(),
            }
        ).encode()

    def token(self, **overrides):
        now = int(datetime.now(UTC).timestamp())
        claims = {
            "iss": "https://accounts.google.com",
            "aud": AUDIENCE,
            "sub": "test-service-account-id",
            "iat": now - 10,
            "exp": now + 300,
            "email": SERVICE_ACCOUNT,
            "email_verified": True,
        }
        claims.update(overrides)
        return jwt.encode(self.signer, claims).decode()

    def verifier(self):
        verifier = GoogleOIDCVerifier()

        def certificate_request(url, **kwargs):
            assert url == "https://www.googleapis.com/oauth2/v1/certs"
            return SimpleNamespace(status=200, data=self.certificates)

        verifier._request = certificate_request
        return verifier
