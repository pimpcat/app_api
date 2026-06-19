"""
Resumen de atributos de ruta y métricas de calidad.

Agrega kilómetros por administración, tipo vial, pavimento, etc.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from column_resolver import resolve_column
from ruteo.routing_engine.sql_builder import len_geog_m_sql
from ruteo.routing_engine.restrictions import is_toll_edge_sql
from tables import SCHEMA, T_RNC, qualified
from utils import quote_ident

_ADMIN_ORDER = (
    "Federal",
    "Estatal",
    "Municipal",
    "Otro",
    "Particular",
    "N/D",
    "N/A",
)


def label_or_na(value: Any) -> str:
    s = str(value or "").strip()
    return s if s else "N/D"


def aggregate_km_by(
    rows: List[Dict[str, Any]],
    field: str,
    sort_order: Optional[Tuple[str, ...]] = None,
) -> Dict[str, float]:
    totals: Dict[str, float] = {}
    for row in rows:
        key = label_or_na(row.get(field))
        totals[key] = totals.get(key, 0.0) + float(row.get("longitud_m") or 0) / 1000.0

    if sort_order:
        ordered: Dict[str, float] = {}
        seen = set()
        for key in sort_order:
            if key in totals:
                ordered[key] = round(totals[key], 2)
                seen.add(key)
        for key, val in sorted(totals.items(), key=lambda kv: -kv[1]):
            if key not in seen:
                ordered[key] = round(val, 2)
        return ordered

    return {k: round(v, 2) for k, v in sorted(totals.items(), key=lambda kv: -kv[1])}


def sum_route_length_m(
    conn,
    route_meta: Dict[str, Any],
    edge_ids: List[int],
) -> float:
    """Longitud real de la ruta (metros) para comparar candidatos."""
    if not edge_ids:
        return float("inf")
    if route_meta.get("intelligent_routing"):
        tbl = route_meta["routing_table"]
        sql = f"SELECT COALESCE(SUM(longitud_m), 0) AS m FROM {tbl} WHERE id = ANY(%s)"
    else:
        eid = quote_ident(route_meta["edge_id"])
        geom = quote_ident(route_meta["geom"])
        cost_q = route_meta["cost_sql"]
        tbl = route_meta["table"]
        sql = f"""
            SELECT COALESCE(SUM(
                COALESCE(
                    NULLIF({cost_q}, 0),
                    NULLIF(longitud, 0) * 1000.0,
                    {len_geog_m_sql(geom)}
                )
            ), 0) AS m
              FROM {tbl}
             WHERE {eid} = ANY(%s)
        """
    with conn.cursor() as cur:
        cur.execute(sql, (edge_ids,))
        row = cur.fetchone()
    return float(row["m"] or 0) if row else float("inf")


def build_route_resumen(
    conn,
    route_meta: Dict[str, Any],
    edge_ids: List[int],
    usar_peajes: bool,
) -> Dict[str, Any]:
    """Desglose de km por administración, tipo vial, pavimento, etc."""
    empty: Dict[str, Any] = {
        "usar_peajes": usar_peajes,
        "longitud_m": 0.0,
        "longitud_km": 0.0,
        "tramos_peaje": 0,
        "km_peaje": 0.0,
        "por_administracion": {},
        "por_tipo_vial": {},
        "por_cond_pav": {},
        "por_recubrimiento": {},
        "por_peaje": {},
        "por_pavimentado": {},
    }
    if not edge_ids:
        return empty

    if route_meta.get("intelligent_routing"):
        sql = f"""
            SELECT tipo_vial, administra, cond_pav, recubrimiento, peaje,
                   es_pavimentado, longitud_m
              FROM {route_meta["routing_table"]}
             WHERE id = ANY(%(ids)s)
        """
    else:
        eid = quote_ident(route_meta["edge_id"])
        cols = ["tipo_vial", "administra", "cond_pav", "recubrimiento", "peaje"]
        resolved = [resolve_column(conn, SCHEMA, T_RNC, [c]) for c in cols]
        sel = ", ".join(
            f"{quote_ident(c)} AS {cols[i]}" if c else f"NULL AS {cols[i]}"
            for i, c in enumerate(resolved)
        )
        es_pav = resolve_column(conn, SCHEMA, T_RNC, ["es_pavimentado"])
        len_col = resolve_column(conn, SCHEMA, T_RNC, ["longitud_m"])
        if es_pav:
            sel += f", {quote_ident(es_pav)} AS es_pavimentado"
        else:
            sel += ", NULL AS es_pavimentado"
        if len_col:
            sel += f", {quote_ident(len_col)} AS longitud_m"
        else:
            geom = quote_ident(route_meta["geom"])
            sel += f", ST_Length({geom}::geography) AS longitud_m"
        sql = f"""
            SELECT {sel}
              FROM {qualified(T_RNC)}
             WHERE {eid} = ANY(%(ids)s)
        """

    with conn.cursor() as cur:
        cur.execute(sql, {"ids": edge_ids})
        rows = cur.fetchall()

    total_m = sum(float(r.get("longitud_m") or 0) for r in rows)
    peaje_col = resolve_column(conn, SCHEMA, T_RNC, ["peaje"])
    tipo_col = resolve_column(conn, SCHEMA, T_RNC, ["tipo_vial"])
    nombre_col = resolve_column(conn, SCHEMA, T_RNC, ["nombre"])
    toll_count = 0
    toll_m = 0.0
    if peaje_col:
        peaje_q = quote_ident(peaje_col)
        tipo_q = quote_ident(tipo_col) if tipo_col else "NULL"
        nombre_q = quote_ident(nombre_col) if nombre_col else None
        is_toll = is_toll_edge_sql(peaje_q, tipo_q, nombre_q)
        for r in rows:
            # Evaluación en Python para resumen (evita SQL por fila).
            peaje_val = str(r.get("peaje") or "").strip().upper()
            tipo_val = str(r.get("tipo_vial") or "").strip().lower()
            nom_val = str(r.get("nombre") or "").strip().lower()
            toll = peaje_val in ("SI", "SÍ") or tipo_val == "autopista"
            toll = toll or "cuota" in tipo_val or "autopista" in nom_val
            toll = toll or "cuota" in nom_val or "peaje" in nom_val
            if toll:
                toll_count += 1
                toll_m += float(r.get("longitud_m") or 0)

    return {
        "usar_peajes": usar_peajes,
        "longitud_m": round(total_m, 2),
        "longitud_km": round(total_m / 1000.0, 2),
        "tramos_peaje": toll_count,
        "km_peaje": round(toll_m / 1000.0, 2),
        "por_administracion": aggregate_km_by(rows, "administra", _ADMIN_ORDER),
        "por_tipo_vial": aggregate_km_by(rows, "tipo_vial"),
        "por_cond_pav": aggregate_km_by(rows, "cond_pav"),
        "por_recubrimiento": aggregate_km_by(rows, "recubrimiento"),
        "por_peaje": aggregate_km_by(rows, "peaje"),
        "por_pavimentado": aggregate_km_by(
            rows, "es_pavimentado", ("true", "false", "t", "f")
        ),
    }
