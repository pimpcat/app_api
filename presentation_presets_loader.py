"""Carga presentation_presets.json (Fase 7).

Contrato de estilos de gráficas/tablas del dashboard. El runtime de Fase 8
implementará un renderer por preset; aquí solo se valida y se sirve el catálogo.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from config_json_errors import load_json_object

PRESETS_ENV = "INDICATORS_PRESENTATION_PRESETS_PATH"


def _presets_search_paths() -> List[Path]:
    env = os.getenv(PRESETS_ENV, "").strip()
    paths: List[Path] = []
    if env:
        paths.append(Path(env))
    # Mismo directorio que catalog.json de indicadores
    catalog_env = os.getenv("INDICATORS_CATALOG_PATH", "").strip()
    if catalog_env:
        paths.append(Path(catalog_env).resolve().parent / "presentation_presets.json")
    here = Path(__file__).resolve().parent
    # Preferir montaje Docker / htdocs (fuente de runtime Fase 1–7).
    paths.extend(
        [
            Path("/config/indicators/presentation_presets.json"),
            here.parent
            / "htdocs"
            / "atlas_gro"
            / "config"
            / "indicators"
            / "presentation_presets.json",
            here.parent / "config" / "indicators" / "presentation_presets.json",
            here / "config" / "indicators" / "presentation_presets.json",
        ]
    )
    return paths


def _resolve_presets_path() -> Path:
    for path in _presets_search_paths():
        try:
            if path.is_file():
                return path
        except OSError:
            continue
    tried = ", ".join(str(p) for p in _presets_search_paths())
    raise FileNotFoundError(
        f"No se encontró presentation_presets.json. Rutas probadas: {tried}"
    )


_presets_cache: Optional[Tuple[float, Dict[str, Any], Path]] = None


def load_presentation_presets_raw() -> Dict[str, Any]:
    global _presets_cache
    path = _resolve_presets_path()
    mtime = path.stat().st_mtime_ns
    if (
        _presets_cache is not None
        and _presets_cache[0] == mtime
        and _presets_cache[2] == path
    ):
        return _presets_cache[1]
    data = load_json_object(path)
    if not isinstance(data, dict) or "presets" not in data:
        raise ValueError(f"{path}: falta lista 'presets'")
    if not isinstance(data["presets"], list) or not data["presets"]:
        raise ValueError(f"{path}: 'presets' debe ser lista no vacía")
    seen: Set[str] = set()
    for p in data["presets"]:
        if not isinstance(p, dict) or not p.get("id"):
            raise ValueError(f"{path}: preset sin 'id'")
        pid = p["id"]
        if pid in seen:
            raise ValueError(f"{path}: preset id duplicado '{pid}'")
        seen.add(pid)
    _presets_cache = (mtime, data, path)
    return data


def invalidate_presentation_presets_cache() -> None:
    global _presets_cache
    _presets_cache = None


def presentation_presets_path() -> str:
    return str(_resolve_presets_path())


def presentation_presets_payload() -> Dict[str, Any]:
    return load_presentation_presets_raw()


def active_preset_ids() -> Set[str]:
    """Ids de presets activos (implemented + catalog_only) + alias/reserved ids."""
    data = load_presentation_presets_raw()
    ids: Set[str] = set()
    for p in data.get("presets") or []:
        ids.add(p["id"])
    for r in data.get("reserved") or []:
        if r.get("id"):
            ids.add(r["id"])
    return ids


def preset_by_id(preset_id: str) -> Optional[Dict[str, Any]]:
    if not preset_id:
        return None
    data = load_presentation_presets_raw()
    for p in data.get("presets") or []:
        if p.get("id") == preset_id:
            return p
    for r in data.get("reserved") or []:
        if r.get("id") == preset_id:
            return r
    return None


def resolve_preset_id(template: str) -> Optional[str]:
    """Resuelve alias (p. ej. national_state_municipal_bars → chartjs_grouped_bars)."""
    entry = preset_by_id(template)
    if not entry:
        return None
    if entry.get("status") == "alias" and entry.get("alias_of"):
        return str(entry["alias_of"])
    if entry.get("status") == "reserved":
        return template
    return entry.get("id")
