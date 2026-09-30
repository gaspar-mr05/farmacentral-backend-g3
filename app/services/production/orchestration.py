# app/services/production/orchestration.py
import asyncio
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clients.farma_central import FarmaCentralClient
from app.clients.farma_central_pow import solve_challenge
from app.models import Location, Lot, ProductionRun, Unit, Product
from app.schemas.farma_central import (
    FarmaCentralChallengeResponse,
    FarmaCentralSupplyResponse,
)
from app.services.production.consumption import consume_units_for_run
from app.services.production.runs import start_production_run


async def produce(
    session: Session,
    *,
    client: FarmaCentralClient,
    sku: str,
    quantity: int,
    input_units_by_lot: dict[UUID, list[Unit]] | None = None,
) -> tuple[ProductionRun, FarmaCentralSupplyResponse]:
    """Orquesta la producción de cualquier SKU (Acondicionado o Kit).

    Resuelve el challenge de PoW, envía la solicitud a Farma Central,
    inicia la corrida y registra el consumo de las unidades físicas utilizadas.
    """
    if input_units_by_lot is None:
        input_units_by_lot = await _resolve_input_units_for_sku(
            session, client, sku=sku, quantity=quantity
        )

    # 1. Challenge & Proof of Work
    raw_challenge = await client.request_fabrication_challenge(sku, quantity)
    challenge = FarmaCentralChallengeResponse.model_validate(raw_challenge)

    nonce = await asyncio.to_thread(
        solve_challenge, challenge.prefix, challenge.difficulty
    )

    # 2. Solicitud a Farma Central
    raw_supply = await client.request_products(
        sku=sku,
        quantity=quantity,
        challenge_id=challenge.challenge_id,
        nonce=nonce,
    )
    supply = FarmaCentralSupplyResponse.model_validate(raw_supply)

    # 3. Registrar inicio de corrida y consumir insumos
    run = start_production_run(
        session, requested_at=datetime.now(timezone.utc), expected_sku=sku
    )
    consume_units_for_run(
        session, production_run_id=run.id, input_units_by_lot=input_units_by_lot
    )
    session.commit()

    return run, supply


async def _resolve_input_units_for_sku(
    session: Session, client: FarmaCentralClient, sku: str, quantity: int
) -> dict[UUID, list[Unit]]:
    """Consulta los componentes requeridos para el SKU en el catálogo y localiza

    las unidades disponibles en la zona de acondicionamiento (packaging).
    """
    catalog = await client.get_available_products()
    product_info = next((p for p in catalog if p.get("sku") == sku), None)

    if not product_info or "components" not in product_info:
        return {}

    # 1. Obtener el espacio de packaging desde Farma Central o buscar por code
    spaces = await client.get_spaces()
    packaging_space = next((s for s in spaces if s.get("packaging")), None)
    if not packaging_space:
        return {}

    # 2. Localizar la ubicación local correspondiente por código
    packaging_loc = session.scalar(
        select(Location).where(Location.code == packaging_space["_id"])
    )
    if not packaging_loc:
        return {}

    input_units_by_lot: dict[UUID, list[Unit]] = {}

    # 3. Buscar unidades locales en esa ubicación
    for component in product_info.get("components", []):
        comp_sku = component.get("sku")
        required_qty = component.get("quantity", 1) * quantity

        units = session.scalars(
            select(Unit)
            .join(Lot, Unit.lot_id == Lot.id)
            .join(Product, Lot.product_id == Product.id)
            .where(
                Product.sku == comp_sku,
                Unit.current_location_id == packaging_loc.id,
                Unit.status == "available",
            )
            .limit(required_qty)
        ).all()

        for u in units:
            input_units_by_lot.setdefault(u.lot_id, []).append(u)

    return input_units_by_lot