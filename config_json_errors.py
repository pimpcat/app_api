"""Errores legibles al parsear JSON de configuración (catálogos, presets).

Los Studios (Visor / Indicators) escriben con json.dump(s) y no producen comas finales.
Los JSONDecodeError suelen venir de edición manual en disco o merges conflictivos.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Tuple


class ConfigJsonSyntaxError(ValueError):
    """JSON inválido en un archivo de configuración."""

    def __init__(self, path: Path, message: str):
        self.path = path
        super().__init__(message)


def format_json_decode_error(path: Path, exc: json.JSONDecodeError) -> str:
    rel = path.name
    line = exc.lineno or "?"
    col = exc.colno or "?"
    hint = (exc.msg or str(exc)).strip()
    return (
        f"JSON inválido en {rel} (línea {line}, columna {col}): {hint}. "
        "Revise comas finales, comillas o llaves sin cerrar. "
        "Los cambios hechos desde Indicators Studio o Visor Studio no generan este tipo de error."
    )


def load_json_object(path: Path) -> Dict[str, Any]:
    """Carga un objeto JSON; mensaje claro si el archivo está corrupto."""
    try:
        with path.open(encoding="utf-8") as fh:
            data = json.load(fh)
    except json.JSONDecodeError as exc:
        raise ConfigJsonSyntaxError(path, format_json_decode_error(path, exc)) from exc
    if not isinstance(data, dict):
        raise ConfigJsonSyntaxError(path, f"{path.name}: se esperaba un objeto JSON en la raíz.")
    return data


def dump_json_object(data: Dict[str, Any]) -> str:
    """Serializa catálogo/config; siempre JSON válido (sin comas finales)."""
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def verify_json_roundtrip(data: Dict[str, Any]) -> None:
    """Comprueba que el objeto se puede serializar y volver a parsear."""
    json.loads(json.dumps(data, ensure_ascii=False))


def check_json_files(paths: List[Path]) -> List[Tuple[str, str]]:
    """Devuelve [(nombre_archivo, mensaje)] por cada JSON inválido."""
    issues: List[Tuple[str, str]] = []
    for path in paths:
        if not path.is_file():
            continue
        try:
            load_json_object(path)
        except ConfigJsonSyntaxError as exc:
            issues.append((path.name, str(exc)))
        except OSError as exc:
            issues.append((path.name, f"No se pudo leer {path.name}: {exc}"))
    return issues
