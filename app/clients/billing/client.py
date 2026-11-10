import asyncio
import logging
import math
from contextlib import suppress
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

import httpx
from pydantic import TypeAdapter, ValidationError

from app.core.config import Settings, get_settings
from app.models.billing import InvoiceSide, InvoiceStatus
from app.schemas.billing import (
    BankStatement,
    BillingId,
    BillingInvoice,
    BillingInvoiceQuery,
)
from app.schemas.purchase_orders import PurchaseOrderId

from . import soap
from .exceptions import (
    BillingAuthenticationError,
    BillingConfigurationError,
    BillingConnectionError,
    BillingHTTPError,
    BillingInvalidResponseError,
    BillingRateLimitError,
    BillingSOAPFault,
    BillingTimeoutError,
)
from .rate_limit import BillingRateLimiter

logger = logging.getLogger(__name__)
MUTATIONS = {"emitInvoice", "payInvoice"}


def _endpoint(value: str, *, wsdl: bool = False) -> str:
    url = urlsplit(value)
    local = url.hostname in {"127.0.0.1", "localhost", "::1"}
    if (
        url.username is not None
        or url.password is not None
        or url.fragment
        or not url.hostname
        or (url.scheme != "https" and not (local and url.scheme == "http"))
        or (url.query and not (wsdl and url.query.lower() in {"wsdl", "wsdl="}))
    ):
        raise BillingConfigurationError(
            "Use HTTPS billing URLs without credentials or secret query parameters"
        )
    return value


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("Retry-After")
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            seconds = (parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds()
        except (ValueError, TypeError, OverflowError):
            return None
    return max(0, seconds) if math.isfinite(seconds) else None


class BillingClient:
    """SOAP transport only: one request per operation, with no mutation retries."""

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        http_client: httpx.AsyncClient | None = None,
        limiter: BillingRateLimiter | None = None,
    ) -> None:
        config = settings or get_settings()
        self._url = _endpoint(config.effective_billing_url)
        self._wsdl_url = _endpoint(config.effective_billing_wsdl_url, wsdl=True)
        self._group = str(config.farma_central_group)
        self._secret = config.farma_central_api_secret
        self._timeout = config.billing_timeout_seconds
        self._limiter = limiter or BillingRateLimiter(
            config.effective_billing_rate_limit_file, config.billing_requests_per_minute
        )
        self._owns_http_client = http_client is None
        self._http_client = http_client or httpx.AsyncClient(timeout=self._timeout)
        self._actions: dict[str, str] | None = None
        self._wsdl_lock = asyncio.Lock()

    async def __aenter__(self) -> "BillingClient":
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_http_client:
            await self._http_client.aclose()

    async def load_wsdl(self) -> None:
        async with self._wsdl_lock:
            if self._actions is not None:
                return
            response = await self._send("GET", self._wsdl_url, operation="load_wsdl")
            self._raise_http_error(response, "load_wsdl")
            try:
                self._actions = soap.wsdl_actions(response.content)
            except ValueError:
                raise BillingInvalidResponseError(
                    "Invalid billing WSDL", operation="load_wsdl"
                ) from None

    async def get_bank_statement(self) -> BankStatement:
        operation = "getBankStatement"
        root = await self._call(operation, {})
        try:
            statement = BankStatement.model_validate(
                soap.records(root, operation, "BankStatement")[0]
            )
            if str(statement.group) != self._group:
                raise ValueError("Bank statement belongs to another group")
            return statement
        except (ValueError, ValidationError):
            raise BillingInvalidResponseError(
                "Invalid billing bank statement", operation=operation
            ) from None

    async def get_invoices(
        self,
        *,
        status: InvoiceStatus,
        side: InvoiceSide,
        from_date: datetime,
        to_date: datetime,
    ) -> list[BillingInvoice]:
        query = BillingInvoiceQuery(
            status=status, side=side, from_date=from_date, to_date=to_date
        )
        operation = "getInvoices"
        root = await self._call(
            operation,
            {
                "status": query.status.value,
                "side": query.side.value,
                "fromDate": query.from_date.astimezone(UTC).isoformat(),
                "toDate": query.to_date.astimezone(UTC).isoformat(),
            },
        )
        try:
            invoices = [
                BillingInvoice.model_validate(item)
                for item in soap.records(root, operation, "BillingDetails")
            ]
            for invoice in invoices:
                party = (
                    invoice.client_id
                    if query.side is InvoiceSide.CLIENT
                    else invoice.supplier_id
                )
                if party != self._group or invoice.status is not query.status:
                    raise ValueError(
                        "Invoice does not match requested group/side/status"
                    )
            return invoices
        except (ValueError, ValidationError):
            raise BillingInvalidResponseError(
                "Invalid billing invoice listing", operation=operation
            ) from None

    async def emit_invoice(self, order_id: str) -> BillingInvoice:
        order_id = TypeAdapter(PurchaseOrderId).validate_python(order_id).lower()
        return await self._invoice_operation("emitInvoice", {"order_id": order_id})

    async def pay_invoice(self, invoice_id: str) -> BillingInvoice:
        invoice_id = TypeAdapter(BillingId).validate_python(invoice_id)
        return await self._invoice_operation("payInvoice", {"invoice_id": invoice_id})

    async def _invoice_operation(
        self, operation: str, values: dict[str, str]
    ) -> BillingInvoice:
        root = await self._call(operation, values)
        try:
            invoice = BillingInvoice.model_validate(
                soap.records(root, operation, "BillingDetails")[0]
            )
            party = (
                invoice.supplier_id if operation == "emitInvoice" else invoice.client_id
            )
            if party != self._group:
                raise ValueError("Invoice belongs to another group")
            if operation == "payInvoice" and (
                invoice.status is not InvoiceStatus.PAID
                or invoice.invoice_id != values["invoice_id"]
            ):
                raise ValueError("Payment is not confirmed for the requested invoice")
            return invoice
        except (ValueError, ValidationError):
            raise BillingInvalidResponseError(
                "Invalid billing operation result",
                operation=operation,
                outcome_uncertain=True,
            ) from None

    async def _call(self, operation: str, values: dict[str, str]):
        await self.load_wsdl()
        response = await self._send(
            "POST",
            self._url,
            operation=operation,
            content=soap.envelope(
                operation, values, self._group, self._secret.get_secret_value()
            ),
            headers={
                "Content-Type": "text/xml; charset=utf-8",
                "SOAPAction": f'"{self._actions[operation]}"',
            },
        )
        root = None
        with suppress(ValueError):
            root = soap.body_element(response.content)
        self._raise_http_error(response, operation, root=root)
        if root is None:
            raise BillingInvalidResponseError(
                "Invalid billing SOAP response",
                operation=operation,
                outcome_uncertain=operation in MUTATIONS,
            )
        return root

    async def _raise_rate_limit(self, response: httpx.Response, operation: str) -> None:
        if response.status_code == 429:
            delay = _retry_after(response)
            try:
                await self._limiter.defer(delay if delay is not None else 60)
            except BillingRateLimitError:
                logger.warning(
                    "Billing cooldown persistence failed: operation=%s", operation
                )

    async def _send(
        self, method: str, url: str, *, operation: str, **kwargs
    ) -> httpx.Response:
        try:
            await self._limiter.acquire()
        except BillingRateLimitError as exc:
            exc.operation = operation
            raise
        try:
            async with asyncio.timeout(self._timeout):
                response = await self._http_client.request(
                    method, url, timeout=self._timeout, follow_redirects=False, **kwargs
                )
        except (httpx.TimeoutException, TimeoutError) as exc:
            uncertain = operation in MUTATIONS and not isinstance(
                exc, (httpx.ConnectTimeout, httpx.PoolTimeout)
            )
            raise BillingTimeoutError(
                "Billing request timed out",
                operation=operation,
                outcome_uncertain=uncertain,
            ) from None
        except httpx.RequestError as exc:
            uncertain = operation in MUTATIONS and not isinstance(
                exc, httpx.ConnectError
            )
            raise BillingConnectionError(
                "Billing connection failed",
                operation=operation,
                outcome_uncertain=uncertain,
            ) from None
        await self._raise_rate_limit(response, operation)
        return response

    @staticmethod
    def _raise_http_error(response: httpx.Response, operation: str, root=None) -> None:
        code = soap.fault_code(root) if root is not None else None
        if response.status_code != 200 or code is not None:
            logger.warning(
                "Billing request failed: operation=%s http_status=%s fault_code=%s",
                operation,
                response.status_code,
                code,
            )
        if response.status_code in {401, 403}:
            raise BillingAuthenticationError(
                "Billing credentials rejected",
                operation=operation,
                status_code=response.status_code,
            )
        if code is not None:
            raise BillingSOAPFault(
                code,
                operation=operation,
                status_code=response.status_code,
                outcome_uncertain=operation in MUTATIONS
                and code not in {"Client", "VersionMismatch", "MustUnderstand"},
            )
        if response.status_code != 200:
            uncertain = operation in MUTATIONS and (
                response.status_code >= 500
                or response.status_code == 408
                or 200 <= response.status_code < 400
            )
            raise BillingHTTPError(
                f"Billing returned HTTP {response.status_code}",
                operation=operation,
                status_code=response.status_code,
                retry_after_seconds=_retry_after(response),
                outcome_uncertain=uncertain,
            )
