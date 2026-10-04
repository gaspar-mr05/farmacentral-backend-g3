from collections.abc import Callable, Iterable, Sequence
from uuid import UUID

from sqlalchemy.orm import Session

from app.models import Lot, ProductionInput
from app.queries.traceability import (
    get_lot_with_units,
    list_deliveries,
    list_downstream_inputs,
    list_upstream_inputs,
)
from app.schemas.traceability import (
    ProductionLinkResponse,
    TraceabilityDeliveryResponse,
    TraceabilityLotResponse,
    TraceabilityResponse,
    TraceabilityUnitLocationResponse,
    TraceabilityUnitResponse,
)


class LotNotFoundError(Exception):
    """Raised when traceability is requested for an unknown lot."""


class TraceabilityService:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get_traceability(self, identifier: UUID | str) -> TraceabilityResponse:
        identifier = str(identifier)
        lot = get_lot_with_units(self._session, identifier)
        if lot is None:
            raise LotNotFoundError(f"Lot {identifier} was not found")

        lot_id = lot.id

        ancestors, upstream_links = self._walk_lineage(
            lot_id,
            list_upstream_inputs,
            lambda item: item.input_lot,
        )
        descendants, downstream_links = self._walk_lineage(
            lot_id,
            list_downstream_inputs,
            lambda item: item.production_run.output_lot,
        )
        links = {item.id: item for item in upstream_links + downstream_links}

        return TraceabilityResponse(
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
            ],
            ancestors=self._sorted_lots(ancestors.values()),
            descendants=self._sorted_lots(descendants.values()),
            production_links=[
                self._to_production_link(item)
                for item in sorted(links.values(), key=lambda item: str(item.id))
            ],
        )

    def _walk_lineage(
        self,
        root_lot_id: UUID,
        load_links: Callable[[Session, set[UUID]], Sequence[ProductionInput]],
        related_lot: Callable[[ProductionInput], Lot | None],
    ) -> tuple[dict[UUID, Lot], list[ProductionInput]]:
        visited = {root_lot_id}
        frontier = {root_lot_id}
        lots: dict[UUID, Lot] = {}
        links: dict[UUID, ProductionInput] = {}

        while frontier:
            next_frontier: set[UUID] = set()
            for item in load_links(self._session, frontier):
                links[item.id] = item
                lot = related_lot(item)
                if lot is None or lot.id in visited:
                    continue
                lots[lot.id] = lot
                next_frontier.add(lot.id)

            visited.update(next_frontier)
            frontier = next_frontier

        return lots, list(links.values())

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
