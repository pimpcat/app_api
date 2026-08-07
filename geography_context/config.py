"""Configuración de GroSIG Geography Context (flag + ruta de catálogo)."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


PACKAGE_DIR = Path(__file__).resolve().parent
DEFAULT_CATALOG = Path("/config/geography/catalog.json")


@lru_cache
def get_geography_settings() -> dict:
    catalog_raw = os.getenv("GEOGRAPHY_CATALOG_PATH", "").strip()
    return {
        "enabled": _env_bool("GEOGRAPHY_CONTEXT_ENABLED", True),
        "catalog_path": Path(catalog_raw).resolve() if catalog_raw else None,
    }


def is_geography_context_enabled() -> bool:
    return bool(get_geography_settings()["enabled"])


def clear_geography_settings_cache() -> None:
    get_geography_settings.cache_clear()
