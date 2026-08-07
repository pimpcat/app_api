"""Carga y validación del catálogo Geography Context."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from config_json_errors import load_json_object

GEOGRAPHY_CATALOG_ENV = "GEOGRAPHY_CATALOG_PATH"
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

_cache: Dict[str, Any] = {"path": None, "mtime": None, "data": None}


def _catalog_search_paths() -> List[Path]:
    env = os.getenv(GEOGRAPHY_CATALOG_ENV, "").strip()
    paths: List[Path] = []
    if env:
        paths.append(Path(env))
    here = Path(__file__).resolve().parent
    paths.extend(
        [
            here.parent.parent / "config" / "geography" / "catalog.json",
            Path("/config/geography/catalog.json"),
            here.parent / "config" / "geography" / "catalog.json",
        ]
    )
    return paths


def geography_catalog_path() -> Path:
    for path in _catalog_search_paths():
        try:
            if path.is_file():
                return path
        except OSError:
            continue
    tried = ", ".join(str(p) for p in _catalog_search_paths())
    raise FileNotFoundError(
        f"No se encontró catalog.json de Geography Context. Rutas: {tried}"
    )


def invalidate_geography_catalog_cache() -> None:
    _cache["path"] = None
    _cache["mtime"] = None
    _cache["data"] = None


def assert_sql_ident(name: str, *, label: str = "identificador") -> str:
    value = (name or "").strip()
    if not value or not _IDENT_RE.match(value):
        raise ValueError(f"{label} inválido: {name!r}")
    return value


def _normalize_tab(raw: Dict[str, Any], defaults: Dict[str, Any], index: int) -> Dict[str, Any]:
    tid = str(raw.get("id") or "").strip()
    if not tid or not _IDENT_RE.match(tid):
        raise ValueError(f"tabs[{index}]: id inválido")
    label = str(raw.get("label") or tid).strip()
    text = raw.get("text") if isinstance(raw.get("text"), dict) else {}
    table = assert_sql_ident(
        str(text.get("table") or "").strip(), label=f"tabs[{tid}].text.table"
    )
    field = assert_sql_ident(
        str(text.get("field") or "").strip(), label=f"tabs[{tid}].text.field"
    )
    key_col = str(text.get("key_column") or defaults.get("key_column") or "cve_mun").strip()
    key_col = assert_sql_ident(key_col, label=f"tabs[{tid}].text.key_column")
    layers_raw = raw.get("layers") or []
    if not isinstance(layers_raw, list):
        raise ValueError(f"tabs[{tid}]: layers debe ser lista")
    layers = [str(x).strip() for x in layers_raw if str(x).strip()]
    try:
        order = int(raw.get("order", (index + 1) * 10))
    except (TypeError, ValueError):
        order = (index + 1) * 10
    return {
        "id": tid,
        "label": label,
        "enabled": raw.get("enabled", True) is not False,
        "order": order,
        "text": {
            "table": table,
            "field": field,
            "key_column": key_col,
        },
        "layers": layers,
        "show_legend": bool(raw.get("show_legend", False)),
    }


def validate_geography_catalog(data: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(data, dict):
        raise ValueError("El catálogo debe ser un objeto JSON")
    version = data.get("version", 1)
    try:
        version = int(version)
    except (TypeError, ValueError) as exc:
        raise ValueError("version inválida") from exc

    menu_in = data.get("menu") if isinstance(data.get("menu"), dict) else {}
    menu = {
        "id": str(menu_in.get("id") or "geo_datos_geo").strip() or "geo_datos_geo",
        "label": str(menu_in.get("label") or "Datos Geográficos").strip()
        or "Datos Geográficos",
        "subtitle": str(menu_in.get("subtitle") or "").strip(),
        "section_id": str(menu_in.get("section_id") or "geo").strip() or "geo",
        "section_label": str(menu_in.get("section_label") or "Geografía").strip()
        or "Geografía",
    }

    layout_in = data.get("layout") if isinstance(data.get("layout"), dict) else {}
    layout = {
        "macro_map": layout_in.get("macro_map", True) is not False,
        "detail_map_lock": layout_in.get("detail_map_lock", True) is not False,
    }

    defaults_in = data.get("defaults") if isinstance(data.get("defaults"), dict) else {}
    defaults = {
        "key_column": assert_sql_ident(
            str(defaults_in.get("key_column") or "cve_mun").strip(),
            label="defaults.key_column",
        ),
        "ent_column": assert_sql_ident(
            str(defaults_in.get("ent_column") or "ent").strip(),
            label="defaults.ent_column",
        ),
        "ent_value": str(defaults_in.get("ent_value") or "12").strip() or "12",
    }

    tabs_in = data.get("tabs")
    if not isinstance(tabs_in, list) or not tabs_in:
        raise ValueError("Se requiere al menos una pestaña en tabs")

    tabs: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for i, raw in enumerate(tabs_in):
        if not isinstance(raw, dict):
            raise ValueError(f"tabs[{i}] debe ser objeto")
        tab = _normalize_tab(raw, defaults, i)
        if tab["id"] in seen:
            raise ValueError(f"id de pestaña duplicado: {tab['id']}")
        seen.add(tab["id"])
        tabs.append(tab)

    tabs.sort(key=lambda t: (t.get("order", 0), t.get("label") or ""))

    out: Dict[str, Any] = {
        "version": version,
        "menu": menu,
        "layout": layout,
        "defaults": defaults,
        "tabs": tabs,
    }
    if data.get("description"):
        out["description"] = str(data["description"])
    return out


def load_geography_catalog_raw() -> Dict[str, Any]:
    path = geography_catalog_path()
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = None
    if (
        _cache["data"] is not None
        and _cache["path"] == str(path)
        and _cache["mtime"] == mtime
    ):
        return _cache["data"]
    data = load_json_object(path)
    validated = validate_geography_catalog(data)
    _cache["path"] = str(path)
    _cache["mtime"] = mtime
    _cache["data"] = validated
    return validated


def enabled_tabs(catalog: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    cat = catalog or load_geography_catalog_raw()
    return [t for t in (cat.get("tabs") or []) if t.get("enabled", True) is not False]


def catalog_status() -> Dict[str, Any]:
    try:
        path = geography_catalog_path()
        cat = load_geography_catalog_raw()
        tabs = enabled_tabs(cat)
        return {
            "ok": True,
            "path": str(path),
            "tabs_count": len(tabs),
            "tabs_total": len(cat.get("tabs") or []),
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
