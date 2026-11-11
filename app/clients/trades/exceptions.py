class TradesError(Exception):
    """Base error for the trades integration."""


class TradesConnectionError(TradesError):
    pass


class TradesTimeoutError(TradesError):
    pass


class TradesUncertainResultError(TradesError):
    """The request may have succeeded, but its response was not received."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        self.status_code = status_code
        super().__init__(message)


class TradesHTTPError(TradesError):
    def __init__(
        self, status_code: int, *, retry_after_seconds: float | None = None
    ) -> None:
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds
        super().__init__(f"Trades service returned HTTP {status_code}")


class TradesAuthenticationError(TradesHTTPError):
    """Credentials or the refreshed token were rejected."""


class TradesInvalidResponseError(TradesError):
    pass
