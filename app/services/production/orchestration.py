import asyncio
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy.orm import Session

from app.clients.farma_central import FarmaCentralClient
from app.clients.farma_central_pow import solve_challenge
from app.models import ProductionRun, Unit
from app.schemas.farma_central import FarmaCentralChallengeResponse, FarmaCentralSupplyResponse
from app.services.production.consumption import consume_units_for_run
from app.services.production.runs import start_production_run


async def produce(
    session: Session,
    *,
    client: FarmaCentralClient,
    sku: str,
    quantity: int,
    input_units_by_lot: dict[UUID, list[Unit]],
) -> tuple[ProductionRun, FarmaCentralSupplyResponse]:
    """Orquesta el flujo completo del Paso 12: challenge -> PoW -> solicitud
    de producción -> registro local de la corrida y consumo de insumos.

    Devuelve también la respuesta de Farma Central con `available_at`, para
    que quien orquesta la sincronización sepa cuándo esperar el lote de salida.
    """
    raw_challenge = await client.request_fabrication_challenge(sku, quantity)
    challenge = FarmaCentralChallengeResponse.model_validate(raw_challenge)

    nonce = await asyncio.to_thread(
        solve_challenge, challenge.prefix, challenge.difficulty
    )

    raw_supply = await client.request_products(
        sku=sku,
        quantity=quantity,
        challenge_id=challenge.challenge_id,
        nonce=nonce,
    )
    supply = FarmaCentralSupplyResponse.model_validate(raw_supply)

    run = start_production_run(session, requested_at=datetime.now(timezone.utc), expected_sku=sku)
    consume_units_for_run(
        session, production_run_id=run.id, input_units_by_lot=input_units_by_lot
    )
    session.commit()

    return run, supply