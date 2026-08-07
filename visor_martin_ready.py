"""Espera a que Martin descubra una tabla PostGIS (sin reiniciar el contenedor)."""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Set

MARTIN_CATALOG_URL = os.getenv("MARTIN_CATALOG_URL", "http://martin:3000/catalog").strip()

# ≥ 2 ciclos de reload_interval (30s) + margen; alinear con nginx proxy_read_timeout ≥ 120s.
DEFAULT_WAIT_TIMEOUT_S = float(os.getenv("MARTIN_WAIT_TIMEOUT_S", "100") or "100")
DEFAULT_WAIT_INTERVAL_S = float(os.getenv("MARTIN_WAIT_INTERVAL_S", "2.5") or "2.5")


def _martin_catalog_layer_ids(catalog: Any) -> List[str]:
    ids: List[str] = []
    if isinstance(catalog, dict):
        tiles = catalog.get("tiles")
        if isinstance(tiles, dict):
            ids.extend(str(k) for k in tiles.keys())
        else:
            ids.extend(str(k) for k in catalog.keys() if k not in ("tiles", "sprites", "fonts"))
    elif isinstance(catalog, list):
        for item in catalog:
            if isinstance(item, dict) and item.get("id"):
                ids.append(str(item["id"]))
    return ids


def normalize_martin_source_id(source_id: str) -> str:
    """
    Martin suele publicar como `{table}` (default). Algunas configs usan
    `{schema}.{table}` o `{schema}.{table}.{column}` — normalizamos al nombre de tabla.
    """
    raw = str(source_id or "").strip()
    if not raw:
        return ""
    # quitar query/fragment por si el id viene de URL
    raw = raw.split("?", 1)[0].split("#", 1)[0]
    parts = [p for p in raw.replace("\\", "/").split("/") if p]
    token = parts[-1] if parts else raw
    # schema.table o schema.table.geom → quedarnos con el segmento c_* / v_c_*
    segs = token.split(".")
    for seg in reversed(segs):
        s = seg.strip().lower()
        if s.startswith("c_") or s.startswith("v_c_"):
            return s
    return segs[-1].strip().lower() if segs else token.lower()


def fetch_martin_catalog() -> Any:
    req = urllib.request.Request(
        MARTIN_CATALOG_URL,
        headers={"User-Agent": "AtlasGro/visor-martin-ready"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_martin_table_ids() -> List[str]:
    try:
        catalog = fetch_martin_catalog()
    except (urllib.error.URLError, json.JSONDecodeError, TimeoutError, OSError) as exc:
        raise RuntimeError(f"MARTIN_UNAVAILABLE:{exc}") from exc
    out: Set[str] = set()
    for lid in _martin_catalog_layer_ids(catalog):
        norm = normalize_martin_source_id(lid)
        if norm.startswith("c_") or norm.startswith("v_c_"):
            out.add(norm)
        # también conservar id crudo en minúsculas por si el cliente compara exacto
        low = str(lid).strip().lower()
        if low.startswith("c_") or low.startswith("v_c_"):
            out.add(low)
    return sorted(out)


def martin_table_id_set() -> Set[str]:
    return {t.lower() for t in fetch_martin_table_ids()}


def martin_has_table(table: str) -> bool:
    name = normalize_martin_source_id(table) or (table or "").strip().lower()
    if not name:
        return False
    return name in martin_table_id_set()


def wait_for_martin_table(
    table: str,
    timeout_s: Optional[float] = None,
    interval_s: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Poll a GET /catalog hasta que la tabla aparezca o expire el timeout.

    Returns:
      in_martin, waited_ms, attempts, martin_available, timed_out
    """
    name = (table or "").strip()
    if not name:
        raise ValueError("INVALID_TABLE")
    want = normalize_martin_source_id(name) or name.lower()

    timeout = DEFAULT_WAIT_TIMEOUT_S if timeout_s is None else float(timeout_s)
    interval = DEFAULT_WAIT_INTERVAL_S if interval_s is None else float(interval_s)
    if timeout < 0:
        timeout = 0.0
    if interval <= 0:
        interval = 0.5

    started = time.monotonic()
    attempts = 0
    last_error: Optional[str] = None
    martin_available = False

    while True:
        attempts += 1
        try:
            ids = martin_table_id_set()
            martin_available = True
            last_error = None
            if want in ids or name.lower() in ids:
                waited_ms = int((time.monotonic() - started) * 1000)
                return {
                    "table": name,
                    "in_martin": True,
                    "waited_ms": waited_ms,
                    "attempts": attempts,
                    "martin_available": True,
                    "timed_out": False,
                }
        except RuntimeError as exc:
            martin_available = False
            last_error = str(exc)

        elapsed = time.monotonic() - started
        if elapsed >= timeout:
            break
        sleep_for = min(interval, max(0.0, timeout - elapsed))
        if sleep_for > 0:
            time.sleep(sleep_for)

    waited_ms = int((time.monotonic() - started) * 1000)
    return {
        "table": name,
        "in_martin": False,
        "waited_ms": waited_ms,
        "attempts": attempts,
        "martin_available": martin_available,
        "timed_out": True,
        "error": last_error,
    }
