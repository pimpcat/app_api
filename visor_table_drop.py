"""Borrado seguro de tablas vectoriales c_* / v_c_* del esquema atlas (Visor Studio)."""

from __future__ import annotations

import re
import time
from typing import Any, Dict, FrozenSet, Optional, Set

from config import get_settings
from database import get_db
from visor_martin_ready import martin_table_id_set, normalize_martin_source_id

# Tablas/núcleo del Atlas que nunca se borran desde Studio.
PROTECTED_TABLES: FrozenSet[str] = frozenset(
    {
        "c_ent",
        "v_c_ent_disp",
        "c_mun",
        "v_c_mun_disp",
        "c_l",
        "c_loc_punto",
        "c_col_ase",
        "c_a",
        "c_ar",
        "c_m",
        "c_e",
        "c_rnc",
        "c_rnc_loc",
        "c_rnc_routing",
        "c_rnc_vertices_pgr",
        "c_agua_sanea",
        "c_residuo_solido",
        "c_clues",
        "c_denue",
        "clima",
        "hcorrientes",
        "hcuerpos",
        "curnivel",
        "usosuelo",
    }
)

_TABLE_RE = re.compile(r"^(c_|v_c_)[a-z0-9_]+$", re.IGNORECASE)


def assert_droppable_table_name(table: str) -> str:
    name = normalize_martin_source_id(table) or (table or "").strip().lower()
    if not name or not _TABLE_RE.match(name):
        raise ValueError("INVALID_TABLE:Solo se pueden borrar tablas c_* / v_c_* del esquema atlas")
    if name in PROTECTED_TABLES:
        raise ValueError(
            f"PROTECTED_TABLE:La tabla {name} es del núcleo del Atlas y no se puede borrar desde Studio"
        )
    if not name.replace("_", "").isalnum():
        raise ValueError("INVALID_TABLE")
    return name


def postgis_table_exists(schema: str, table: str) -> bool:
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT 1 FROM information_schema.tables
                 WHERE table_schema = %s AND table_name = %s
                """,
                (schema, table),
            )
            return cur.fetchone() is not None


def drop_postgis_table(table: str) -> Dict[str, Any]:
    """
    DROP TABLE IF EXISTS atlas.<table> CASCADE.
    No toca catalog.json ni Martin (Martin lo quita en reload_interval).
    """
    name = assert_droppable_table_name(table)
    settings = get_settings()
    schema = settings.get("schema") or "atlas"
    existed = postgis_table_exists(schema, name)
    with get_db() as conn:
        with conn.cursor() as cur:
            # Identificadores ya validados (solo [a-z0-9_]).
            cur.execute(f'DROP TABLE IF EXISTS "{schema}"."{name}" CASCADE')
    return {
        "table": name,
        "schema": schema,
        "dropped": True,
        "existed_before": existed,
    }


def wait_for_martin_table_gone(
    table: str,
    timeout_s: float = 60.0,
    interval_s: float = 2.5,
) -> Dict[str, Any]:
    """Espera a que Martin deje de listar la tabla tras un DROP."""
    name = assert_droppable_table_name(table)
    timeout = max(0.0, float(timeout_s))
    interval = max(0.5, float(interval_s))
    started = time.monotonic()
    attempts = 0
    martin_available = False
    last_error: Optional[str] = None

    while True:
        attempts += 1
        try:
            ids: Set[str] = martin_table_id_set()
            martin_available = True
            last_error = None
            if name not in ids:
                return {
                    "table": name,
                    "gone_from_martin": True,
                    "waited_ms": int((time.monotonic() - started) * 1000),
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
        time.sleep(min(interval, max(0.0, timeout - elapsed)))

    return {
        "table": name,
        "gone_from_martin": False,
        "waited_ms": int((time.monotonic() - started) * 1000),
        "attempts": attempts,
        "martin_available": martin_available,
        "timed_out": True,
        "error": last_error,
    }
