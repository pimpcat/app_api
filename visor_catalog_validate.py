"""Validación de entradas de capa para el asistente admin (Fase 1)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

PHASE1_PRESETS: Dict[str, Dict[str, Any]] = {
    "point_default": {"geometry": "point", "type": "circle"},
    "point_symbol": {"geometry": "point", "type": "symbol", "needs_icon": True},
    "line_simple": {"geometry": "line", "type": "line"},
    "line_outline": {"geometry": "line", "type": "line", "line_stack": True},
    "polygon_outline": {"geometry": "polygon", "type": "line"},
    "polygon_outline_detail": {"geometry": "polygon", "type": "line", "line_stack": True},
    "polygon_fill": {"geometry": "polygon", "type": "fill"},
}

PRESET_LABELS = {
    "point_default": "Punto (círculo)",
    "point_symbol": "Punto con icono",
    "line_simple": "Línea simple",
    "line_outline": "Línea con contorno",
    "polygon_outline": "Polígono contorno",
    "polygon_outline_detail": "Polígono doble línea",
    "polygon_fill": "Polígono relleno",
    "point_by_attribute": "Punto por atributo",
    "line_by_attribute": "Línea por atributo",
    "polygon_by_attribute": "Polígono por atributo",
}

# Presets avanzados / compuestos — no se ofrecen en Visor Studio (Fase 3).
ADMIN_PRESET_EXCLUDE = frozenset(
    {
        "rnc_tiered",
        "polygon_outline_detail",
        "point_symbol_by_attribute",
    }
)


def _presets_dir() -> Path:
    here = Path(__file__).resolve().parent.parent
    for candidate in (
        Path("/config/visor/presets"),
        here / "config" / "visor" / "presets",
    ):
        if candidate.is_dir():
            return candidate
    return here / "config" / "visor" / "presets"


def _read_preset_json(path: Path) -> Optional[Dict[str, Any]]:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def preset_meta_index() -> Dict[str, Dict[str, Any]]:
    """Índice id → metadatos de presets disponibles en Visor Studio."""
    index: Dict[str, Dict[str, Any]] = {}
    preset_dir = _presets_dir()
    paths = sorted(preset_dir.glob("*.json")) if preset_dir.is_dir() else []
    for path in paths:
        data = _read_preset_json(path)
        if not data:
            continue
        pid = str(data.get("id") or path.stem).strip()
        if not pid or pid in ADMIN_PRESET_EXCLUDE or pid in index:
            continue
        phase1 = PHASE1_PRESETS.get(pid, {})
        geom = data.get("geometry") or phase1.get("geometry")
        is_attr = bool((data.get("attribute") or {}).get("paint"))
        is_symbol = data.get("type") == "symbol"
        index[pid] = {
            "id": pid,
            "label": data.get("label") or PRESET_LABELS.get(pid, pid),
            "geometry": geom,
            "by_attribute": is_attr,
            "needs_icon": bool(phase1.get("needs_icon") or (is_symbol and not is_attr)),
            "type": data.get("type") or phase1.get("type"),
        }
    for pid, phase1 in PHASE1_PRESETS.items():
        if pid in index or pid in ADMIN_PRESET_EXCLUDE:
            continue
        path = preset_dir / f"{pid}.json"
        data = _read_preset_json(path) or {}
        index[pid] = {
            "id": pid,
            "label": data.get("label") or PRESET_LABELS.get(pid, pid),
            "geometry": data.get("geometry") or phase1.get("geometry"),
            "by_attribute": bool((data.get("attribute") or {}).get("paint")),
            "needs_icon": bool(phase1.get("needs_icon")),
            "type": data.get("type") or phase1.get("type"),
        }
    return index


def load_preset_meta() -> List[Dict[str, Any]]:
    index = preset_meta_index()
    order = [
        "point_default",
        "point_symbol",
        "point_by_attribute",
        "line_simple",
        "line_outline",
        "line_by_attribute",
        "polygon_outline",
        "polygon_fill",
        "polygon_by_attribute",
    ]
    out: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for pid in order:
        if pid in index:
            out.append(index[pid])
            seen.add(pid)
    for pid in sorted(index.keys()):
        if pid not in seen:
            out.append(index[pid])
    return out


def preset_meta(preset_id: str) -> Optional[Dict[str, Any]]:
    return preset_meta_index().get((preset_id or "").strip())


def load_icons_meta() -> List[Dict[str, str]]:
    for base in (Path("/config/visor"), Path(__file__).resolve().parent.parent / "config" / "visor"):
        path = base / "icons.json"
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                return []
            icons = data.get("icons") or {}
            return [
                {"key": key, "label": (val.get("label") or key)}
                for key, val in icons.items()
                if isinstance(val, dict)
            ]
    return []


def slug_layer_id(value: str) -> str:
    return _slug(value)


def validate_layer_payload(payload: Dict[str, Any], icon_keys: Optional[Sequence[str]] = None) -> List[str]:
    warnings: List[str] = []
    layer_id = slug_layer_id(payload.get("layer_id") or "")
    if not layer_id:
        warnings.append("layer_id requerido (solo letras, números y _)")
    label = (payload.get("label") or "").strip()
    if not label:
        warnings.append("label requerido")
    group_id = (payload.get("group_id") or "").strip()
    if not group_id:
        warnings.append("group_id requerido")
    geometry = (payload.get("geometry") or "").strip().lower()
    preset = (payload.get("style_preset") or "").strip()
    meta = preset_meta(preset)
    if not meta:
        warnings.append(f"style_preset '{preset}' no disponible en Visor Studio")
    elif geometry and meta.get("geometry") and meta.get("geometry") != geometry:
        warnings.append(f"geometry '{geometry}' no coincide con preset '{preset}'")
    table = ((payload.get("data") or {}).get("table") or payload.get("table") or "").strip()
    if not table:
        warnings.append("data.table requerido")
    style = payload.get("style") or {}
    if meta and meta.get("needs_icon"):
        icon_key = (style.get("icon_key") or "").strip()
        if not icon_key:
            warnings.append("style.icon_key requerido para point_symbol")
        elif icon_keys is not None and icon_key not in icon_keys:
            warnings.append(f"icon_key '{icon_key}' no está en icons.json")
    if meta and meta.get("by_attribute"):
        field = (style.get("field") or "").strip()
        classes = style.get("classes")
        if not field:
            warnings.append("style.field requerido para preset por atributo")
        if not isinstance(classes, list) or not classes:
            warnings.append("style.classes requiere al menos una clase valor/color")
    data = payload.get("data") or {}
    denue = payload.get("denue") or {}
    if str(table).lower() == "c_denue":
        codigos = denue.get("codigo_act") or (data.get("filter") or {}).get("codigo_act")
        if not codigos:
            warnings.append("denue.codigo_act requerido para tabla c_denue (códigos SCIAN)")
    caps = payload.get("capabilities") or {}
    export = caps.get("export")
    if export is not None and not isinstance(export, list):
        warnings.append("capabilities.export debe ser array")
    identify = payload.get("identify") or {}
    if isinstance(identify, dict):
        fields = identify.get("fields")
        if fields is not None and not isinstance(fields, list):
            warnings.append("identify.fields debe ser array")
    export_cols = data.get("export_columns")
    if export_cols is not None and not isinstance(export_cols, list):
        warnings.append("data.export_columns debe ser array")
    labels = payload.get("labels")
    if isinstance(labels, dict) and labels.get("field"):
        minz = labels.get("minzoom")
        if minz is not None:
            try:
                float(minz)
            except (TypeError, ValueError):
                warnings.append("labels.minzoom debe ser numérico")
    return warnings


def validate_layer_update_payload(payload: Dict[str, Any], layer_id: str, icons: Optional[Sequence[str]] = None) -> List[str]:
    warnings = validate_layer_payload(payload, icons)
    lid = slug_layer_id(layer_id)
    if payload.get("layer_id") and slug_layer_id(payload.get("layer_id")) != lid:
        warnings.append("No se puede cambiar el id de capa al editar")
    return warnings


def build_layer_entry(payload: Dict[str, Any]) -> Dict[str, Any]:
    layer_id = slug_layer_id(payload.get("layer_id") or "")
    preset = payload.get("style_preset")
    pmeta = preset_meta(str(preset or "")) or PHASE1_PRESETS.get(preset) or {}
    geometry = payload.get("geometry") or pmeta.get("geometry") or "point"
    style = dict(payload.get("style") or {})
    data = dict(payload.get("data") or {})
    payload_mun = payload.get("mun_filter")
    data_mun = data.get("mun_filter")
    if payload_mun is False or data_mun is False:
        data["mun_filter"] = False
    else:
        data.setdefault("mun_filter", data_mun or payload_mun or "cve_mun")

    export_columns = data.pop("export_columns", None)
    if export_columns:
        data["export"] = {"mode": "columns", "columns": list(export_columns)}
    elif isinstance(data.get("export"), dict) and data["export"].get("columns"):
        data["export"] = dict(data["export"])

    denue = payload.get("denue") or {}
    table_lower = str(data.get("table") or "").strip().lower()
    codigos = denue.get("codigo_act") or (data.get("filter") or {}).get("codigo_act")
    if table_lower == "c_denue" and codigos:
        data["filter"] = {"codigo_act": [int(c) for c in codigos]}
        data.setdefault("gid_table", "c_denue")

    caps = dict(payload.get("capabilities") or {})
    caps.setdefault("export", [])
    caps.setdefault("tabular", False)
    caps.setdefault("spatial_analysis", False)

    entry: Dict[str, Any] = {
        "label": payload.get("label"),
        "geometry": geometry,
        "renderer": "overlay",
        "style_preset": preset,
        "data": data,
        "capabilities": caps,
    }
    if style:
        entry["style"] = style
    identify = payload.get("identify")
    if table_lower == "c_denue" and denue.get("use_template", True):
        entry["identify"] = {
            "template": "denue",
            "title": (identify or {}).get("title") or payload.get("label") or layer_id,
        }
    elif identify:
        fields = identify.get("fields")
        if isinstance(fields, list):
            norm_fields: List[Any] = []
            for f in fields:
                if isinstance(f, str) and f.strip():
                    col = f.strip()
                    norm_fields.append({"column": col, "label": col})
                elif isinstance(f, dict):
                    col = f.get("column") or f.get("field") or f.get("name")
                    if col and str(col).strip():
                        col_s = str(col).strip()
                        label = f.get("label")
                        norm_fields.append(
                            {
                                "column": col_s,
                                "label": str(label).strip() if label else col_s,
                            }
                        )
            title = identify.get("title")
            if title and str(title).strip():
                identify = {**identify, "title": str(title).strip(), "fields": norm_fields}
            else:
                identify = {**identify, "fields": norm_fields}
            entry["identify"] = identify
    labels = payload.get("labels")
    if isinstance(labels, dict) and labels.get("field"):
        field = str(labels["field"]).strip()
        if field:
            geom = entry.get("geometry") or geometry
            default_minz = 16.0 if geom == "line" else 14.0
            try:
                minz = float(labels["minzoom"]) if labels.get("minzoom") is not None else default_minz
            except (TypeError, ValueError):
                minz = default_minz
            label_block: Dict[str, Any] = {"field": field, "minzoom": minz}
            if geom == "point" and labels.get("above_icon") is not False:
                label_block["above_icon"] = True
            color = labels.get("color")
            if color and str(color).strip():
                label_block["color"] = str(color).strip()
            entry["labels"] = label_block
    legend = payload.get("legend")
    if legend:
        entry["legend"] = legend
    if payload.get("overlay_key"):
        entry["overlay_key"] = payload["overlay_key"]
    if payload.get("checkbox_id"):
        entry["checkbox_id"] = payload["checkbox_id"]
    return entry


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (value or "").strip().lower()).strip("_")
