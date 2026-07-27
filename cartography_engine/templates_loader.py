"""Carga de plantillas JSON data-driven."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from cartography_engine.config import get_cartography_settings
from cartography_engine.models import CartographyError


def list_template_ids() -> list[str]:
    root: Path = get_cartography_settings()["templates_dir"]
    if not root.is_dir():
        return []
    return sorted(p.stem for p in root.glob("*.json"))


def load_template(template_id: str) -> dict[str, Any]:
    safe = str(template_id or "").strip()
    if not safe or "/" in safe or "\\" in safe or ".." in safe:
        raise CartographyError("INVALID_TEMPLATE", f"Plantilla inválida: {template_id}")

    root: Path = get_cartography_settings()["templates_dir"]
    path = root / f"{safe}.json"
    if not path.is_file():
        raise CartographyError(
            "TEMPLATE_NOT_FOUND",
            f"Plantilla no encontrada: {safe}",
            status_code=404,
        )
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except json.JSONDecodeError as exc:
        raise CartographyError(
            "TEMPLATE_INVALID_JSON",
            f"JSON inválido en plantilla {safe}: {exc}",
        ) from exc
    if not isinstance(data, dict):
        raise CartographyError("TEMPLATE_INVALID", f"Plantilla {safe} debe ser un objeto JSON")
    data.setdefault("id", safe)
    return data
