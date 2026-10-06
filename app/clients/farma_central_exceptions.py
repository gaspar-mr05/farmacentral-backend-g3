class FarmaCentralError(Exception):
    """Base exception for Farma Central integration errors."""


class FarmaCentralConnectionError(FarmaCentralError):
    """Raised when Farma Central cannot be reached."""


class FarmaCentralTimeoutError(FarmaCentralError):
    """Raised when Farma Central does not respond in time."""


class FarmaCentralHTTPError(FarmaCentralError):
    def __init__(
        self,
        status_code: int,
        message: str | None = None,
        *,
        retry_after_seconds: float | None = None,
    ) -> None:
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds
        super().__init__(message or f"Farma Central returned HTTP {status_code}")


class FarmaCentralAuthenticationError(FarmaCentralHTTPError):
    def __init__(self, status_code: int) -> None:
        super().__init__(status_code, "Farma Central authentication failed")


class FarmaCentralInvalidResponseError(FarmaCentralError):
    """Raised when a Farma Central response has an invalid shape."""
