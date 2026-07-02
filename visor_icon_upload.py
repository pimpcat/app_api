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


_DANGEROUS_SVG_TAGS = ("script", "foreignobject", "iframe", "embed", "object", "use", "audio", "video")
_EVENT_HANDLER_RE = re.compile(r"\s+on[a-z]+\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)", re.IGNORECASE)
_JS_URL_RE = re.compile(
    r"(href|xlink:href)\s*=\s*(\"|\')\s*javascript:[^\"\']*(\"|\')",
    re.IGNORECASE,
)


def _sanitize_svg(content: bytes) -> bytes:
    """Elimina scripts, handlers y referencias javascript: antes de publicar el SVG."""
    text = content.decode("utf-8", errors="replace")
    for tag in _DANGEROUS_SVG_TAGS:
        text = re.sub(
            rf"<{tag}\b[^>]*>.*?</{tag}>",
            "",
            text,
            flags=re.IGNORECASE | re.DOTALL,
        )
        text = re.sub(rf"<{tag}\b[^>]*/>", "", text, flags=re.IGNORECASE)
    text = _EVENT_HANDLER_RE.sub("", text)
    text = _JS_URL_RE.sub(r'\1=""', text)
    return text.encode("utf-8")


def _normalize_svg_for_map(content: bytes) -> bytes:
    """Quita metadatos potrace y deja que viewBox defina la proporción."""
    text = content.decode("utf-8", errors="replace")
    text = re.sub(r"<!DOCTYPE[^>]*>", "", text, flags=re.IGNORECASE)
    text = re.sub(r"<\?xml[^?]*\?>", "", text, flags=re.IGNORECASE)
    text = re.sub(r'\s+width="[^"]*"', "", text, count=1)
    text = re.sub(r'\s+height="[^"]*"', "", text, count=1)
    if "preserveAspectRatio" not in text:
        text = re.sub(r"<svg\b", '<svg preserveAspectRatio="xMidYMid meet"', text, count=1)
    return text.strip().encode("utf-8")


def _svg_viewbox_aspect(text: str) -> Optional[float]:
    m = re.search(
        r'viewBox\s*=\s*["\']?\s*[\d.]+\s+[\d.]+\s+([\d.]+)\s+([\d.]+)',
        text,
        flags=re.IGNORECASE,
    )
    if not m:
        return None
    w, h = float(m.group(1)), float(m.group(2))
    if w <= 0 or h <= 0:
        return None
    return w / h


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
    sanitized = _sanitize_svg(svg_content)
    normalized = _normalize_svg_for_map(sanitized)
    text_norm = normalized.decode("utf-8", errors="replace")
    aspect = _svg_viewbox_aspect(text_norm)
    is_tall_pin = aspect is not None and aspect < 0.92
    prev = icons.get(key) or {}
    next_version = int(prev.get("version") or 0) + 1 if key in icons else 1
    icons[key] = {
        "id": map_id,
        "file": file_name,
        "label": label_s,
        "size_profile": "pin_zoom" if is_tall_pin else "standard_zoom",
        "logical_px": 32,
        "max_scale": 2.63,
        "supersample": 4,
        "texture_anchor": "bottom" if is_tall_pin else "center",
        "version": next_version,
    }

    map_dir = _icons_map_dir()
    map_dir.mkdir(parents=True, exist_ok=True)
    (map_dir / file_name).write_bytes(normalized)
    json_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    return {
        "icon_key": key,
        "label": label_s,
        "file": file_name,
        "id": map_id,
        "version": next_version,
        "texture_anchor": icons[key]["texture_anchor"],
    }
