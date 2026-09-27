from fastapi import APIRouter

from app.api.routes.health import router as health_router
from app.api.routes.inventory import router as inventory_router
from app.api.routes.products import router as products_router
from app.api.routes.supply import router as supply_router

api_router = APIRouter(prefix="/api")
api_router.include_router(health_router)
api_router.include_router(products_router)
api_router.include_router(inventory_router)
api_router.include_router(supply_router)
