class CheckoutError(Exception):
    pass


class CheckoutConnectionError(CheckoutError):
    pass


class CheckoutTimeoutError(CheckoutError):
    pass


class CheckoutHTTPError(CheckoutError):
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(f"Checkout returned HTTP {status_code}")


class CheckoutInvalidResponseError(CheckoutError):
    pass
