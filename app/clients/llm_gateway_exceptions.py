from typing import Any


class LLMGatewayError(Exception):
    """Controlled integration failure; never authorizes creating an order."""


class LLMGatewayTimeoutError(LLMGatewayError):
    pass


class LLMGatewayConnectionError(LLMGatewayError):
    pass


class LLMGatewayHTTPError(LLMGatewayError):
    def __init__(
        self, status_code: int, *, retry_after_seconds: float | None = None
    ) -> None:
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds
        super().__init__(f"LLM gateway returned HTTP {status_code}")


class LLMGatewayBudgetExceededError(LLMGatewayHTTPError):
    pass


class LLMGatewayInvalidResponseError(LLMGatewayError):
    def __init__(self, message: str, *, raw_output: Any = None) -> None:
        self.raw_output = raw_output
        super().__init__(message)
