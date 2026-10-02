from sqlalchemy.orm import Session

from app.services.traceability import TraceabilityService
from tests.support.traceability import create_traceability_scenario


def test_traces_ancestors_and_descendants(db_session: Session) -> None:
    scenario = create_traceability_scenario(db_session)
    service = TraceabilityService(db_session)

    raw_trace = service.get_traceability(scenario.raw_lot_id)
    intermediate_trace = service.get_traceability(scenario.intermediate_lot_id)
    kit_trace = service.get_traceability(scenario.kit_lot_id)

    assert [lot.external_lot_id for lot in raw_trace.ancestors] == []
    assert {lot.external_lot_id for lot in raw_trace.descendants} == {
        scenario.intermediate_lot_external_id,
        scenario.kit_lot_external_id,
    }
    assert {lot.external_lot_id for lot in intermediate_trace.ancestors} == {
        scenario.raw_lot_external_id
    }
    assert {lot.external_lot_id for lot in intermediate_trace.descendants} == {
        scenario.kit_lot_external_id
    }
    assert {lot.external_lot_id for lot in kit_trace.ancestors} == {
        scenario.raw_lot_external_id,
        scenario.intermediate_lot_external_id,
    }
    assert [lot.external_lot_id for lot in kit_trace.descendants] == []
    assert len(kit_trace.production_links) == 2
    assert kit_trace.production_links[0].consumed_unit_ids
