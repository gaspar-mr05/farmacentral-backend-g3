import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clients.checkout import CheckoutClient
from app.clients.checkout_exceptions import CheckoutInvalidResponseError
from app.core.config import Settings, get_settings
from app.models import Order, OrderStatus, Payment, PaymentStatus
from app.schemas.payments import PaymentStartResponse


class PaymentNotFoundError(Exception):
    pass


class OrderAlreadyPaidError(Exception):
    pass


class PaymentAlreadyPendingError(Exception):
    pass


class PaymentService:
    def __init__(
        self,
        client: CheckoutClient,
        session: Session,
        settings: Settings | None = None,
    ) -> None:
        self._client = client
        self._session = session
        self._settings = settings or get_settings()

    async def start(self, order: Order) -> PaymentStartResponse:
        if order.status in {OrderStatus.PAID, OrderStatus.DISPATCHED}:
            raise OrderAlreadyPaidError("The order has already been paid")
        if self._has_pending_payment(order.id):
            raise PaymentAlreadyPendingError("The order already has a pending payment")

        payment_id = uuid.uuid4()
        payload = await self._client.initialize_payment(
            amount=order.total,
            back_urls=self._back_urls(payment_id),
        )
        external_id = payload.get("payment_id")
        redirect_url = payload.get("redirect_url")
        if not isinstance(external_id, str) or not external_id:
            raise CheckoutInvalidResponseError(
                "Checkout response did not include a payment_id"
            )
        if not isinstance(redirect_url, str) or not redirect_url:
            raise CheckoutInvalidResponseError(
                "Checkout response did not include a redirect_url"
            )

        payment = Payment(
            id=payment_id,
            order_id=order.id,
            external_transaction_id=external_id,
            amount=order.total,
            status=PaymentStatus.PENDING,
        )
        order.status = OrderStatus.PENDING_PAYMENT
        self._session.add(payment)
        self._session.commit()
        return PaymentStartResponse(
            id=payment.id,
            order_id=payment.order_id,
            external_transaction_id=payment.external_transaction_id,
            amount=payment.amount,
            status=payment.status,
            payment_url=redirect_url,
        )

    async def confirm(self, payment_id: uuid.UUID) -> Payment:
        payment = self._get_for_update(payment_id)
        if payment.status != PaymentStatus.PENDING:
            return payment

        payload = await self._client.get_payment(payment.external_transaction_id)
        external_status = self._external_status(payload)
        self._validate_external_payment(payload, payment)

        payment.status = external_status
        payment.order.status = _order_status(external_status)
        self._session.commit()
        return payment

    def _get_for_update(self, payment_id: uuid.UUID) -> Payment:
        payment = self._session.scalar(
            select(Payment).where(Payment.id == payment_id).with_for_update()
        )
        if payment is None:
            raise PaymentNotFoundError(f"Payment {payment_id} was not found")
        return payment

    def _has_pending_payment(self, order_id: uuid.UUID) -> bool:
        return (
            self._session.scalar(
                select(Payment.id).where(
                    Payment.order_id == order_id,
                    Payment.status == PaymentStatus.PENDING,
                )
            )
            is not None
        )

    def _back_urls(self, payment_id: uuid.UUID) -> dict[str, str]:
        base_url = str(self._settings.app_public_url).rstrip("/")
        path = f"{base_url}/api/payments/{payment_id}/return"
        return {
            "success": f"{path}/success",
            "error": f"{path}/error",
            "cancelled": f"{path}/cancelled",
        }

    def _validate_external_payment(
        self, payload: dict[str, Any], payment: Payment
    ) -> None:
        if payload.get("amount") != payment.amount:
            raise CheckoutInvalidResponseError("Checkout returned a different amount")
        if payload.get("group") != self._settings.farma_central_group:
            raise CheckoutInvalidResponseError("Checkout returned a different group")

    @staticmethod
    def _external_status(payload: dict[str, Any]) -> PaymentStatus:
        status = payload.get("status")
        if not isinstance(status, str):
            raise CheckoutInvalidResponseError(
                "Checkout response did not include a status"
            )
        try:
            return PaymentStatus(status.lower())
        except ValueError as exc:
            raise CheckoutInvalidResponseError(
                f"Checkout returned unknown status {status}"
            ) from exc


def _order_status(payment_status: PaymentStatus) -> OrderStatus:
    if payment_status == PaymentStatus.SUCCESS:
        return OrderStatus.PAID
    if payment_status == PaymentStatus.CANCELLED:
        return OrderStatus.CANCELLED
    if payment_status in (PaymentStatus.ERROR, PaymentStatus.OBSOLETE):
        return OrderStatus.PAYMENT_ERROR
    return OrderStatus.PENDING_PAYMENT
