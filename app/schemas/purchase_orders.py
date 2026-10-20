from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, Field

PurchaseOrderId = Annotated[str, Field(pattern=r"^[0-9a-fA-F]{24}$")]
PurchaseOrderDecision = Literal["aceptada", "rechazada", "anulada"]


class PurchaseOrderState(StrEnum):
    CREATED = "creada"
    ACCEPTED = "aceptada"
    REJECTED = "rechazada"
    CANCELLED = "anulada"
    EXPIRED = "vencida"
    FULFILLED = "cumplida"


class PurchaseOrderStateChange(BaseModel):
    state: PurchaseOrderState = Field(alias="nombre")
    occurred_at: AwareDatetime = Field(alias="fecha")


class PurchaseOrder(BaseModel):
    """External OC contract, independent of the local sales model."""

    id: PurchaseOrderId
    customer_id: str = Field(alias="cliente", min_length=1)
    supplier_id: str = Field(alias="proveedor", min_length=1)
    sku: str = Field(min_length=1)
    quantity: int = Field(alias="cantidad", gt=0)
    dispatched_quantity: int = Field(alias="despachado", ge=0)
    unit_price: Decimal = Field(alias="precioUnitario", ge=0)
    customer_name: str = Field(default="", alias="nombreCliente")
    state: PurchaseOrderState = Field(alias="estado")
    history: list[PurchaseOrderStateChange] = Field(alias="historial")
    created_at: AwareDatetime = Field(alias="creada")
    updated_at: AwareDatetime = Field(alias="actualizada")
    delivery_deadline: AwareDatetime = Field(alias="vencimiento")
    invoiced: bool = Field(alias="facturado")


class PurchaseOrderStateUpdate(BaseModel):
    state: PurchaseOrderDecision = Field(alias="estado")
