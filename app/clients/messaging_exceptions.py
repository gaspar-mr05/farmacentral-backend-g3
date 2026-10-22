class MessagingError(Exception):
    """Base error for the messaging platform integration."""


class MessagingConnectionError(MessagingError):
    pass


class MessagingTimeoutError(MessagingError):
    pass


class MessagingHTTPError(MessagingError):
    def __init__(
        self, status_code: int, *, retry_after_seconds: float | None = None
    ) -> None:
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds
        super().__init__(f"Messaging service returned HTTP {status_code}")


class MessagingAuthenticationError(MessagingHTTPError):
    pass


class MessagingAlreadyRespondedError(MessagingHTTPError):
    pass


class MessagingInvalidResponseError(MessagingError):
    pass
