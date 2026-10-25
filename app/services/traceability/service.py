import logging
from collections.abc import Iterable
from time import perf_counter
from uuid import UUID

from sqlalchemy.orm import Session

from app.models import Lot, ProductionInput
from app.queries.traceability import (
    get_lot_with_units,
    list_deliveries,
    list_lineage_inputs,
)
from app.schemas.traceability import (
    ProductionLinkResponse,
    TraceabilityDeliveryResponse,
    TraceabilityLotResponse,
    TraceabilityResponse,
    TraceabilityUnitLocationResponse,
    TraceabilityUnitResponse,
)

logger = logging.getLogger(__name__)


class LotNotFoundError(Exception):
    """Raised when traceability is requested for an unknown lot."""


class TraceabilityService:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get_traceability(self, identifier: UUID | str) -> TraceabilityResponse:
        started = perf_counter()
        identifier = str(identifier)
        lot = get_lot_with_units(self._session, identifier)
        if lot is None:
            raise LotNotFoundError(f"Lot {identifier} was not found")

        lot_id = lot.id

        upstream_links = list_lineage_inputs(self._session, lot_id, upstream=True)
        downstream_links = list_lineage_inputs(self._session, lot_id, upstream=False)
        ancestors = {item.input_lot.id: item.input_lot for item in upstream_links}
        descendants = {
            item.production_run.output_lot.id: item.production_run.output_lot
            for item in downstream_links
        }
        ancestors.pop(lot_id, None)
        descendants.pop(lot_id, None)
        links = {item.id: item for item in upstream_links + downstream_links}

        response = TraceabilityResponse(
            lot=self._to_lot(lot),
            deliveries=[
                TraceabilityDeliveryResponse(
                    lot_id=assignment.unit.lot_id,
                    order_id=assignment.order_item.order_id,
                    buyer_name=assignment.order_item.order.buyer_name,
                    buyer_email=assignment.order_item.order.buyer_email,
                    sku=assignment.order_item.sku,
                    external_unit_id=assignment.unit.external_unit_id,
                    dispatched_at=assignment.dispatched_at,
                )
                for assignment in list_deliveries(self._session, {lot_id, *descendants})
            ],
            current_units=[
                TraceabilityUnitResponse(
                    external_unit_id=unit.external_unit_id,
                    status=unit.status,
                    effective_expires_at=unit.effective_expires_at,
                    location=TraceabilityUnitLocationResponse(
                        code=unit.current_location.code,
                        name=unit.current_location.name,
                    ),
                )
                for unit in sorted(lot.units, key=lambda item: item.external_unit_id)
                if unit.status in {"available", "reserved"}
            ],
            ancestors=self._sorted_lots(ancestors.values()),
            descendants=self._sorted_lots(descendants.values()),
            production_links=[
                self._to_production_link(item)
                for item in sorted(links.values(), key=lambda item: str(item.id))
            ],
        )
        elapsed_ms = (perf_counter() - started) * 1000
        logger.log(
            logging.WARNING if elapsed_ms >= 10000 else logging.INFO,
            "Traceability queried: lot_id=%s elapsed_ms=%.2f "
            "ancestors=%s descendants=%s",
            identifier,
            elapsed_ms,
            len(ancestors),
            len(descendants),
        )
        return response

    @staticmethod
    def _to_lot(lot: Lot) -> TraceabilityLotResponse:
        return TraceabilityLotResponse(
            id=lot.id,
            external_lot_id=lot.external_lot_id,
            product_sku=lot.product.sku,
            product_name=lot.product.name,
            origin=lot.origin,
            expires_at=lot.expires_at,
            quantity=len(lot.units),
            requires_refrigeration=lot.product.requires_refrigeration,
        )

    @classmethod
    def _sorted_lots(cls, lots: Iterable[Lot]) -> list[TraceabilityLotResponse]:
        return [
            cls._to_lot(lot)
            for lot in sorted(lots, key=lambda item: item.external_lot_id)
        ]

    @staticmethod
    def _to_production_link(item: ProductionInput) -> ProductionLinkResponse:
        run = item.production_run
        return ProductionLinkResponse(
            production_run_id=run.id,
            input_lot_id=item.input_lot_id,
            output_lot_id=run.output_lot_id,
            quantity_consumed=item.quantity_consumed,
            consumed_unit_ids=sorted(
                input_unit.unit.external_unit_id for input_unit in item.units
            ),
            requested_at=run.requested_at,
            completed_at=run.completed_at,
        )
