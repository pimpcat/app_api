"""Configuración del API Atlas (variables de entorno / .env del stack)."""

import os
from functools import lru_cache
from urllib.parse import urlparse

from dotenv import load_dotenv

load_dotenv()


def default_cors_origins() -> list[str]:
    """Orígenes del portal vía Nginx (PORT_NGINX en .env, p. ej. 850)."""
    origins = [
        "http://localhost",
        "http://127.0.0.1",
        "http://localhost:80",
        "http://127.0.0.1:80",
    ]
    port = os.getenv("PORT_NGINX", "850").strip()
    if port and port not in ("80", "443"):
        origins.append(f"http://localhost:{port}")
        origins.append(f"http://127.0.0.1:{port}")
    return origins


def database_name_from_url(db_url: str) -> str:
    """Nombre de la base PostgreSQL en la URL (sin credenciales)."""
    if not db_url:
        return ""
    path = urlparse(db_url).path or ""
    return path.lstrip("/").split("/")[0] or ""


@lru_cache
def get_settings():
    db_url = os.getenv("DATABASE_URL", "").strip()
    db_name = os.getenv("DB_NAME", "").strip()
    if not db_url:
        user = os.getenv("DB_USER", "postgres")
        password = os.getenv("DB_PASSWORD", "")
        host = os.getenv("DB_HOST", "db_mapas")
        port = os.getenv("DB_PORT", "5432")
        name = db_name or "atlas"
        db_url = f"postgresql://{user}:{password}@{host}:{port}/{name}"
    elif not db_name:
        db_name = database_name_from_url(db_url)

    carto_url = os.getenv("CARTOGRAPHY_DATABASE_URL", "").strip()
    carto_name = os.getenv("CARTOGRAPHY_DB_NAME", "GroSIG_Cartography").strip() or (
        "GroSIG_Cartography"
    )
    if not carto_url:
        user = os.getenv("DB_USER", "postgres")
        password = os.getenv("DB_PASSWORD", "")
        host = os.getenv("DB_HOST", "db_mapas")
        port = os.getenv("DB_PORT", "5432")
        # Misma instancia Postgres; BD distinta del Atlas.
        carto_url = f"postgresql://{user}:{password}@{host}:{port}/{carto_name}"

    return {
        "database_url": db_url,
        "database_name": db_name or database_name_from_url(db_url),
        "schema": os.getenv("ATLAS_SCHEMA", "atlas").strip() or "atlas",
        "cartography_database_url": carto_url,
        "cartography_database_name": carto_name
        or database_name_from_url(carto_url)
        or "GroSIG_Cartography",
        "jwt_secret": os.getenv("JWT_SECRET", "").strip(),
        "jwt_expire_hours": int(os.getenv("JWT_EXPIRE_HOURS", "8") or "8"),
        "cors_origins": _parse_cors_origins(),
        "enable_api_docs": os.getenv("ENABLE_API_DOCS", "false").strip().lower()
        in ("1", "true", "yes", "on"),
        "cartography_engine_enabled": os.getenv(
            "CARTOGRAPHY_ENGINE_ENABLED", "false"
        )
        .strip()
        .lower()
        in ("1", "true", "yes", "on"),
        "geography_context_enabled": os.getenv(
            "GEOGRAPHY_CONTEXT_ENABLED", "true"
        )
        .strip()
        .lower()
        in ("1", "true", "yes", "on"),
    }


def _parse_cors_origins() -> list[str]:
    raw = os.getenv("CORS_ORIGINS", "").strip()
    if raw:
        items = [o.strip() for o in raw.split(",") if o.strip()]
    else:
        items = default_cors_origins()
    # Sin duplicados, orden estable
    seen: set[str] = set()
    out: list[str] = []
    for origin in items:
        if origin not in seen:
            seen.add(origin)
            out.append(origin)
    return out
