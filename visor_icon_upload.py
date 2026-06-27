"""Registro de iconos SVG custom en icons.json + assets/icons/map."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Dict, Optional

ICONS_JSON_PATHS = (
    Path("/config/visor/icons.json"),
    Path(__file__).resolve().parent.parent / "config" / "visor" / "icons.json",
)


def _icons_json_path() -> Path:
    for candidate in ICONS_JSON_PATHS:
        if candidate.is_file():
            return candidate
    return ICONS_JSON_PATHS[0]


def _icons_map_dir() -> Path:
    env = os.getenv("VISOR_ICONS_MAP_DIR", "").strip()
    if env:
        return Path(env)
    here = Path(__file__).resolve().parent.parent
    return here / "htdocs" / "atlas_gro" / "assets" / "icons" / "map"


def _validate_icon_key(key: str) -> str:
    k = (key or "").strip()
    if not re.fullmatch(r"[a-z][a-z0-9_]{1,48}", k):
        raise ValueError("INVALID_ICON_KEY")
    return k


def _validate_svg(content: bytes) -> None:
    if not content or len(content) > 512_000:
        raise ValueError("INVALID_SVG_SIZE")
    head = content[:4096].lstrip().decode("utf-8", errors="ignore")
    if "<svg" not in head:
        raise ValueError("INVALID_SVG")


def register_custom_icon(
    icon_key: str,
    label: str,
    svg_content: bytes,
    overwrite: bool = False,
) -> Dict[str, Any]:
    key = _validate_icon_key(icon_key)
    _validate_svg(svg_content)
    label_s = (label or key).strip()[:120] or key

    json_path = _icons_json_path()
    if not json_path.is_file():
        raise RuntimeError("ICONS_JSON_NOT_FOUND")

    data = json.loads(json_path.read_text(encoding="utf-8"))
    icons = data.setdefault("icons", {})
    if key in icons and not overwrite:
        raise ValueError(f"ICON_EXISTS:{key}")

    file_name = key.replace("_", "-") + ".svg"
    map_id = f"atlas-{key.replace('_', '-')}"
    icons[key] = {
        "id": map_id,
        "file": file_name,
        "label": label_s,
        "size_profile": "standard_zoom",
        "logical_px": 32,
        "max_scale": 2.63,
        "version": 1,
    }

    map_dir = _icons_map_dir()
    map_dir.mkdir(parents=True, exist_ok=True)
    (map_dir / file_name).write_bytes(svg_content)
    json_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    return {"icon_key": key, "label": label_s, "file": file_name, "id": map_id}
