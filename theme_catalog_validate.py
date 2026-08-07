"""Validación del catálogo de temas UI."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Tuple

from theme_catalog_loader import allowed_token_keys, load_theme_schema_raw

_HEX_RE = re.compile(r"^#([0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")
_RGB_FUNC_RE = re.compile(
    r"^rgba?\(\s*[\d.]+\s*,\s*[\d.]+\s*,\s*[\d.]+(?:\s*,\s*[\d.]+\s*)?\)$",
    re.I,
)
_RGB_TRIPLET_RE = re.compile(r"^\s*\d{1,3}\s*,\s*\d{1,3}\s*,\s*\d{1,3}\s*$")
_THEME_IDS = ("claro", "oscuro")


def _token_format(schema: Dict[str, Any], key: str) -> str:
    for group in schema.get("token_groups") or []:
        if not isinstance(group, dict):
            continue
        for tok in group.get("tokens") or []:
            if isinstance(tok, dict) and tok.get("key") == key:
                return str(tok.get("format") or "color")
    return "color"


def _validate_value(fmt: str, value: Any, key: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"TOKEN_EMPTY:{key}")
    v = value.strip()
    if fmt == "rgb_triplet":
        if not _RGB_TRIPLET_RE.match(v):
            raise ValueError(f"TOKEN_FORMAT:{key}: se espera r, g, b (ej. 0, 51, 102)")
        parts = [int(p.strip()) for p in v.split(",")]
        if any(p < 0 or p > 255 for p in parts):
            raise ValueError(f"TOKEN_FORMAT:{key}: componentes RGB deben estar en 0–255")
        return
    # color: hex, rgb(), rgba(), o var() limitado no — solo colores literales
    if _HEX_RE.match(v) or _RGB_FUNC_RE.match(v):
        return
    raise ValueError(
        f"TOKEN_FORMAT:{key}: use #rgb/#rrggbb o rgb()/rgba()"
    )


def validate_theme_catalog(data: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(data, dict):
        raise ValueError("CATALOG_INVALID: raíz debe ser objeto")
    version = data.get("version")
    if version is not None and int(version) < 1:
        raise ValueError("CATALOG_INVALID: version")
    default = str(data.get("default_theme") or "claro").strip().lower()
    if default not in _THEME_IDS:
        raise ValueError("CATALOG_INVALID: default_theme debe ser claro u oscuro")

    themes = data.get("themes")
    if not isinstance(themes, dict) or not themes:
        raise ValueError("CATALOG_INVALID: themes requerido")
    for tid in _THEME_IDS:
        if tid not in themes:
            raise ValueError(f"CATALOG_INVALID: falta tema '{tid}'")

    schema = load_theme_schema_raw()
    allowed = allowed_token_keys(schema)
    if not allowed:
        raise ValueError("SCHEMA_EMPTY")

    for tid, theme in themes.items():
        if tid not in _THEME_IDS:
            raise ValueError(f"THEME_UNKNOWN:{tid}")
        if not isinstance(theme, dict):
            raise ValueError(f"THEME_INVALID:{tid}")
        tokens = theme.get("tokens")
        if not isinstance(tokens, dict) or not tokens:
            raise ValueError(f"THEME_TOKENS:{tid}")
        unknown = [k for k in tokens.keys() if k not in allowed]
        if unknown:
            raise ValueError(
                f"TOKEN_UNKNOWN:{tid}:{', '.join(sorted(unknown)[:8])}"
            )
        for key, val in tokens.items():
            fmt = _token_format(schema, key)
            _validate_value(fmt, val, key)

    out = dict(data)
    out["default_theme"] = default
    out["version"] = int(data.get("version") or 1)
    out["product"] = str(data.get("product") or "GroSIG")
    return out
