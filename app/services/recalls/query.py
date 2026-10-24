from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.models import InboundEvent, InboundEventSource, Recall, RecallUnit, Unit
from app.schemas.recalls import (
    RecallDetailResponse,
    RecalledUnitResponse,
    RecallSummaryResponse,
    RecallWebhookPayload,
)


class RecallNotFoundError(Exception):
    pass


class RecallQueryService:
    def __init__(self, session: Session) -> None:
        self._session = session

    def list_recalls(self) -> list[RecallSummaryResponse]:
        recalls = {
            recall.recall_id: self._summary(recall)
            for recall in self._session.scalars(select(Recall))
        }
        events = self._session.scalars(
            select(InboundEvent).where(InboundEvent.source == InboundEventSource.RECALL)
        )
        for event in events:
            if event.external_event_id not in recalls:
                recalls[event.external_event_id] = self._event_summary(event)
        return sorted(recalls.values(), key=lambda item: item.issued_at, reverse=True)

    def get_recall(self, recall_id: str) -> RecallDetailResponse:
        recall = self._session.scalar(
            select(Recall).where(Recall.recall_id == recall_id)
        )
        if recall is None:
            event = self._session.scalar(
                select(InboundEvent).where(
                    InboundEvent.source == InboundEventSource.RECALL,
                    InboundEvent.external_event_id == recall_id,
                )
            )
            if event is None:
                raise RecallNotFoundError(f"Recall {recall_id} was not found")
            return RecallDetailResponse(
                **self._event_summary(event).model_dump(), units=[]
            )

        snapshots = self._session.scalars(
            select(RecallUnit)
            .where(RecallUnit.recall_id == recall.id)
            .options(
                joinedload(RecallUnit.unit).joinedload(Unit.lot),
                joinedload(RecallUnit.unit).joinedload(Unit.current_location),
                joinedload(RecallUnit.original_location),
            )
        )
        units = [
            RecalledUnitResponse(
                product_id=snapshot.unit.external_unit_id,
                sku=snapshot.unit.lot.product.sku,
                batch=snapshot.unit.lot.external_lot_id,
                original_location=snapshot.original_location.code,
                current_location=snapshot.unit.current_location.code,
                quarantined_at=snapshot.quarantined_at,
            )
            for snapshot in snapshots
        ]
        units.sort(key=lambda unit: unit.product_id)
        return RecallDetailResponse(**self._summary(recall).model_dump(), units=units)

    @staticmethod
    def _summary(recall: Recall) -> RecallSummaryResponse:
        return RecallSummaryResponse(
            recall_id=recall.recall_id,
            batch=recall.batch,
            sku=recall.sku,
            reason=recall.reason,
            issued_at=recall.issued_at,
            deadline=recall.deadline,
            status=recall.status.value,
            reported_at=recall.reported_at,
        )

    @staticmethod
    def _event_summary(event: InboundEvent) -> RecallSummaryResponse:
        try:
            payload = RecallWebhookPayload.model_validate(event.payload)
        except ValidationError as exc:
            raise RecallNotFoundError(
                f"Recall {event.external_event_id} has an invalid payload"
            ) from exc
        return RecallSummaryResponse(
            recall_id=payload.recall_id,
            batch=payload.batch,
            sku=payload.sku,
            reason=payload.reason,
            issued_at=payload.issued_at,
            deadline=payload.deadline,
            status="received",
            reported_at=None,
        )
