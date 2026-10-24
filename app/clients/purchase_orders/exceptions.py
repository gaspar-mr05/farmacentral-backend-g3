class PurchaseOrdersError(Exception):
    """Base error for the purchase order integration."""


class PurchaseOrdersConnectionError(PurchaseOrdersError):
    pass


class PurchaseOrdersTimeoutError(PurchaseOrdersError):
    pass


class PurchaseOrdersHTTPError(PurchaseOrdersError):
    def __init__(
        self, status_code: int, *, retry_after_seconds: float | None = None
    ) -> None:
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds
        super().__init__(f"Purchase orders service returned HTTP {status_code}")


class PurchaseOrdersAuthenticationError(PurchaseOrdersHTTPError):
    """Credentials or the refreshed token were rejected."""


class PurchaseOrdersInvalidResponseError(PurchaseOrdersError):
    pass
