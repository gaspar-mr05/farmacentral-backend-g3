class FarmaCentralError(Exception):
    """Base exception for all Farma Central integration errors."""


class FarmaCentralConnectionError(FarmaCentralError):
    """Raised when Farma Central cannot be reached."""


class FarmaCentralTimeoutError(FarmaCentralError):
    """Raised when Farma Central does not respond within the configured timeout."""


class FarmaCentralHTTPError(FarmaCentralError):
    """Raised when Farma Central responds with an unsuccessful HTTP status."""

    def __init__(self, status_code: int, message: str | None = None) -> None:
        self.status_code = status_code
        super().__init__(message or f"Farma Central returned HTTP {status_code}")


class FarmaCentralAuthenticationError(FarmaCentralHTTPError):
    """Raised when Farma Central rejects the configured credentials."""

    def __init__(self, status_code: int) -> None:
        super().__init__(status_code, "Farma Central authentication failed")


class FarmaCentralInvalidResponseError(FarmaCentralError):
    """Raised when a Farma Central response cannot be decoded or validated."""
