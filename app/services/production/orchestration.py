import asyncio
from collections import defaultdict
from datetime import UTC, datetime
from uuid import UUID

from pydantic import BaseModel, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clients.farma_central import FarmaCentralClient
from app.clients.farma_central_exceptions import (
    FarmaCentralHTTPError,
    FarmaCentralInvalidResponseError,
)
from app.clients.farma_central_pow import solve_challenge
from app.models import Location, Lot, Product, ProductionRun, Unit
from app.schemas.farma_central import (
    FarmaCentralChallengeResponse,
    FarmaCentralProduct,
    FarmaCentralSupplyResponse,
)
from app.services.production.consumption import consume_units_for_run
from app.services.production.runs import start_production_run


class ProductionError(Exception):
    """Base error for a production request rejected locally."""


class ProductionProductNotFoundError(ProductionError):
    def __init__(self, sku: str) -> None:
        super().__init__(f"Production formula for {sku} was not found")


class InvalidProductionQuantityError(ProductionError):
    def __init__(self, sku: str, quantity: int, batch_size: int) -> None:
        super().__init__(
            f"Quantity {quantity} for {sku} must be a positive multiple of {batch_size}"
        )


class PackagingLocationNotFoundError(ProductionError):
    pass


class ProductionInputsUnavailableError(ProductionError):
    def __init__(self, missing_by_sku: dict[str, int]) -> None:
        details = ", ".join(
            f"{sku}: {quantity}" for sku, quantity in sorted(missing_by_sku.items())
        )
        super().__init__(f"Missing production inputs ({details})")
        self.missing_by_sku = missing_by_sku


async def produce(
    session: Session,
    *,
    client: FarmaCentralClient,
    sku: str,
    quantity: int,
) -> tuple[ProductionRun, FarmaCentralSupplyResponse]:
    """Produce any packaging SKU and persist the exact consumed units."""
    recipe = await _get_recipe(client, sku)
    _validate_quantity(recipe, quantity)
    input_units_by_lot = await _resolve_input_units(
        session,
        client,
        recipe=recipe,
        quantity=quantity,
    )

    supply = await _request_production(client, sku=sku, quantity=quantity)
    _validate_external_response(supply.sku, supply.quantity, sku, quantity)

    try:
        run = start_production_run(
            session,
            requested_at=datetime.now(UTC),
            expected_sku=sku,
            expected_quantity=quantity,
            available_at=supply.available_at,
        )
        consume_units_for_run(
            session,
            production_run_id=run.id,
            input_units_by_lot=input_units_by_lot,
        )
        session.commit()
    except Exception:
        session.rollback()
        raise

    return run, supply


async def _request_production(
    client: FarmaCentralClient,
    *,
    sku: str,
    quantity: int,
) -> FarmaCentralSupplyResponse:
    for attempt in range(2):
        challenge = _parse_response(
            await client.request_fabrication_challenge(sku, quantity),
            FarmaCentralChallengeResponse,
        )
        _validate_external_response(challenge.sku, challenge.quantity, sku, quantity)
        _ensure_not_expired(challenge.expires_at)

        nonce = await asyncio.to_thread(
            solve_challenge,
            challenge.prefix,
            challenge.difficulty,
        )
        _ensure_not_expired(challenge.expires_at)

        try:
            payload = await client.request_products(
                sku=sku,
                quantity=quantity,
                challenge_id=challenge.challenge_id,
                nonce=nonce,
            )
        except FarmaCentralHTTPError as exc:
            if exc.status_code not in {400, 409} or attempt == 1:
                raise
            continue
        return _parse_response(payload, FarmaCentralSupplyResponse)

    raise AssertionError("Production challenge retry loop exhausted")


async def _get_recipe(client: FarmaCentralClient, sku: str) -> FarmaCentralProduct:
    payload = await client.get_available_products()
    if not isinstance(payload, list):
        raise FarmaCentralInvalidResponseError("Invalid products: expected a list")

    try:
        products = [FarmaCentralProduct.model_validate(item) for item in payload]
    except ValidationError as exc:
        raise FarmaCentralInvalidResponseError("Invalid product catalog") from exc

    recipe = next((product for product in products if product.sku == sku), None)
    if recipe is None or recipe.production.at != "packaging" or not recipe.components:
        raise ProductionProductNotFoundError(sku)
    return recipe


def _validate_quantity(recipe: FarmaCentralProduct, quantity: int) -> None:
    batch_size = recipe.production.batch
    if quantity <= 0 or quantity % batch_size != 0:
        raise InvalidProductionQuantityError(recipe.sku, quantity, batch_size)


async def _resolve_input_units(
    session: Session,
    client: FarmaCentralClient,
    *,
    recipe: FarmaCentralProduct,
    quantity: int,
) -> dict[UUID, list[Unit]]:
    spaces = await client.get_spaces()
    if not isinstance(spaces, list):
        raise FarmaCentralInvalidResponseError("Invalid spaces: expected a list")
    packaging_space = next(
        (space for space in spaces if space.get("packaging") is True), None
    )
    if packaging_space is None:
        raise PackagingLocationNotFoundError("Packaging space was not found")

    packaging_id = packaging_space.get("_id")
    packaging_location = session.scalar(
        select(Location).where(Location.code == packaging_id)
    )
    if packaging_location is None:
        raise PackagingLocationNotFoundError(
            "Packaging space has not been synchronized locally"
        )

    selected_by_lot: dict[UUID, list[Unit]] = defaultdict(list)
    missing_by_sku: dict[str, int] = {}
    now = datetime.now(UTC)

    for component in recipe.components:
        required_quantity = component.required_per_unit * quantity
        units = list(
            session.scalars(
                select(Unit)
                .join(Lot, Unit.lot_id == Lot.id)
                .join(Product, Lot.product_id == Product.id)
                .where(
                    Product.sku == component.sku,
                    Unit.current_location_id == packaging_location.id,
                    Unit.status == "available",
                    Unit.effective_expires_at > now,
                )
                .order_by(Unit.effective_expires_at, Unit.id)
                .limit(required_quantity)
                .with_for_update()
            )
        )
        if len(units) != required_quantity:
            missing_by_sku[component.sku] = required_quantity - len(units)
            continue
        for unit in units:
            selected_by_lot[unit.lot_id].append(unit)

    if missing_by_sku:
        raise ProductionInputsUnavailableError(missing_by_sku)
    return dict(selected_by_lot)


def _parse_response[Schema: BaseModel](payload: object, schema: type[Schema]) -> Schema:
    try:
        return schema.model_validate(payload)
    except ValidationError as exc:
        raise FarmaCentralInvalidResponseError(
            "Farma Central returned an invalid production response"
        ) from exc


def _validate_external_response(
    actual_sku: str,
    actual_quantity: int,
    expected_sku: str,
    expected_quantity: int,
) -> None:
    if actual_sku != expected_sku or actual_quantity != expected_quantity:
        raise FarmaCentralInvalidResponseError(
            "Farma Central returned a response for another production request"
        )


def _ensure_not_expired(expires_at: datetime) -> None:
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if expires_at <= datetime.now(UTC):
        raise ProductionError("The production challenge expired")
