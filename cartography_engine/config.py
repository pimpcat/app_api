"""Configuración del Cartography Engine (flag + rutas + CRS)."""

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
TEMPLATES_DIR = PACKAGE_DIR / "templates"
ASSETS_DIR = PACKAGE_DIR / "assets"

# Guerrero → UTM zona 14N (métrico) para escalas cartográficas.
DEFAULT_MAP_CRS = "EPSG:32614"
DEFAULT_SOURCE_CRS = "EPSG:4326"


@lru_cache
def get_cartography_settings() -> dict:
    branding_raw = os.getenv("CARTOGRAPHY_BRANDING_FILE", "").strip()
    return {
        "enabled": _env_bool("CARTOGRAPHY_ENGINE_ENABLED", False),
        "templates_dir": Path(
            os.getenv("CARTOGRAPHY_TEMPLATES_DIR", str(TEMPLATES_DIR))
        ).resolve(),
        "assets_dir": Path(
            os.getenv("CARTOGRAPHY_ASSETS_DIR", str(ASSETS_DIR))
        ).resolve(),
        # Opcional: ruta absoluta a branding.json (si vacío → assets/branding.json)
        "branding_file": Path(branding_raw).resolve() if branding_raw else None,
        "map_crs": os.getenv("CARTOGRAPHY_MAP_CRS", DEFAULT_MAP_CRS).strip()
        or DEFAULT_MAP_CRS,
        "source_crs": os.getenv("CARTOGRAPHY_SOURCE_CRS", DEFAULT_SOURCE_CRS).strip()
        or DEFAULT_SOURCE_CRS,
    }


def is_cartography_enabled() -> bool:
    return bool(get_cartography_settings()["enabled"])
