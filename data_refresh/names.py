"""Constantes y nombres seguros para Data Refresh."""

from __future__ import annotations

import re

STAGING_SCHEMA = "atlas_staging"
JOB_ID_RE = re.compile(r"^[a-z0-9]{8,64}$")
TABLE_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")

# Marco / ruteo: no reemplazar desde Data Refresh MVP (riesgo alto).
BLOCKED_REFRESH_TARGETS = frozenset(
    {
        "c_ent",
        "v_c_ent_disp",
        "c_mun",
        "v_c_mun_disp",
        "c_rnc_routing",
        "c_rnc_vertices_pgr",
        "tab_municipal",
        "tab_nacional",
        "c_contexto",
    }
)


def assert_table_name(name: str) -> str:
    n = (name or "").strip().lower()
    if not n or not TABLE_RE.match(n):
        raise ValueError(f"Nombre de tabla inválido: {name!r}")
    if n in BLOCKED_REFRESH_TARGETS:
        raise ValueError(
            f"La tabla {n} está bloqueada para Data Refresh (núcleo / no soportada en MVP)"
        )
    return n


def assert_job_id(job_id: str) -> str:
    j = (job_id or "").strip().lower()
    if not JOB_ID_RE.match(j):
        raise ValueError("job_id inválido")
    return j


def staging_table_name(job_id: str) -> str:
    j = assert_job_id(job_id)
    return f"dr_{j}"[:63]
