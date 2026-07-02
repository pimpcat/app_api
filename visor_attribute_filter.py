"""Filtro data-driven por campo/atributo (catálogo data.filter.field + values)."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from utils import quote_ident

_IDENT_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


def parse_attribute_filter(data: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Ignora filter.codigo_act (DENUE). Devuelve {field, values} o None."""
    if not isinstance(data, dict):
        return None
    filt = data.get("filter") or {}
    if not isinstance(filt, dict) or filt.get("codigo_act"):
        return None
    field = str(filt.get("field") or "").strip()
    values = filt.get("values")
    if not field or not _IDENT_RE.match(field):
        return None
    if not isinstance(values, list):
        return None
    cleaned = [str(v).strip() for v in values if str(v).strip()]
    if not cleaned:
        return None
    return {"field": field, "values": cleaned}


def attribute_filter_where_sql(attribute_filter: Dict[str, Any], *, alias: str = "") -> str:
    field = str(attribute_filter.get("field") or "").strip()
    values = attribute_filter.get("values") or []
    if not field or not _IDENT_RE.match(field):
        return ""
    cleaned = [str(v).strip().replace("'", "''") for v in values if str(v).strip()]
    if not cleaned:
        return ""
    col = quote_ident(field)
    prefix = f"{alias}." if alias else ""
    in_list = ", ".join(f"'{v}'" for v in cleaned)
    return f"TRIM(BOTH FROM {prefix}{col}::text) IN ({in_list})"
