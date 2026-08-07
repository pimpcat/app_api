"""Carga el catálogo data-driven de temas UI (Theme Studio).

Rutas (en orden):
  1. ``$THEME_CATALOG_PATH``
  2. ``<repo>/config/theme/catalog.json``
  3. ``/config/theme/catalog.json`` (Docker)
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from config_json_errors import load_json_object

THEME_CATALOG_ENV = "THEME_CATALOG_PATH"

_cache: Optional[Dict[str, Any]] = None
_cache_mtime: Optional[float] = None
_cache_path: Optional[Path] = None


def _catalog_search_paths() -> List[Path]:
    env = os.getenv(THEME_CATALOG_ENV, "").strip()
    paths: List[Path] = []
    if env:
        paths.append(Path(env))
    here = Path(__file__).resolve().parent
    paths.extend(
        [
            here.parent / "config" / "theme" / "catalog.json",
            Path("/config/theme/catalog.json"),
            here / "config" / "theme" / "catalog.json",
        ]
    )
    return paths


def theme_catalog_path() -> Path:
    for path in _catalog_search_paths():
        try:
            if path.is_file():
                return path
        except OSError:
            continue
    tried = ", ".join(str(p) for p in _catalog_search_paths())
    raise FileNotFoundError(
        f"No se encontró catalog.json de temas. Rutas probadas: {tried}"
    )


def theme_schema_path() -> Path:
    cat = theme_catalog_path()
    sibling = cat.parent / "schema.json"
    if sibling.is_file():
        return sibling
    here = Path(__file__).resolve().parent
    for path in (
        here.parent / "config" / "theme" / "schema.json",
        Path("/config/theme/schema.json"),
    ):
        if path.is_file():
            return path
    raise FileNotFoundError("No se encontró schema.json de temas.")


def theme_defaults_path() -> Path:
    cat = theme_catalog_path()
    sibling = cat.parent / "catalog.defaults.json"
    if sibling.is_file():
        return sibling
    here = Path(__file__).resolve().parent
    for path in (
        here.parent / "config" / "theme" / "catalog.defaults.json",
        Path("/config/theme/catalog.defaults.json"),
    ):
        if path.is_file():
            return path
    raise FileNotFoundError("No se encontró catalog.defaults.json de temas.")


def load_theme_defaults_raw() -> Dict[str, Any]:
    return load_json_object(theme_defaults_path())


def invalidate_theme_catalog_cache() -> None:
    global _cache, _cache_mtime, _cache_path
    _cache = None
    _cache_mtime = None
    _cache_path = None


def load_theme_catalog_raw() -> Dict[str, Any]:
    global _cache, _cache_mtime, _cache_path
    path = theme_catalog_path()
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = None
    if (
        _cache is not None
        and _cache_path == path
        and _cache_mtime is not None
        and mtime == _cache_mtime
    ):
        return _cache
    data = load_json_object(path)
    _cache = data
    _cache_mtime = mtime
    _cache_path = path
    return data


def load_theme_schema_raw() -> Dict[str, Any]:
    return load_json_object(theme_schema_path())


def allowed_token_keys(schema: Optional[Dict[str, Any]] = None) -> set[str]:
    raw = schema or load_theme_schema_raw()
    keys: set[str] = set()
    for group in raw.get("token_groups") or []:
        if not isinstance(group, dict):
            continue
        for tok in group.get("tokens") or []:
            if isinstance(tok, dict) and tok.get("key"):
                keys.add(str(tok["key"]))
    return keys


def public_theme_payload() -> Dict[str, Any]:
    """Respuesta pública para el portal (sin rutas internas)."""
    data = load_theme_catalog_raw()
    return {
        "ok": True,
        "version": data.get("version"),
        "product": data.get("product"),
        "default_theme": data.get("default_theme") or "claro",
        "themes": data.get("themes") or {},
    }
