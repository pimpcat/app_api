"""Identidad institucional configurable (logos + textos).

Los nombres de archivo y la línea de marca salen de ``assets/branding.json``
(o ``CARTOGRAPHY_BRANDING_FILE``), no de literales en el código del plugin.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from cartography_engine.config import get_cartography_settings

_DEFAULT_BRANDING: dict[str, Any] = {
    "brand_line": "CESIEG Guerrero",
    "engine_line": "GroSIG Cartography Engine",
    "logos": ["cesieg.png"],
    "fallback_labels": ["CESIEG"],
}


@lru_cache
def get_branding() -> dict[str, Any]:
    """Carga branding desde JSON; si falta o es inválido, usa defaults del plugin."""
    settings = get_cartography_settings()
    assets = Path(settings["assets_dir"])
    override = settings.get("branding_file")
    path = Path(override).resolve() if override else (assets / "branding.json")

    data: dict[str, Any] = dict(_DEFAULT_BRANDING)
    if path.is_file():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                if isinstance(raw.get("brand_line"), str) and raw["brand_line"].strip():
                    data["brand_line"] = raw["brand_line"].strip()
                if isinstance(raw.get("engine_line"), str) and raw["engine_line"].strip():
                    data["engine_line"] = raw["engine_line"].strip()
                logos = raw.get("logos")
                if isinstance(logos, list) and logos:
                    data["logos"] = [str(x).strip() for x in logos if str(x).strip()]
                labels = raw.get("fallback_labels")
                if isinstance(labels, list) and labels:
                    data["fallback_labels"] = [
                        str(x).strip() for x in labels if str(x).strip()
                    ]
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            pass

    data["logos_dir"] = assets / "logos"
    return data


def resolve_logo_paths() -> list[Path]:
    """Rutas de logos existentes, en el orden declarado en branding."""
    branding = get_branding()
    logos_dir: Path = branding["logos_dir"]
    search_dirs = [logos_dir]
    # Catálogos alternos (host / montajes Docker)
    settings = get_cartography_settings()
    assets = Path(settings["assets_dir"])
    for extra in (
        assets / "logos",
        Path(__file__).resolve().parent / "assets" / "logos",
        Path("/app/cartography_engine/assets/logos"),
        Path("/app/app_api/cartography_engine/assets/logos"),
    ):
        if extra not in search_dirs:
            search_dirs.append(extra)

    found: list[Path] = []
    for name in branding.get("logos") or []:
        stem = Path(name).stem
        for folder in search_dirs:
            if not folder.is_dir():
                continue
            path = folder / name
            if path.is_file():
                found.append(path)
                break
            for ext in (".png", ".jpg", ".jpeg", ".webp"):
                alt = folder / f"{stem}{ext}"
                if alt.is_file():
                    found.append(alt)
                    break
            else:
                continue
            break
    return found


def clear_branding_cache() -> None:
    get_branding.cache_clear()
