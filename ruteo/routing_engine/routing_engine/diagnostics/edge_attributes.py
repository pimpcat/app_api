"""
Atributos de vialidad (c_rnc) para aristas de diagnóstico de continuidad.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from column_resolver import resolve_column
from ruteo.routing_engine.diagnostics.spatial import (
    SpatialContext,
    length_m_from_routing_join_sql,
)
from tables import SCHEMA, T_RNC, qualified
from utils import quote_ident

# alias de salida → candidatos de columna en c_rnc
_RNC_ATTR_COLUMNS: Dict[str, Tuple[str, ...]] = {
    "nombre": ("nombre",),
    "tipo_vial": ("tipo_vial",),
    "peaje": ("peaje",),
    "administracion": ("administra",),
    "circulacion": ("circulacion", "circula"),
    "condicion": ("condicion",),
    "recubrimiento": ("recubrimiento", "recubri"),
    "superficie": ("cond_pav", "es_pavimentado"),
}

BREAK_EDGE_FIELDS = (
    "gid",
    "nombre",
    "tipo_vial",
    "peaje",
    "longitud",
    "administracion",
    "circulacion",
    "condicion",
    "recubrimiento",
    "superficie",
    "source",
    "target",
    "componente_origen",
    "componente_destino",
    "distancia_entre_componentes",
    "kind",
)


def _text_col_sql(conn, alias: str, candidates: Tuple[str, ...], out_alias: str) -> str:
    col = None
    for name in candidates:
        col = resolve_column(conn, SCHEMA, T_RNC, [name])
        if col:
            break
    if col:
        return f"TRIM(COALESCE(c.{quote_ident(col)}::text, '')) AS {out_alias}"
    return f"''::text AS {out_alias}"


def routing_edge_attr_select_sql(conn, spatial: SpatialContext) -> str:
    """Fragmento SELECT con gid, topología, longitud y atributos de catálogo."""
    edge_id = resolve_column(conn, SCHEMA, T_RNC, ["gid", "id", "ogc_fid"])
    if not edge_id:
        raise ValueError("c_rnc sin columna gid/id")
    eid_q = quote_ident(edge_id)
    rnc = qualified(T_RNC)
    len_m = length_m_from_routing_join_sql(conn, spatial, "c.the_geom")
    geom_json = spatial.as_geojson_sql("c.the_geom")
    attr_parts = [
        _text_col_sql(conn, "c", cols, alias)
        for alias, cols in _RNC_ATTR_COLUMNS.items()
    ]
    attrs = ",\n               ".join(attr_parts)
    return f"""
               r.id::bigint AS gid,
               r.source::int AS source,
               r.target::int AS target,
               {len_m} AS longitud,
               {attrs},
               {geom_json}::text AS geom_json
    """.strip()


def component_pair_distance_map(
    pairs: List[Dict[str, Any]],
) -> Dict[Tuple[int, int], float]:
    out: Dict[Tuple[int, int], float] = {}
    for p in pairs:
        a, b = int(p["component_a"]), int(p["component_b"])
        out[(min(a, b), max(a, b))] = float(p["distance_m"])
    return out


def lookup_component_distance(
    dist_map: Dict[Tuple[int, int], float],
    comp_a: Optional[int],
    comp_b: Optional[int],
) -> Optional[float]:
    if comp_a is None or comp_b is None:
        return None
    return dist_map.get((min(comp_a, comp_b), max(comp_a, comp_b)))


def break_edge_from_row(
    row: Dict[str, Any],
    *,
    vertex_comp: Dict[int, int],
    comp_dist_map: Dict[Tuple[int, int], float],
    kind: str,
    componente_origen: Optional[int] = None,
    componente_destino: Optional[int] = None,
) -> Dict[str, Any]:
    """Registro enriquecido para continuity_gap o candidate_join."""
    source = int(row["source"])
    target = int(row["target"])
    comp_o = componente_origen if componente_origen is not None else vertex_comp.get(source)
    comp_d = (
        componente_destino
        if componente_destino is not None
        else vertex_comp.get(target)
    )
    dist_comp = lookup_component_distance(comp_dist_map, comp_o, comp_d)
    record: Dict[str, Any] = {
        "gid": int(row["gid"]),
        "nombre": row.get("nombre") or "",
        "tipo_vial": row.get("tipo_vial") or "",
        "peaje": row.get("peaje") or "",
        "longitud": round(float(row.get("longitud") or 0), 2),
        "administracion": row.get("administracion") or "",
        "circulacion": row.get("circulacion") or "",
        "condicion": row.get("condicion") or "",
        "recubrimiento": row.get("recubrimiento") or "",
        "superficie": row.get("superficie") or "",
        "source": source,
        "target": target,
        "componente_origen": comp_o,
        "componente_destino": comp_d,
        "distancia_entre_componentes": dist_comp,
        "kind": kind,
    }
    if row.get("geom_json"):
        record["geom_json"] = row["geom_json"]
    return record


def break_edge_for_json(record: Dict[str, Any]) -> Dict[str, Any]:
    """Copia sin geometría para JSON resumido."""
    return {k: v for k, v in record.items() if k != "geom_json"}
