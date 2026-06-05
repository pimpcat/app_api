"""
API Atlas Municipal de Guerrero — FastAPI.

Monta todos los endpoints REST del Atlas (app_api/routers/api.py).
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from config import get_settings
from routers.api import router as api_router

settings = get_settings()

app = FastAPI(
    title="Atlas Gro API",
    description="Backend del Atlas Municipal (PostgreSQL/PostGIS)",
    version="2.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings["cors_origins"] + ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router)


@app.get("/")
def read_root():
    return {"status": "FastAPI corriendo", "ok": True}
