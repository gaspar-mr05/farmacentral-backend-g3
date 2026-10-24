import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.clients.farma_central import FarmaCentralClient
from app.models import (
    Location,
    Recall,
    RecallNotification,
    RecallNotificationStatus,
    RecallOrder,
    RecallStatus,
    RecallUnit,
    Unit,
)
from app.schemas.farma_central import FarmaCentralSpace
from app.schemas.recall_investigation import RecallInvestigation
from app.schemas.recalls import (
    RecallDeliveryReport,
    RecallDerivedBatchReport,
    RecallNotificationRequest,
    RecallReportRequest,
    RecallStockReport,
)
from app.services.custody.events import record_move

from .investigation import RecallInvestigationService

logger = logging.getLogger(__name__)


class RecallResponseError(Exception):
    pass


@dataclass(frozen=True)
class RecallResponseResult:
    recall_id: str
    quarantined_units: int
    notified_orders: int


class RecallResponseService:
    def __init__(self, client: FarmaCentralClient, session: Session) -> None:
        self._client = client
        self._session = session

    async def respond(self, recall_id: str) -> RecallResponseResult:
        investigation = RecallInvestigationService(self._session).investigate(recall_id)
        recall = self._get_recall(recall_id)
        recall.status = RecallStatus.RESPONDING
        self._session.commit()

        try:
            quarantine = await self._get_quarantine()
            quarantined = await self._quarantine_units(
                recall, investigation, quarantine
            )
            notified = await self._notify_orders(recall)
            report = self._build_report(investigation)
            await self._client.report_recall(recall_id, report)

            recall = self._get_recall(recall_id)
            recall.status = RecallStatus.COMPLETED
            recall.reported_at = datetime.now(UTC)
            recall.last_report = report.model_dump(by_alias=True)
            self._session.commit()
        except Exception:
            self._session.rollback()
            self._mark_failed(recall_id)
            raise

        logger.info(
            "Recall response completed: recall_id=%s quarantined=%s notified=%s",
            recall_id,
            quarantined,
            notified,
        )
        return RecallResponseResult(recall_id, quarantined, notified)

    async def _get_quarantine(self) -> Location:
        payload = await self._client.get_spaces()
        if not isinstance(payload, list):
            raise RecallResponseError("Farma Central spaces must be a list")
        try:
            spaces = [FarmaCentralSpace.model_validate(item) for item in payload]
        except ValidationError as exc:
            raise RecallResponseError("Farma Central returned invalid spaces") from exc
        matches = [space for space in spaces if space.quarantine]
        if len(matches) != 1:
            raise RecallResponseError("Exactly one quarantine space is required")
        location = self._session.scalar(
            select(Location).where(Location.code == matches[0].external_id)
        )
        if location is None:
            raise RecallResponseError(
                "Synchronize inventory before responding to recalls"
            )
        return location

    async def _quarantine_units(
        self,
        recall: Recall,
        investigation: RecallInvestigation,
        quarantine: Location,
    ) -> int:
        moved = 0
        for finding in investigation.stock_units:
            snapshot = self._session.scalar(
                select(RecallUnit)
                .where(
                    RecallUnit.recall_id == recall.id,
                    RecallUnit.unit_id == finding.unit_id,
                )
                .with_for_update()
            )
            unit = self._session.get(Unit, finding.unit_id, with_for_update=True)
            if snapshot is None or unit is None:
                raise RecallResponseError("Recall stock changed during response")
            if snapshot.quarantined_at is not None:
                continue

            assignment = unit.order_assignment
            if assignment is not None and assignment.dispatched_at is None:
                if assignment.dispatch_expected_quantity is not None:
                    raise RecallResponseError(
                        f"Unit {unit.external_unit_id} has an unresolved dispatch"
                    )
                self._session.delete(assignment)

            if unit.current_location_id != quarantine.id:
                await self._client.move_product(unit.external_unit_id, quarantine.code)
                record_move(self._session, unit=unit, to_location_id=quarantine.id)
                moved += 1
            unit.status = "quarantined"
            snapshot.quarantined_at = datetime.now(UTC)
            self._session.commit()
        return moved

    async def _notify_orders(self, recall: Recall) -> int:
        orders = list(
            self._session.scalars(
                select(RecallOrder)
                .where(RecallOrder.recall_id == recall.id)
                .options(
                    joinedload(RecallOrder.order),
                    joinedload(RecallOrder.deliveries),
                    joinedload(RecallOrder.notification),
                )
                .order_by(RecallOrder.id)
            ).unique()
        )
        sent = 0
        for affected in orders:
            if (
                affected.notification is not None
                and affected.notification.status == RecallNotificationStatus.SENT
            ):
                continue
            request = RecallNotificationRequest(
                orderId=affected.order.external_order_id or str(affected.order.id),
                customerName=affected.order.buyer_name,
                productIds=[
                    delivery.unit.external_unit_id for delivery in affected.deliveries
                ],
                message=f"Retiro de productos por {recall.reason}",
            )
            notification = affected.notification
            if notification is None:
                notification = RecallNotification(
                    recall_order=affected,
                    payload=request.model_dump(by_alias=True, exclude_none=True),
                    attempt_count=0,
                )
                self._session.add(notification)
            notification.status = RecallNotificationStatus.PENDING
            notification.attempt_count += 1
            notification.last_error = None
            self._session.commit()

            try:
                await self._client.notify_recall(recall.recall_id, request)
            except Exception as exc:
                notification.status = RecallNotificationStatus.FAILED
                notification.last_error = str(exc)[:1000]
                self._session.commit()
                raise
            notification.status = RecallNotificationStatus.SENT
            notification.sent_at = datetime.now(UTC)
            self._session.commit()
            sent += 1
        return sent

    @staticmethod
    def _build_report(investigation: RecallInvestigation) -> RecallReportRequest:
        lots = {lot.lot_id: lot for lot in investigation.lots}
        return RecallReportRequest(
            inStock=[
                RecallStockReport(
                    productId=unit.external_unit_id,
                    batch=lots[unit.lot_id].external_lot_id,
                    store=unit.original_location_code,
                )
                for unit in investigation.stock_units
            ],
            derivedBatches=[
                RecallDerivedBatchReport(
                    batch=lot.external_lot_id,
                    sku=lot.sku,
                    sourceBatch=lots[lot.parent_lot_id].external_lot_id,
                )
                for lot in investigation.lots
                if lot.parent_lot_id is not None
            ],
            deliveries=[
                RecallDeliveryReport(
                    orderId=order.external_order_id or str(order.order_id),
                    customerName=order.buyer_name,
                    productIds=[
                        delivery.external_unit_id for delivery in order.deliveries
                    ],
                )
                for order in investigation.orders
            ],
        )

    def _get_recall(self, recall_id: str) -> Recall:
        recall = self._session.scalar(
            select(Recall).where(Recall.recall_id == recall_id).with_for_update()
        )
        if recall is None:
            raise RecallResponseError(f"Recall {recall_id} was not found")
        return recall

    def _mark_failed(self, recall_id: str) -> None:
        try:
            recall = self._get_recall(recall_id)
            recall.status = RecallStatus.FAILED
            self._session.commit()
        except Exception:
            self._session.rollback()
