from functools import partial

import requests
from cachecontrol import CacheControl
from google.auth.exceptions import GoogleAuthError, TransportError
from google.auth.transport.requests import Request
from google.oauth2 import id_token


class InvalidOIDCTokenError(Exception):
    pass


class OIDCVerificationUnavailableError(Exception):
    pass


class GoogleOIDCVerifier:
    """Verify Google signatures and claims, caching public signing certificates."""

    def __init__(self, *, timeout_seconds: float = 3) -> None:
        self._session = CacheControl(requests.Session())
        self._request = partial(Request(session=self._session), timeout=timeout_seconds)

    def close(self) -> None:
        self._session.close()

    def verify(self, token: str, *, audience: str, service_account: str) -> None:
        if not audience or not service_account:
            raise InvalidOIDCTokenError("OIDC audience and sender must be configured")
        try:
            claims = id_token.verify_oauth2_token(
                token,
                self._request,
                audience=audience,
            )
        except TransportError as exc:
            raise OIDCVerificationUnavailableError(
                "Google signing certificates could not be obtained"
            ) from exc
        except (GoogleAuthError, ValueError, KeyError, TypeError) as exc:
            raise InvalidOIDCTokenError("Invalid Google OIDC token") from exc
        if (
            claims.get("email") != service_account
            or claims.get("email_verified") is not True
        ):
            raise InvalidOIDCTokenError("Unauthorized OIDC service account")
