"""
Resolución de metadatos de tablas RNC (localidades, red, vértices).

Usa ``resolve_column`` para portabilidad entre versiones de la RNC.
Toda consulta a ``information_schema`` pasa por aquí o por ``column_resolver``.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from column_resolver import resolve_column
from ruteo.routing_engine.errors import RuteoError
from tables import SCHEMA, T_RNC, T_RNC_ROUTING, qualified
from utils import quote_ident

T_RNC_LOC = "c_rnc_loc"
T_RNC_VERTICES = "c_rnc_vertices_pgr"

_ROUTING_PEAJE_SYNC_DONE = False
_ROUTING_NOMBRE_SYNC_DONE = False


def _resolve_rnc_columns(conn) -> Dict[str, Optional[str]]:
    """Columnas opcionales de ``c_rnc`` / ``c_rnc_routing`` usadas en ruteo."""
    optional = [
        "longitud_m", "longitud", "velocidad_kmh", "velocidad", "peaje",
        "tipo_vial", "nombre", "cond_pav", "recubrimiento", "recubri",
        "condicion", "circulacion", "circula", "carriles", "estatus",
        "es_pavimentado", "jerarquia_tipo_vial", "jerarquia_admin", "administra",
    ]
    out: Dict[str, Optional[str]] = {}
    for name in optional:
        out[name] = resolve_column(conn, SCHEMA, T_RNC, [name])
    # Alias canónicos para ColumnSet
    out["longitud_m"] = out.get("longitud_m") or out.get("longitud")
    out["velocidad_kmh"] = out.get("velocidad_kmh") or out.get("velocidad")
    out["recubrimiento"] = out.get("recubrimiento") or out.get("recubri")
    out["circulacion"] = out.get("circulacion") or out.get("circula")
    out["cost_col"] = resolve_column(
        conn, SCHEMA, T_RNC, ["cost", "length", "longitud", "distancia"]
    )
    out["reverse_cost_col"] = resolve_column(
        conn, SCHEMA, T_RNC, ["reverse_cost", "rcost"]
    )
    return out


def routing_table_ready(conn) -> bool:
    """True si existe ``atlas.c_rnc_routing``."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT 1 FROM information_schema.tables
             WHERE table_schema = %s AND table_name = %s LIMIT 1
            """,
            (SCHEMA, T_RNC_ROUTING),
        )
        return cur.fetchone() is not None


def routing_table_intelligent(conn) -> bool:
    """True si ``c_rnc_routing`` tiene columnas de ruteo inteligente."""
    if not routing_table_ready(conn):
        return False
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT 1 FROM information_schema.columns
             WHERE table_schema = %s AND table_name = %s
               AND column_name = 'cost_sin_peaje' LIMIT 1
            """,
            (SCHEMA, T_RNC_ROUTING),
        )
        return cur.fetchone() is not None


def routing_has_nombre_column(conn) -> bool:
    return bool(resolve_column(conn, SCHEMA, T_RNC_ROUTING, ["nombre"]))


