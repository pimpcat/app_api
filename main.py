"""
API Atlas Municipal de Guerrero — FastAPI.

Monta todos los endpoints REST del Atlas (app_api/routers/api.py).
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import logging

from config import get_settings
from routers.api import router as api_router

logger = logging.getLogger(__name__)

settings = get_settings()
_docs_enabled = bool(settings.get("enable_api_docs"))

app = FastAPI(
    title="Atlas Gro API",
    description="Backend del Atlas Municipal (PostgreSQL/PostGIS)",
    version="2.0.0",
    docs_url="/docs" if _docs_enabled else None,
    redoc_url="/redoc" if _docs_enabled else None,
    openapi_url="/openapi.json" if _docs_enabled else None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings["cors_origins"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

if not _docs_enabled:
    logger.info("OpenAPI/Swagger deshabilitado (ENABLE_API_DOCS=false).")

app.include_router(api_router)

try:
    from ruteo.router import router as ruteo_router

    app.include_router(ruteo_router)
except Exception as exc:
    logger.warning("Módulo ruteo no disponible (portal sigue operativo): %s", exc)

try:
    from routers.admin_auth import router as admin_auth_router
    from routers.admin_users import router as admin_users_router
    from routers.visor_admin import router as visor_admin_router
    from routers.indicators_admin import router as indicators_admin_router

    app.include_router(admin_auth_router)
    app.include_router(admin_users_router)
    app.include_router(visor_admin_router)
    app.include_router(indicators_admin_router)
except Exception as exc:
    logger.warning("Módulo admin visor/indicadores no disponible: %s", exc)


@app.get("/")
def read_root():
    return {"status": "FastAPI corriendo", "ok": True}
