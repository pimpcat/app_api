"""Configuración del API Atlas (variables de entorno / .env del stack)."""

import os
from functools import lru_cache
from urllib.parse import urlparse

from dotenv import load_dotenv

load_dotenv()


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

    return {
        "database_url": db_url,
        "database_name": db_name or database_name_from_url(db_url),
        "schema": os.getenv("ATLAS_SCHEMA", "atlas").strip() or "atlas",
        "cors_origins": [
            o.strip()
            for o in os.getenv(
                "CORS_ORIGINS",
                "http://localhost,http://127.0.0.1,http://localhost:80,http://127.0.0.1:80",
            ).split(",")
            if o.strip()
        ],
    }
