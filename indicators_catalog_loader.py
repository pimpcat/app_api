"""Carga el catálogo data-driven de indicadores del dashboard.

Rutas de búsqueda (en orden):
  1. ``$INDICATORS_CATALOG_PATH`` (env var; en Docker = ``/config/indicators/catalog.json``).
  2. ``<repo>/config/indicators/catalog.json`` (copia canónica en el host).
  3. ``/config/indicators/catalog.json`` (montaje Docker duro).
  4. ``<repo>/htdocs/atlas_gro/config/indicators/catalog.json`` (copia servida al frontend).

Compatible con múltiples workers Gunicorn: cache invalidada por ``mtime`` del archivo.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from config_json_errors import load_json_object

INDICATORS_CATALOG_ENV = "INDICATORS_CATALOG_PATH"


def _catalog_search_paths() -> List[Path]:
    env = os.getenv(INDICATORS_CATALOG_ENV, "").strip()
    paths: List[Path] = []
    if env:
        paths.append(Path(env))
    here = Path(__file__).resolve().parent
    paths.extend(
        [
            here.parent / "config" / "indicators" / "catalog.json",
            Path("/config/indicators/catalog.json"),
            here.parent / "htdocs" / "atlas_gro" / "config" / "indicators" / "catalog.json",
            here / "config" / "indicators" / "catalog.json",
        ]
    )
    return paths


def _resolve_catalog_path() -> Path:
    for path in _catalog_search_paths():
        try:
            if path.is_file():
                return path
        except OSError:
            continue
    tried = ", ".join(str(p) for p in _catalog_search_paths())
    raise FileNotFoundError(
        f"No se encontró catalog.json de indicadores. Rutas probadas: {tried}"
    )


# Fallback si presentation_presets.json no está montado aún.
_FALLBACK_TEMPLATES = {
    "ranking_dual_bars",
    "ranking_with_rates_table",
    "entity_bars_municipal_table",
    "multi_column_table",
    "chartjs_grouped_bars",
    "analfabetismo_composite",
    "national_state_municipal_bars",
    "external_links",
}


def _known_templates() -> set:
    try:
        from presentation_presets_loader import active_preset_ids

        return active_preset_ids()
    except (FileNotFoundError, ValueError, OSError):
        return set(_FALLBACK_TEMPLATES)


def _validate_presentation(ind: Dict[str, Any], source: Path, known: set) -> None:
    """Valida presentation.template y claves requeridas del preset (Fase 7)."""
    iid = ind.get("id")
    pres = ind.get("presentation") or {}
    tpl = pres.get("template")
    if not tpl:
        return
    if tpl not in known:
        raise ValueError(
            f"{source}: indicador '{iid}' tiene template desconocido '{tpl}'"
        )

    try:
        from presentation_presets_loader import preset_by_id, resolve_preset_id

        resolved = resolve_preset_id(tpl) or tpl
        preset = preset_by_id(resolved)
    except (FileNotFoundError, ValueError, OSError):
        preset = None

    if not preset or preset.get("status") in ("alias", "reserved"):
        return

    field_keys = {f.get("key") for f in (ind.get("fields") or []) if f.get("key")}
    cfg = preset.get("config") or {}
    for req in cfg.get("required") or []:
        if req == "bar_metrics":
            metrics = pres.get("bar_metrics") or []
            if not metrics:
                raise ValueError(
                    f"{source}: indicador '{iid}' preset '{resolved}' exige bar_metrics"
                )
            for m in metrics:
                if field_keys and m not in field_keys:
                    raise ValueError(
                        f"{source}: indicador '{iid}' bar_metrics '{m}' no está en fields[]"
                    )
        elif req == "chart_metrics":
            metrics = pres.get("chart_metrics") or []
            if not metrics:
                raise ValueError(
                    f"{source}: indicador '{iid}' preset '{resolved}' exige chart_metrics"
                )
        elif req == "sort_by":
            if not pres.get("sort_by"):
                raise ValueError(
                    f"{source}: indicador '{iid}' preset '{resolved}' exige sort_by"
                )


def _validate_catalog(data: Dict[str, Any], source: Path) -> None:
    """Validación ligera: estructura mínima y coherencia de referencias."""
    if not isinstance(data, dict):
        raise ValueError(f"{source}: raíz debe ser objeto")
    for key in ("version", "groups", "indicators"):
        if key not in data:
            raise ValueError(f"{source}: falta clave requerida '{key}'")
    if not isinstance(data["groups"], list) or not data["groups"]:
        raise ValueError(f"{source}: 'groups' debe ser lista no vacía")
    if not isinstance(data["indicators"], list) or not data["indicators"]:
        raise ValueError(f"{source}: 'indicators' debe ser lista no vacía")

    group_ids = set()
    for group in data["groups"]:
        gid = group.get("id") if isinstance(group, dict) else None
        if not gid:
            raise ValueError(f"{source}: grupo sin 'id'")
        group_ids.add(gid)

    known_templates = _known_templates()
    seen_ids: set = set()
    for ind in data["indicators"]:
        if not isinstance(ind, dict):
            raise ValueError(f"{source}: indicador debe ser objeto")
        iid = ind.get("id")
        if not iid:
            raise ValueError(f"{source}: indicador sin 'id'")
        if iid in seen_ids:
            raise ValueError(f"{source}: id duplicado '{iid}'")
        seen_ids.add(iid)

        gid = ind.get("group_id")
        if gid and gid not in group_ids:
            raise ValueError(
                f"{source}: indicador '{iid}' referencia group_id inexistente '{gid}'"
            )

        _validate_presentation(ind, source, known_templates)


_catalog_cache: Optional[Tuple[float, Dict[str, Any], Path]] = None


def load_indicators_catalog_raw() -> Dict[str, Any]:
    """Lee ``catalog.json`` (con cache por mtime del archivo)."""
    global _catalog_cache
    path = _resolve_catalog_path()
    mtime = path.stat().st_mtime_ns
    if _catalog_cache is not None and _catalog_cache[0] == mtime and _catalog_cache[2] == path:
        return _catalog_cache[1]
    data = load_json_object(path)
    _validate_catalog(data, path)
    _catalog_cache = (mtime, data, path)
    return data


def invalidate_indicators_catalog_cache() -> None:
    """Limpia caché (p. ej. tras editar catalog.json en runtime)."""
    global _catalog_cache
    _catalog_cache = None


def indicators_catalog_path() -> str:
    return str(_resolve_catalog_path())


def indicators_catalog_payload() -> Dict[str, Any]:
    """Catálogo completo para el frontend del dashboard.

    Devuelve el JSON tal cual, tras validación. Los consumidores (Fase 2+)
    pueden derivar payloads más específicos aquí (p. ej. sólo indicadores
    ``enabled``, o filtrados por grupo).
    """
    return load_indicators_catalog_raw()


def enabled_indicators() -> List[Dict[str, Any]]:
    """Lista plana de indicadores con ``enabled: true`` en orden del catálogo."""
    data = load_indicators_catalog_raw()
    return [ind for ind in data.get("indicators", []) if ind.get("enabled")]


def indicator_by_id(indicator_id: str) -> Optional[Dict[str, Any]]:
    if not indicator_id:
        return None
    key = str(indicator_id).strip()
    for ind in load_indicators_catalog_raw().get("indicators", []):
        if ind.get("id") == key:
            return ind
    return None
