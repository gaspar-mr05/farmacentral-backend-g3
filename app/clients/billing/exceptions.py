class BillingError(Exception):
    """Safe diagnostics; never includes a SOAP envelope or server fault text."""

    def __init__(
        self,
        message: str,
        *,
        operation: str | None = None,
        outcome_uncertain: bool = False,
        status_code: int | None = None,
        retry_after_seconds: float | None = None,
    ) -> None:
        self.operation = operation
        self.outcome_uncertain = outcome_uncertain
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds
        super().__init__(message)


class BillingConfigurationError(BillingError):
    pass


class BillingTimeoutError(BillingError):
    pass


class BillingConnectionError(BillingError):
    pass


class BillingHTTPError(BillingError):
    pass


class BillingAuthenticationError(BillingHTTPError):
    pass


class BillingInvalidResponseError(BillingError):
    pass


class BillingRateLimitError(BillingError):
    pass


class BillingSOAPFault(BillingError):
    def __init__(self, fault_code: str, **kwargs) -> None:
        self.fault_code = fault_code
        super().__init__(f"Billing SOAP fault ({fault_code})", **kwargs)
