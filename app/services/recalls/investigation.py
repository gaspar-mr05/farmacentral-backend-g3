import logging
from collections import defaultdict, deque
from time import perf_counter
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.recalls import lock_recall, lock_recall_event, store_investigation
from app.models import Lot, Recall, RecallStatus
from app.queries.recalls import (
    affected_lineage,
    affected_lots,
    get_origin_lot,
    stock_units,
)
from app.queries.traceability import list_deliveries
from app.schemas.recall_investigation import (
    RecallDeliveryFinding,
    RecallInvestigation,
    RecallLineageLink,
    RecallLotFinding,
    RecallOrderFinding,
    RecallStockFinding,
)
from app.schemas.recalls import RecallWebhookPayload

logger = logging.getLogger(__name__)


class RecallInvestigationError(Exception):
    pass


class RecallInvestigationService:
    def __init__(self, session: Session) -> None:
        self._session = session

    def investigate(
        self, recall_id: str, *, block_stock: bool = False
    ) -> RecallInvestigation:
        started = perf_counter()
        try:
            self._session.execute(select(func.set_config("lock_timeout", "3000", True)))
            self._session.execute(
                select(func.set_config("statement_timeout", "15000", True))
            )
            recall = self._load_recall(recall_id)
            origin = get_origin_lot(self._session, recall.batch)
            if origin is None:
                raise RecallInvestigationError(
                    f"Origin batch {recall.batch} is not synchronized. "
                    f"Run scripts.audit_lot_identities --sku {recall.sku}; "
                    "official unit-to-batch evidence is required"
                )
            if origin.product.sku != recall.sku:
                raise RecallInvestigationError(
                    "The recall SKU does not match its origin batch"
                )
            if recall.origin_lot_id not in {None, origin.id}:
                raise RecallInvestigationError(
                    "The stored recall references a different origin lot"
                )
            recall.origin_lot_id = origin.id
            rows = affected_lineage(self._session, origin.id)
            lot_ids = {row.lot_id for row in rows}
            lots = {lot.id: lot for lot in affected_lots(self._session, lot_ids)}
            links = [
                RecallLineageLink(
                    input_lot_id=row.lot_id,
                    output_lot_id=row.child_lot_id,
                    production_run_id=row.production_run_id,
                )
                for row in rows
                if row.child_lot_id is not None
            ]
            generations = self._generations(origin.id, links, lots)
            units = stock_units(self._session, lot_ids)
            findings = RecallInvestigation(
                recall_id=recall.recall_id,
                origin_lot_id=origin.id,
                lots=[
                    RecallLotFinding(
                        lot_id=lot_id,
                        external_lot_id=lots[lot_id].external_lot_id,
                        sku=lots[lot_id].product.sku,
                        generation=generation,
                        parent_lot_id=parent,
                    )
                    for lot_id, (generation, parent) in generations.items()
                ],
                production_links=sorted(
                    links,
                    key=lambda link: (
                        str(link.input_lot_id),
                        str(link.output_lot_id),
                        str(link.production_run_id),
                    ),
                ),
                stock_units=[
                    RecallStockFinding(
                        unit_id=unit.id,
                        external_unit_id=unit.external_unit_id,
                        lot_id=unit.lot_id,
                        sku=lots[unit.lot_id].product.sku,
                        status=unit.status,
                        location_id=unit.current_location_id,
                        location_code=unit.current_location.code,
                        original_location_id=unit.current_location_id,
                        original_location_code=unit.current_location.code,
                    )
                    for unit in units
                ],
                orders=self._orders(lot_ids, lots),
            )
            snapshots = store_investigation(self._session, recall, findings, units)
            for unit in findings.stock_units:
                snapshot = snapshots[unit.unit_id]
                unit.original_location_id = snapshot.original_location_id
                unit.original_location_code = snapshot.original_location.code
            if block_stock:
                # Persist the block while the stock locks are still held. Other
                # workers must not sell or consume it during external API calls.
                for unit in units:
                    if unit.status != "quarantined":
                        unit.status = "recall_pending"
                recall.status = RecallStatus.RESPONDING
            elif recall.status in {RecallStatus.RECEIVED, RecallStatus.FAILED}:
                recall.status = RecallStatus.INVESTIGATING
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise
        findings.elapsed_ms = (perf_counter() - started) * 1000
        logger.info(
            "Recall investigated: recall_id=%s lots=%s stock=%s "
            "orders=%s elapsed_ms=%.2f",
            recall_id,
            len(findings.lots),
            len(findings.stock_units),
            len(findings.orders),
            findings.elapsed_ms,
        )
        return findings

    def _load_recall(self, recall_id: str) -> Recall:
        event = lock_recall_event(self._session, recall_id)
        recall = lock_recall(self._session, recall_id)
        if recall is not None:
            return recall
        if event is None:
            raise RecallInvestigationError(f"Recall {recall_id} was not found")
        try:
            payload = RecallWebhookPayload.model_validate(event.payload)
        except ValidationError as exc:
            raise RecallInvestigationError(
                "The recall event payload is invalid"
            ) from exc
        if payload.recall_id != recall_id:
            raise RecallInvestigationError(
                "The recall event identity does not match its payload"
            )
        recall = Recall(
            inbound_event_id=event.id,
            recall_id=recall_id,
            batch=payload.batch,
            sku=payload.sku,
            reason=payload.reason,
            issued_at=payload.issued_at,
            deadline=payload.deadline,
        )
        self._session.add(recall)
        self._session.flush()
        return recall

    @staticmethod
    def _generations(
        origin_id: UUID, links: list[RecallLineageLink], lots: dict[UUID, Lot]
    ) -> dict[UUID, tuple[int, UUID | None]]:
        children = defaultdict(set)
        for link in links:
            children[link.input_lot_id].add(link.output_lot_id)
        generations = {origin_id: (0, None)}
        frontier = deque([origin_id])
        while frontier:
            parent = frontier.popleft()
            for child in sorted(
                children[parent], key=lambda lot_id: lots[lot_id].external_lot_id
            ):
                if child not in generations:
                    generations[child] = (generations[parent][0] + 1, parent)
                    frontier.append(child)
        return generations

    def _orders(
        self, lot_ids: set[UUID], lots: dict[UUID, Lot]
    ) -> list[RecallOrderFinding]:
        orders = {}
        for assignment in list_deliveries(self._session, lot_ids):
            order = assignment.order_item.order
            if order.id not in orders:
                customer = (
                    order.external_customer_id or order.buyer_email or order.buyer_name
                )
                if not customer or not customer.strip():
                    raise RecallInvestigationError(
                        "A delivered order has no customer reference"
                    )
                orders[order.id] = RecallOrderFinding(
                    order_id=order.id,
                    external_order_id=order.external_order_id,
                    external_customer_id=order.external_customer_id,
                    customer_reference=customer,
                    buyer_name=order.buyer_name,
                    buyer_email=order.buyer_email,
                    deliveries=[],
                )
            unit = assignment.unit
            orders[order.id].deliveries.append(
                RecallDeliveryFinding(
                    order_unit_id=assignment.id,
                    unit_id=unit.id,
                    external_unit_id=unit.external_unit_id,
                    lot_id=unit.lot_id,
                    sku=lots[unit.lot_id].product.sku,
                    dispatched_at=assignment.dispatched_at,
                )
            )
        return list(orders.values())