def ensure_routing_peaje_synced(conn) -> None:
    """Copia peaje desde c_rnc → c_rnc_routing si hay desfase (una vez por proceso)."""
    global _ROUTING_PEAJE_SYNC_DONE
    if _ROUTING_PEAJE_SYNC_DONE:
        return
    if not routing_table_ready(conn):
        _ROUTING_PEAJE_SYNC_DONE = True
        return
    peaje_r = resolve_column(conn, SCHEMA, T_RNC_ROUTING, ["peaje"])
    peaje_c = resolve_column(conn, SCHEMA, T_RNC, ["peaje"])
    edge_id = resolve_column(conn, SCHEMA, T_RNC, ["gid", "id", "ogc_fid"])
    if not all([peaje_r, peaje_c, edge_id]):
        _ROUTING_PEAJE_SYNC_DONE = True
        return

    eid_q = quote_ident(edge_id)
    peaje_rq = quote_ident(peaje_r)
    peaje_cq = quote_ident(peaje_c)
    rnc = qualified(T_RNC)
    routing = qualified(T_RNC_ROUTING)

    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT 1 FROM {routing} r
              JOIN {rnc} c ON c.{eid_q} = r.id
             WHERE UPPER(TRIM(COALESCE(c.{peaje_cq}::text, 'No'))) IN ('SI', 'SÍ')
               AND UPPER(TRIM(COALESCE(r.{peaje_rq}::text, 'No'))) NOT IN ('SI', 'SÍ')
             LIMIT 1
            """
        )
        if cur.fetchone() is None:
            _ROUTING_PEAJE_SYNC_DONE = True
            return
        cur.execute(
            f"""
            UPDATE {routing} r
               SET {peaje_rq} = TRIM(c.{peaje_cq}::text)
              FROM {rnc} c
             WHERE c.{eid_q} = r.id
               AND TRIM(COALESCE(r.{peaje_rq}::text, ''))
                   IS DISTINCT FROM TRIM(COALESCE(c.{peaje_cq}::text, ''))
            """
        )
        conn.commit()
    _ROUTING_PEAJE_SYNC_DONE = True


def ensure_routing_nombre_synced(conn) -> None:
    """Copia nombre desde c_rnc → c_rnc_routing (solo UPDATE; una vez por proceso)."""
    global _ROUTING_NOMBRE_SYNC_DONE
    if _ROUTING_NOMBRE_SYNC_DONE:
        return
    if not routing_table_ready(conn):
        _ROUTING_NOMBRE_SYNC_DONE = True
        return
    from column_resolver import clear_column_cache

    if not routing_has_nombre_column(conn):
        _ROUTING_NOMBRE_SYNC_DONE = True
        return
    clear_column_cache(SCHEMA, T_RNC_ROUTING)
    edge_id = resolve_column(conn, SCHEMA, T_RNC, ["gid", "id", "ogc_fid"])
    nombre_c = resolve_column(conn, SCHEMA, T_RNC, ["nombre"])
    if not edge_id or not nombre_c:
        _ROUTING_NOMBRE_SYNC_DONE = True
        return

    routing = qualified(T_RNC_ROUTING)
    rnc = qualified(T_RNC)
    eid_q = quote_ident(edge_id)
    nombre_cq = quote_ident(nombre_c)

    with conn.cursor() as cur:
        cur.execute(f"SELECT 1 FROM {routing} WHERE nombre IS NULL LIMIT 1")
        if cur.fetchone() is None:
            _ROUTING_NOMBRE_SYNC_DONE = True
            return
        cur.execute("SET LOCAL statement_timeout TO 120000")
        cur.execute(
            f"""
            UPDATE {routing} r
               SET nombre = TRIM(c.{nombre_cq}::text)
              FROM {rnc} c
             WHERE c.{eid_q} = r.id AND r.nombre IS NULL
            """
        )
        conn.commit()
    _ROUTING_NOMBRE_SYNC_DONE = True


def ensure_routing_tables_synced(conn) -> None:
    """Sincronización ligera c_rnc → c_rnc_routing antes de calcular rutas."""
    if not routing_table_ready(conn):
        return
    ensure_routing_peaje_synced(conn)
    if routing_table_intelligent(conn) and routing_has_nombre_column(conn):
        ensure_routing_nombre_synced(conn)


def routing_meta(conn) -> Dict[str, Any]:
    """
    Metadatos de red: columnas resueltas, SQL base y flags de tabla routing.

    No precomputa todos los grafos posibles; ``graph.build_graph_sql`` lo hace
    bajo demanda según ``RoutingOptions``.
    """
    edge_id = resolve_column(conn, SCHEMA, T_RNC, ["gid", "id", "ogc_fid"])
    source = resolve_column(conn, SCHEMA, T_RNC, ["source"])
    target = resolve_column(conn, SCHEMA, T_RNC, ["target"])
    geom = resolve_column(conn, SCHEMA, T_RNC, ["the_geom", "geom", "wkb_geometry"])
    columns = _resolve_rnc_columns(conn)

    if not all([edge_id, source, target, geom]):
        raise RuteoError(
            "RNC_SCHEMA",
            "La tabla c_rnc no tiene las columnas mínimas (id/gid, source, target, the_geom).",
        )

    cost_col = columns.get("cost_col")
    reverse_col = columns.get("reverse_cost_col")
    geom_q = quote_ident(geom)

    if cost_col:
        cost_sql = quote_ident(cost_col)
    else:
        cost_sql = f"ST_Length({geom_q})"

    if reverse_col:
        reverse_sql = quote_ident(reverse_col)
    else:
        reverse_sql = cost_sql

    uses_routing = routing_table_ready(conn)
    intelligent = uses_routing and routing_table_intelligent(conn)
    routing_tbl = qualified(T_RNC_ROUTING) if uses_routing else None

    eid_q = quote_ident(edge_id)
    src_q = quote_ident(source)
    tgt_q = quote_ident(target)
    where = [f"{src_q} IS NOT NULL", f"{tgt_q} IS NOT NULL"]
    if cost_col:
        where.append(f"{cost_sql} > 0")

    edges_sql_fallback = (
        f"SELECT {eid_q} AS id, {src_q} AS source, {tgt_q} AS target, "
        f"{cost_sql} AS cost, {reverse_sql} AS reverse_cost "
        f"FROM {qualified(T_RNC)} WHERE {' AND '.join(where)}"
    )

    edges_sql_peajes = (
        f"SELECT id, source, target, cost, reverse_cost FROM {routing_tbl}"
        if routing_tbl
        else edges_sql_fallback
    )

    meta: Dict[str, Any] = {
        "edge_id": edge_id,
        "source": source,
        "target": target,
        "cost_sql": cost_sql,
        "reverse_sql": reverse_sql,
        "cost_is_column": bool(cost_col),
        "reverse_is_column": bool(reverse_col),
        "geom": geom,
        "table": qualified(T_RNC),
        "edges_sql": edges_sql_peajes,
        "edges_sql_peajes": edges_sql_peajes,
        "uses_routing_table": uses_routing,
        "intelligent_routing": intelligent,
        "routing_has_nombre": intelligent and routing_has_nombre_column(conn),
        "routing_table": routing_tbl,
        "columns": columns,
    }

    if routing_tbl:
        from ruteo.routing_engine.graph import build_graph_sql
        from ruteo.routing_engine.types import CostProfile, GraphVariant, RoutingOptions

        def _edge_sql(profile: CostProfile, variant: GraphVariant) -> str:
            opts = RoutingOptions(
                evitar_peajes=True,
                usar_costos_materializados=profile == CostProfile.MATERIALIZED,
                cost_profile=profile,
                graph_variant=variant,
            )
            return build_graph_sql(meta, opts)

        if intelligent:
            meta["edges_sql_sin_peaje"] = _edge_sql(
                CostProfile.LEGACY_SIN_PEAJE, GraphVariant.SIN_PEAJE_FULL
            )
            meta["edges_sql_sin_peaje_paved"] = _edge_sql(
                CostProfile.LEGACY_SIN_PEAJE, GraphVariant.SIN_PEAJE_PAVED
            )
            meta["edges_sql_sin_peaje_no_toll"] = _edge_sql(
                CostProfile.LEGACY_SIN_PEAJE, GraphVariant.SIN_PEAJE_NO_TOLL
            )
        else:
            meta["edges_sql_sin_peaje"] = _edge_sql(
                CostProfile.MATERIALIZED, GraphVariant.SIN_PEAJE_FULL
            )

    return meta


def vertices_meta(conn) -> Dict[str, str]:
    """Metadatos de ``c_rnc_vertices_pgr``."""
    vid = resolve_column(conn, SCHEMA, T_RNC_VERTICES, ["id", "gid"])
    vgeom = resolve_column(conn, SCHEMA, T_RNC_VERTICES, ["the_geom", "geom"])
    if not all([vid, vgeom]):
        raise RuteoError(
            "VERTICES_SCHEMA",
            "La tabla c_rnc_vertices_pgr no tiene columnas id y geometría.",
        )
    return {"id": vid, "geom": vgeom, "table": qualified(T_RNC_VERTICES)}


def loc_meta(conn) -> Dict[str, str]:
    """Metadatos de ``c_rnc_loc``."""
    cvegeo = resolve_column(conn, SCHEMA, T_RNC_LOC, ["cvegeo"])
    nombre = resolve_column(conn, SCHEMA, T_RNC_LOC, ["nombre", "nom_loc", "nomgeo"])
    geom = resolve_column(conn, SCHEMA, T_RNC_LOC, ["the_geom", "geom"])
    cve_mun = resolve_column(conn, SCHEMA, T_RNC_LOC, ["cve_mun"])
    node = resolve_column(
        conn,
        SCHEMA,
        T_RNC_LOC,
        ["node", "id_node", "vertex_id", "source", "target", "id_vertice", "vertice"],
    )
    if not all([cvegeo, nombre, geom]):
        raise RuteoError(
            "LOC_SCHEMA",
            "La tabla c_rnc_loc no tiene cvegeo, nombre o geometría.",
        )
    return {
        "cvegeo": cvegeo,
        "nombre": nombre,
        "geom": geom,
        "cve_mun": cve_mun,
        "node": node,
        "table": qualified(T_RNC_LOC),
    }
