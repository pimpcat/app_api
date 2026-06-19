"""
Diagnóstico topológico del subgrafo del corredor OD (estrategia sin peajes).

No ejecuta Dijkstra ni modifica el endpoint de ruteo: solo describe por qué
el tronco del corredor puede quedar partido en varias componentes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

from column_resolver import resolve_column
from ruteo.routing_engine.diagnostics.edge_attributes import (
    break_edge_for_json,
    break_edge_from_row,
    component_pair_distance_map,
    routing_edge_attr_select_sql,
)
from ruteo.routing_engine.diagnostics.spatial import (
    SpatialContext,
    detect_spatial_context_conn,
    length_m_from_rnc_sql,
    length_m_from_routing_join_sql,
)
from ruteo.routing_engine.legacy_od_filters import not_toll_on_alias_sql, od_direct_nombre_lc_values
from ruteo.routing_engine.strategies.od_corridor import (
    CORRIDOR_BRIDGE_WAVES,
    corridor_anchor_candidates,
    fetch_corridor_bridge_edge_ids,
    fetch_corridor_named_edge_ids,
    snap_vertex_to_corridor,
)
from ruteo.routing_engine.types import RouteContext
from tables import SCHEMA, T_RNC, T_RNC_ROUTING, qualified
from utils import quote_ident

T_RNC_VERTICES = "c_rnc_vertices_pgr"


class _UnionFind:
    def __init__(self) -> None:
        self._parent: Dict[int, int] = {}
        self._rank: Dict[int, int] = {}

    def add(self, v: int) -> None:
        if v not in self._parent:
            self._parent[v] = v
            self._rank[v] = 0

    def find(self, v: int) -> int:
        self.add(v)
        while self._parent[v] != v:
            self._parent[v] = self._parent[self._parent[v]]
            v = self._parent[v]
        return v

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        if self._rank[ra] < self._rank[rb]:
            ra, rb = rb, ra
        self._parent[rb] = ra
        if self._rank[ra] == self._rank[rb]:
            self._rank[ra] += 1

    def components(self) -> Dict[int, List[int]]:
        groups: Dict[int, List[int]] = {}
        for v in self._parent:
            root = self.find(v)
            groups.setdefault(root, []).append(v)
        for root in groups:
            groups[root].sort()
        return groups


COMPONENT_COLORS = [
    "#e41a1c",
    "#377eb8",
    "#4daf4a",
    "#984ea3",
    "#ff7f00",
    "#a65628",
    "#f781bf",
    "#999999",
    "#66c2a5",
    "#fc8d62",
    "#8da0cb",
    "#e78ac3",
]

BRIDGE_ND_COLOR = "#1a1a1a"
ANCHOR_COLOR = "#ffff33"
GAP_COLOR = "#d62728"
CANDIDATE_COLOR = "#17becf"
MISSING_LINK_COLOR = "#bcbd22"


@dataclass
class CorridorSubgraphReport:
    """Resultado estructurado del análisis."""

    cvegeo_origen: str
    cvegeo_destino: str
    origen_nombre: str
    destino_nombre: str
    corridor_names: Tuple[str, ...]
    edge_count_named: int
    edge_count_bridge_nd: int
    edge_count_total: int
    component_count: int
    spatial_srid: int = 0
    spatial_is_projected: bool = True
    components: List[Dict[str, Any]] = field(default_factory=list)
    bridges_nd: List[Dict[str, Any]] = field(default_factory=list)
    anchors: Dict[str, Any] = field(default_factory=dict)
    component_distances: List[Dict[str, Any]] = field(default_factory=list)
    continuity_gaps: List[Dict[str, Any]] = field(default_factory=list)
    candidate_join_edges: List[Dict[str, Any]] = field(default_factory=list)
    excluded_named_edges: List[Dict[str, Any]] = field(default_factory=list)
    edges: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "cvegeo_origen": self.cvegeo_origen,
            "cvegeo_destino": self.cvegeo_destino,
            "origen_nombre": self.origen_nombre,
            "destino_nombre": self.destino_nombre,
            "corridor_names": list(self.corridor_names),
            "edge_count_named": self.edge_count_named,
            "edge_count_bridge_nd": self.edge_count_bridge_nd,
            "edge_count_total": self.edge_count_total,
            "component_count": self.component_count,
            "spatial_srid": self.spatial_srid,
            "spatial_is_projected": self.spatial_is_projected,
            "components": self.components,
            "bridges_nd": self.bridges_nd,
            "anchors": self.anchors,
            "component_distances": self.component_distances,
            "continuity_gaps": [break_edge_for_json(g) for g in self.continuity_gaps],
            "candidate_join_edges": [
                break_edge_for_json(c) for c in self.candidate_join_edges
            ],
            "excluded_named_edges": self.excluded_named_edges,
        }


def fetch_trunk_with_bridge_meta(
    conn,
    route_ctx: RouteContext,
) -> Tuple[List[int], Set[int], List[Dict[str, Any]]]:
    """IDs del tronco, conjunto nombrado y puentes N/D con ola de expansión."""
    named = fetch_corridor_named_edge_ids(conn, route_ctx)
    named_set = set(named)
    ids = list(named)
    bridges: List[Dict[str, Any]] = []
    for wave in range(1, CORRIDOR_BRIDGE_WAVES + 1):
        wave_ids = fetch_corridor_bridge_edge_ids(conn, ids)
        new_ids = [i for i in wave_ids if i not in set(ids)]
        for bid in new_ids:
            bridges.append({"edge_id": bid, "wave": wave})
        if not new_ids:
            break
        ids.extend(new_ids)
    return ids, named_set, bridges


def _load_subgraph_edges(
    conn,
    edge_ids: List[int],
    spatial: SpatialContext,
) -> List[Dict[str, Any]]:
    if not edge_ids:
        return []
    edge_id_col = resolve_column(conn, SCHEMA, T_RNC, ["gid", "id", "ogc_fid"])
    nombre_col = resolve_column(conn, SCHEMA, T_RNC, ["nombre"])
    if not edge_id_col:
        return []
    eid_q = quote_ident(edge_id_col)
    nom_q = quote_ident(nombre_col) if nombre_col else None
    nom_sel = f"TRIM(COALESCE(c.{nom_q}::text, ''))" if nom_q else "''"
    routing = qualified(T_RNC_ROUTING)
    rnc = qualified(T_RNC)
    len_m = length_m_from_routing_join_sql(conn, spatial, "c.the_geom")
    geom_json = spatial.as_geojson_sql("c.the_geom")
    sql = f"""
        SELECT r.id::bigint AS edge_id,
               r.source::int AS source,
               r.target::int AS target,
               {len_m} AS longitud_m,
               {nom_sel} AS nombre,
               {geom_json}::text AS geom_json
          FROM {routing} r
          JOIN {rnc} c ON c.{eid_q} = r.id
         WHERE r.id = ANY(%(ids)s)
    """
    with conn.cursor() as cur:
        cur.execute(sql, {"ids": edge_ids})
        return [dict(row) for row in cur.fetchall()]


def _build_components(
    edges: List[Dict[str, Any]],
) -> Tuple[Dict[int, int], Dict[int, List[int]], Dict[int, List[int]]]:
    """component_id por vértice, aristas por componente, vértices por componente."""
    uf = _UnionFind()
    edge_by_comp: Dict[int, List[int]] = {}
    for e in edges:
        s, t = int(e["source"]), int(e["target"])
        uf.union(s, t)
    roots = sorted(set(uf.find(v) for v in uf._parent))
    root_to_cid = {root: idx + 1 for idx, root in enumerate(roots)}
    vertex_comp: Dict[int, int] = {}
    for v in uf._parent:
        vertex_comp[v] = root_to_cid[uf.find(v)]
    verts_by_comp: Dict[int, List[int]] = {cid: [] for cid in root_to_cid.values()}
    for v, cid in vertex_comp.items():
        verts_by_comp.setdefault(cid, []).append(v)
    for cid in verts_by_comp:
        verts_by_comp[cid] = sorted(verts_by_comp[cid])
    for e in edges:
        cid = vertex_comp[int(e["source"])]
        edge_by_comp.setdefault(cid, []).append(int(e["edge_id"]))
    return vertex_comp, edge_by_comp, verts_by_comp


def _fetch_excluded_named_edges(
    conn,
    route_ctx: RouteContext,
    trunk_ids: Set[int],
    spatial: SpatialContext,
) -> List[Dict[str, Any]]:
    names = od_direct_nombre_lc_values(route_ctx)
    if not names:
        return []
    edge_id = resolve_column(conn, SCHEMA, T_RNC, ["gid", "id", "ogc_fid"])
    nombre_col = resolve_column(conn, SCHEMA, T_RNC, ["nombre"])
    peaje_col = resolve_column(conn, SCHEMA, T_RNC, ["peaje"])
    if not edge_id or not nombre_col:
        return []
    eid_q = quote_ident(edge_id)
    nom_q = quote_ident(nombre_col)
    peaje_sel = (
        f"TRIM(COALESCE(c.{quote_ident(peaje_col)}::text, ''))"
        if peaje_col
        else "'N/D'"
    )
    from ruteo.routing_engine.strategies.od_corridor import _c_rnc_toll_predicate_sql

    is_toll = _c_rnc_toll_predicate_sql(conn, alias="c")
    rnc = qualified(T_RNC)
    len_m = length_m_from_rnc_sql(conn, spatial, "c.the_geom")
    geom_json = spatial.as_geojson_sql("c.the_geom")
    sql = f"""
        SELECT c.{eid_q}::bigint AS edge_id,
               TRIM(COALESCE(c.{nom_q}::text, '')) AS nombre,
               {peaje_sel} AS peaje,
               {len_m} AS longitud_m,
               ({is_toll}) AS es_peaje,
               {geom_json}::text AS geom_json
          FROM {rnc} c
         WHERE LOWER(TRIM(COALESCE(c.{nom_q}::text, ''))) = ANY(%s)
         ORDER BY c.{eid_q}
    """
    out: List[Dict[str, Any]] = []
    with conn.cursor() as cur:
        cur.execute(sql, (list(names),))
        for row in cur.fetchall():
            eid = int(row["edge_id"])
            if eid in trunk_ids:
                continue
            reason = "peaje" if row.get("es_peaje") else "no_en_tronco"
            out.append(
                {
                    "edge_id": eid,
                    "nombre": row["nombre"],
                    "peaje": row["peaje"],
                    "longitud_m": float(row["longitud_m"] or 0),
                    "reason": reason,
                    "geom_json": row.get("geom_json"),
                }
            )
    return out


def _fetch_internal_missing_edges(
    conn,
    trunk_ids: List[int],
    subgraph_vertices: Set[int],
    vertex_comp: Dict[int, int],
    spatial: SpatialContext,
    comp_dist_map: Dict[Tuple[int, int], float],
) -> List[Dict[str, Any]]:
    """Aristas sin peaje entre vértices del subgrafo que no entraron al tronco."""
    if len(subgraph_vertices) < 2 or not trunk_ids:
        return []
    routing = qualified(T_RNC_ROUTING)
    rnc = qualified(T_RNC)
    edge_id = resolve_column(conn, SCHEMA, T_RNC, ["gid", "id", "ogc_fid"])
    if not edge_id:
        return []
    eid_q = quote_ident(edge_id)
    not_toll = not_toll_on_alias_sql("r")
    attr_sel = routing_edge_attr_select_sql(conn, spatial)
    sql = f"""
        SELECT {attr_sel}
          FROM {routing} r
          JOIN {rnc} c ON c.{eid_q} = r.id
         WHERE r.id <> ALL(%(trunk)s)
           AND ({not_toll})
           AND r.source = ANY(%(verts)s)
           AND r.target = ANY(%(verts)s)
         ORDER BY longitud
         LIMIT 80
    """
    with conn.cursor() as cur:
        cur.execute(
            sql,
            {"trunk": trunk_ids, "verts": list(subgraph_vertices)},
        )
        rows = [dict(r) for r in cur.fetchall()]
    gaps: List[Dict[str, Any]] = []
    for row in rows:
        s, t = int(row["source"]), int(row["target"])
        comp_s = vertex_comp.get(s)
        comp_t = vertex_comp.get(t)
        if comp_s is None or comp_t is None or comp_s == comp_t:
            continue
        gaps.append(
            break_edge_from_row(
                row,
                vertex_comp=vertex_comp,
                comp_dist_map=comp_dist_map,
                kind="continuity_gap",
            )
        )
    gaps.sort(key=lambda x: x["longitud"])
    return gaps


def _component_pair_distances(
    conn,
    verts_by_comp: Dict[int, List[int]],
    spatial: SpatialContext,
) -> List[Dict[str, Any]]:
    cids = sorted(verts_by_comp.keys())
    if len(cids) < 2:
        return []
    verts = qualified(T_RNC_VERTICES)
    pairs: List[Dict[str, Any]] = []
    for i, ca in enumerate(cids):
        for cb in cids[i + 1 :]:
            va = verts_by_comp[ca]
            vb = verts_by_comp[cb]
            if not va or not vb:
                continue
            dist_sql = spatial.distance_m_sql("a.the_geom", "b.the_geom")
            sql = f"""
                SELECT {dist_sql}::double precision AS dist_m,
                       a.id::int AS vid_a,
                       b.id::int AS vid_b
                  FROM {verts} a
                  CROSS JOIN {verts} b
                 WHERE a.id = ANY(%(va)s)
                   AND b.id = ANY(%(vb)s)
                 ORDER BY a.the_geom <-> b.the_geom
                 LIMIT 1
            """
            with conn.cursor() as cur:
                cur.execute(sql, {"va": va, "vb": vb})
                row = cur.fetchone()
            if not row or row.get("dist_m") is None:
                continue
            pairs.append(
                {
                    "component_a": ca,
                    "component_b": cb,
                    "distance_m": round(float(row["dist_m"]), 2),
                    "nearest_vertex_a": int(row["vid_a"]) if row.get("vid_a") else None,
                    "nearest_vertex_b": int(row["vid_b"]) if row.get("vid_b") else None,
                }
            )
    pairs.sort(key=lambda x: x["distance_m"])
    return pairs


def _fetch_candidate_join_edges(
    conn,
    trunk_ids: List[int],
    verts_by_comp: Dict[int, List[int]],
    vertex_comp: Dict[int, int],
    spatial: SpatialContext,
    comp_dist_map: Dict[Tuple[int, int], float],
    *,
    limit_per_pair: int = 15,
) -> List[Dict[str, Any]]:
    cids = sorted(verts_by_comp.keys())
    if len(cids) < 2:
        return []
    routing = qualified(T_RNC_ROUTING)
    rnc = qualified(T_RNC)
    edge_id = resolve_column(conn, SCHEMA, T_RNC, ["gid", "id", "ogc_fid"])
    if not edge_id:
        return []
    eid_q = quote_ident(edge_id)
    not_toll = not_toll_on_alias_sql("r")
    attr_sel = routing_edge_attr_select_sql(conn, spatial)
    candidates: List[Dict[str, Any]] = []
    for i, ca in enumerate(cids):
        for cb in cids[i + 1 :]:
            va, vb = verts_by_comp[ca], verts_by_comp[cb]
            sql = f"""
                SELECT {attr_sel}
                  FROM {routing} r
                  JOIN {rnc} c ON c.{eid_q} = r.id
                 WHERE r.id <> ALL(%(trunk)s)
                   AND ({not_toll})
                   AND (
                         (r.source = ANY(%(va)s) AND r.target = ANY(%(vb)s))
                      OR (r.source = ANY(%(vb)s) AND r.target = ANY(%(va)s))
                   )
                 ORDER BY longitud
                 LIMIT %(lim)s
            """
            with conn.cursor() as cur:
                cur.execute(
                    sql,
                    {"trunk": trunk_ids, "va": va, "vb": vb, "lim": limit_per_pair},
                )
                for row in cur.fetchall():
                    candidates.append(
                        break_edge_from_row(
                            dict(row),
                            vertex_comp=vertex_comp,
                            comp_dist_map=comp_dist_map,
                            kind="candidate_join",
                        )
                    )
    candidates.sort(key=lambda x: x["longitud"])
    return candidates


def _resolve_anchor_info(
    conn,
    loc_meta: Dict[str, str],
    cvegeo: str,
    route_ctx: RouteContext,
    named_ids: List[int],
    vertex_comp: Dict[int, int],
    role: str,
) -> Dict[str, Any]:
    snap = snap_vertex_to_corridor(conn, loc_meta, cvegeo, route_ctx)
    cands = corridor_anchor_candidates(conn, loc_meta, cvegeo, named_ids)
    cand_info = [
        {
            "vertex_id": vid,
            "component_id": vertex_comp.get(vid),
        }
        for vid in cands
    ]
    return {
        "role": role,
        "snap_vertex_id": snap,
        "snap_component_id": vertex_comp.get(snap) if snap else None,
        "candidates": cand_info,
    }


def analyze_corridor_subgraph(
    conn,
    *,
    cvegeo_origen: str,
    cvegeo_destino: str,
    route_ctx: RouteContext,
    loc_meta: Dict[str, str],
    origen_nombre: str = "",
    destino_nombre: str = "",
) -> CorridorSubgraphReport:
    """
    Analiza el subgrafo del corredor OD usado por la estrategia stitch.

    Retorna métricas topológicas y metadatos para GeoJSON de diagnóstico.
    """
    spatial = detect_spatial_context_conn(conn)
    trunk_ids, named_set, bridges_meta = fetch_trunk_with_bridge_meta(conn, route_ctx)
    bridge_id_set = {b["edge_id"] for b in bridges_meta}
    edges_raw = _load_subgraph_edges(conn, trunk_ids, spatial)

    for e in edges_raw:
        eid = int(e["edge_id"])
        if eid in named_set:
            e["edge_role"] = "named"
        elif eid in bridge_id_set:
            e["edge_role"] = "bridge_nd"
        else:
            e["edge_role"] = "other"

    vertex_comp, edge_by_comp, verts_by_comp = _build_components(edges_raw)
    subgraph_vertices = set(vertex_comp.keys())

    components: List[Dict[str, Any]] = []
    for idx, cid in enumerate(sorted(verts_by_comp.keys())):
        color = COMPONENT_COLORS[idx % len(COMPONENT_COLORS)]
        components.append(
            {
                "component_id": cid,
                "edge_count": len(edge_by_comp.get(cid, [])),
                "vertex_count": len(verts_by_comp[cid]),
                "edge_ids": sorted(edge_by_comp.get(cid, [])),
                "vertex_ids": verts_by_comp[cid],
                "color": color,
            }
        )

    bridges_nd: List[Dict[str, Any]] = []
    for b in bridges_meta:
        eid = b["edge_id"]
        edge = next((x for x in edges_raw if int(x["edge_id"]) == eid), None)
        bridges_nd.append(
            {
                "edge_id": eid,
                "wave": b["wave"],
                "component_id": vertex_comp.get(int(edge["source"])) if edge else None,
                "longitud_m": float(edge["longitud_m"]) if edge else None,
                "nombre": edge.get("nombre") if edge else None,
            }
        )

    named_ids = list(named_set)
    anchors = {
        "origen": _resolve_anchor_info(
            conn, loc_meta, cvegeo_origen, route_ctx, named_ids, vertex_comp, "origen"
        ),
        "destino": _resolve_anchor_info(
            conn, loc_meta, cvegeo_destino, route_ctx, named_ids, vertex_comp, "destino"
        ),
    }

    comp_dist = _component_pair_distances(conn, verts_by_comp, spatial)
    comp_dist_map = component_pair_distance_map(comp_dist)
    continuity_gaps = _fetch_internal_missing_edges(
        conn, trunk_ids, subgraph_vertices, vertex_comp, spatial, comp_dist_map
    )
    candidate_join = _fetch_candidate_join_edges(
        conn, trunk_ids, verts_by_comp, vertex_comp, spatial, comp_dist_map
    )
    excluded = _fetch_excluded_named_edges(conn, route_ctx, set(trunk_ids), spatial)

    for e in edges_raw:
        cid = vertex_comp.get(int(e["source"]))
        comp = next((c for c in components if c["component_id"] == cid), None)
        e["component_id"] = cid
        e["color"] = comp["color"] if comp else "#888888"

    return CorridorSubgraphReport(
        cvegeo_origen=cvegeo_origen,
        cvegeo_destino=cvegeo_destino,
        origen_nombre=origen_nombre,
        destino_nombre=destino_nombre,
        corridor_names=od_direct_nombre_lc_values(route_ctx),
        edge_count_named=len(named_set),
        edge_count_bridge_nd=len(bridge_id_set),
        edge_count_total=len(trunk_ids),
        component_count=len(components),
        spatial_srid=spatial.srid,
        spatial_is_projected=spatial.is_projected,
        components=components,
        bridges_nd=bridges_nd,
        anchors=anchors,
        component_distances=comp_dist,
        continuity_gaps=continuity_gaps,
        candidate_join_edges=candidate_join,
        excluded_named_edges=excluded,
        edges=edges_raw,
    )


def format_report_summary(report: CorridorSubgraphReport) -> str:
    """Texto legible para consola."""
    lines = [
        f"Corredor: {report.origen_nombre or report.cvegeo_origen} → "
        f"{report.destino_nombre or report.cvegeo_destino}",
        f"Nombres OD: {', '.join(report.corridor_names) or '(ninguno)'}",
        f"Aristas tronco: {report.edge_count_total} "
        f"(nombradas={report.edge_count_named}, puentes N/D={report.edge_count_bridge_nd})",
        f"Componentes conexas: {report.component_count}",
        f"SRID geometrías: {report.spatial_srid} "
        f"({'proyectado' if report.spatial_is_projected else 'geográfico'})",
    ]
    for comp in report.components:
        lines.append(
            f"  · componente {comp['component_id']}: "
            f"{comp['edge_count']} aristas, {comp['vertex_count']} vértices "
            f"[color {comp['color']}]"
        )

    ao = report.anchors.get("origen", {})
    ad = report.anchors.get("destino", {})
    lines.append(
        f"Ancla snap origen: vértice {ao.get('snap_vertex_id')} "
        f"→ componente {ao.get('snap_component_id')}"
    )
    lines.append(
        f"Ancla snap destino: vértice {ad.get('snap_vertex_id')} "
        f"→ componente {ad.get('snap_component_id')}"
    )
    lines.append(
        f"Candidatos ancla: origen={len(ao.get('candidates') or [])}, "
        f"destino={len(ad.get('candidates') or [])}"
    )

    if report.bridges_nd:
        lines.append(f"Puentes N/D agregados ({len(report.bridges_nd)}):")
        by_wave: Dict[int, int] = {}
        for b in report.bridges_nd:
            by_wave[b["wave"]] = by_wave.get(b["wave"], 0) + 1
        for w in sorted(by_wave):
            lines.append(f"  ola {w}: {by_wave[w]} aristas")

    if report.component_distances:
        lines.append("Distancias entre componentes (mín. geográfica vértice–vértice):")
        for p in report.component_distances[:8]:
            lines.append(
                f"  {p['component_a']} ↔ {p['component_b']}: "
                f"{p['distance_m']:.1f} m "
                f"(v{p['nearest_vertex_a']}–v{p['nearest_vertex_b']})"
            )

    if report.continuity_gaps:
        lines.append(
            f"Enlaces internos ausentes en tronco ({len(report.continuity_gaps)}): "
            "aristas sin peaje entre vértices del subgrafo en componentes distintas"
        )
        for g in report.continuity_gaps[:6]:
            lines.append(
                f"  gid={g['gid']} comp {g['componente_origen']}→{g['componente_destino']} "
                f"{g['longitud']:.0f} m «{g['nombre']}» "
                f"({g.get('tipo_vial') or 'N/D'}, peaje={g.get('peaje') or 'N/D'})"
            )

    if report.candidate_join_edges:
        lines.append(
            f"Candidatas para unir componentes ({len(report.candidate_join_edges)}):"
        )
        for c in report.candidate_join_edges[:8]:
            dist = c.get("distancia_entre_componentes")
            dist_s = f"{dist:.0f} m entre comp." if dist is not None else "N/D"
            lines.append(
                f"  gid={c['gid']} comp {c['componente_origen']}↔{c['componente_destino']} "
                f"tramo={c['longitud']:.0f} m, {dist_s} «{c['nombre']}» "
                f"({c.get('tipo_vial') or 'N/D'})"
            )

    if report.excluded_named_edges:
        lines.append(
            f"Aristas nombradas del corredor excluidas del tronco: "
            f"{len(report.excluded_named_edges)}"
        )
        peaje_n = sum(1 for x in report.excluded_named_edges if x["reason"] == "peaje")
        if peaje_n:
            lines.append(f"  ({peaje_n} por peaje)")

    same_comp = (
        ao.get("snap_component_id") is not None
        and ao.get("snap_component_id") == ad.get("snap_component_id")
    )
    if report.component_count > 1:
        lines.append(
            "Diagnóstico: el corredor está PARTIDO — las anclas caen en componentes distintas "
            "o no hay camino en el subgrafo."
            if not same_comp
            else "Diagnóstico: anclas en la misma componente, pero otras partes del corredor "
            "permanecen desconectadas."
        )
    elif report.component_count == 1:
        lines.append("Diagnóstico: subgrafo en UNA sola componente conexa.")

    return "\n".join(lines)
