from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from tests.support.traceability import create_traceability_scenario


def test_get_traceability_returns_lot_lineage_and_current_units(
    api_client: TestClient,
    db_session: Session,
) -> None:
    scenario = create_traceability_scenario(db_session)

    response = api_client.get(f"/api/traceability/{scenario.kit_lot_id}")

    assert response.status_code == 200
    body = response.json()
    assert body["lot"]["external_lot_id"] == scenario.kit_lot_external_id
    assert body["lot"]["origin"] == "own_production"
    assert body["lot"]["quantity"] == 1
    assert body["lot"]["requires_refrigeration"] is False
    assert {lot["external_lot_id"] for lot in body["ancestors"]} == {
        scenario.raw_lot_external_id,
        scenario.intermediate_lot_external_id,
    }
    assert body["descendants"] == []
    assert body["current_units"] == [
        {
            "external_unit_id": scenario.kit_unit_external_id,
            "status": "available",
            "effective_expires_at": body["current_units"][0]["effective_expires_at"],
            "location": {
                "code": scenario.location_code,
                "name": "Área de trazabilidad",
            },
        }
    ]
    assert len(body["production_links"]) == 2


def test_get_traceability_returns_not_found(api_client: TestClient) -> None:
    response = api_client.get(f"/api/traceability/{uuid4()}")

    assert response.status_code == 404


def test_get_traceability_accepts_external_lot_id(
    api_client: TestClient,
    db_session: Session,
) -> None:
    scenario = create_traceability_scenario(db_session)
    response = api_client.get(f"/api/traceability/{scenario.kit_lot_external_id}")

    assert response.status_code == 200
    assert response.json()["lot"]["id"] == str(scenario.kit_lot_id)


def test_get_traceability_excludes_consumed_units_from_current_inventory(
    api_client: TestClient,
    db_session: Session,
) -> None:
    scenario = create_traceability_scenario(db_session)

    response = api_client.get(f"/api/traceability/{scenario.raw_lot_id}")

    assert response.status_code == 200
    assert response.json()["lot"]["quantity"] == 1
    assert response.json()["current_units"] == []
