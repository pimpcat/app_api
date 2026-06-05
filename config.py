"""Configuración del API Atlas (variables de entorno / .env del stack)."""

import os
from functools import lru_cache

from dotenv import load_dotenv

load_dotenv()


@lru_cache
def get_settings():
    db_url = os.getenv("DATABASE_URL", "").strip()
    if not db_url:
        user = os.getenv("DB_USER", "postgres")
        password = os.getenv("DB_PASSWORD", "")
        host = os.getenv("DB_HOST", "db_mapas")
        port = os.getenv("DB_PORT", "5432")
        name = os.getenv("DB_NAME", "atlas")
        db_url = f"postgresql://{user}:{password}@{host}:{port}/{name}"

    return {
        "database_url": db_url,
        "schema": os.getenv("ATLAS_SCHEMA", "atlas"),
        "cors_origins": [
            o.strip()
            for o in os.getenv(
                "CORS_ORIGINS",
                "http://localhost,http://127.0.0.1,http://localhost:80,http://127.0.0.1:80",
            ).split(",")
            if o.strip()
        ],
    }
