"""Vistas con ranking municipal + bloque states/entities desde tab_nacional."""

import re
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from column_resolver import resolve_column
from ranking import build_top_bottom_response
from tables import SCHEMA, T_TAB_MUNICIPAL, T_TAB_NACIONAL, qualified
from utils import is_mun_cve3, norm_cve_mun, quote_ident, row_matches_selection, row_numeric


def ent_key_to_int(ent: Any) -> int:
    d = re.sub(r"\D+", "", str(ent or ""))
    if not d:
        return -1
    try:
        n = int(d)
        return n if 1 <= n <= 32 else -1
    except ValueError:
        return -1


def build_states_ranking_vista(
    conn,
    cve_selected: str,
    nom_sel_norm: str,
    *,
    nat_metric_cols: Sequence[Tuple[str, Sequence[str]]],
    mun_sort_col: Sequence[str],
    mun_sort_alias: str,
    state_builder: Callable[[Dict], Dict],
    row_formatter: Callable[[Dict, bool], Dict],
    extra_nat_keys: Optional[Dict[str, Sequence[str]]] = None,
    response_key_states: str = "states",
    por_guerrero_col: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    col_ent = resolve_column(conn, SCHEMA, T_TAB_NACIONAL, ("ent", "ENT", "cve_ent", "CVE_ENT"))
    col_nom_ent = resolve_column(conn, SCHEMA, T_TAB_NACIONAL, ("nom_ent", "NOM_ENT", "nomgeo", "NOMGEO"))
    col_estatal = resolve_column(conn, SCHEMA, T_TAB_NACIONAL, ("estatal", "ESTATAL"))
    if not col_ent or not col_nom_ent:
        raise ValueError("Columnas ent/nom_ent no encontradas en tab_nacional")

    nat_select = [
        f"TRIM(BOTH FROM t.{quote_ident(col_ent)}::text) AS ent",
        f"TRIM(BOTH FROM t.{quote_ident(col_nom_ent)}::text) AS nom_ent",
    ]
    for alias, cands in nat_metric_cols:
        c = resolve_column(conn, SCHEMA, T_TAB_NACIONAL, cands)
        if not c:
            raise ValueError(f"Columna nacional {alias} no encontrada")
        nat_select.append(f"t.{quote_ident(c)} AS {alias}")
    if col_estatal:
        nat_select.append(f"TRIM(BOTH FROM t.{quote_ident(col_estatal)}::text) AS estatal")
    else:
        nat_select.append("''::text AS estatal")
    if extra_nat_keys:
        for alias, cands in extra_nat_keys.items():
            c = resolve_column(conn, SCHEMA, T_TAB_NACIONAL, cands)
            if c:
                nat_select.append(f"t.{quote_ident(c)} AS {alias}")

    sql_nat = f"SELECT {', '.join(nat_select)} FROM {qualified(T_TAB_NACIONAL)} t"
    with conn.cursor() as cur:
        cur.execute(sql_nat)
        nat_rows = cur.fetchall()

    col_nom = resolve_column(conn, SCHEMA, T_TAB_MUNICIPAL, ("nom_mun", "NOM_MUN"))
    col_cve = resolve_column(conn, SCHEMA, T_TAB_MUNICIPAL, ("cve_mun", "CVE_MUN"))
    col_mun = resolve_column(conn, SCHEMA, T_TAB_MUNICIPAL, mun_sort_col)
    if not col_nom or not col_cve or not col_mun:
        raise ValueError("Columnas municipales no encontradas")

    sql_mun = f"""
      SELECT TRIM(BOTH FROM t.{quote_ident(col_nom)}::text) AS nom_mun,
             TRIM(BOTH FROM t.{quote_ident(col_cve)}::text) AS cve_mun,
             t.{quote_ident(col_mun)} AS {mun_sort_alias}
        FROM {qualified(T_TAB_MUNICIPAL)} t
    """
    with conn.cursor() as cur:
        cur.execute(sql_mun)
        mun_db = cur.fetchall()

    states = []
    por_guerrero = None
    for r in nat_rows:
        ek = ent_key_to_int(r.get("ent"))
        if ek < 1:
            continue
        nom = (r.get("nom_ent") or "").strip()
        if not nom:
            continue
        st = state_builder(r)
        if st is None:
            continue
        est_raw = (r.get("estatal") or "").strip().lower()
        estatal_si = est_raw == "si" if col_estatal else ek == 12
        st["ent"] = str(ek).zfill(2)
        st["nom_ent"] = nom
        st["estatal_si"] = estatal_si
        states.append(st)
        if estatal_si and por_guerrero_col and por_guerrero is None:
            por_guerrero = row_numeric(r, por_guerrero_col, None)

    states.sort(key=lambda s: (-float(s.get(list(s.keys())[-2]) or 0), s.get("nom_ent", "")))

    rows = []
    for r in mun_db:
        if not is_mun_cve3(r.get("cve_mun")):
            continue
        nom = (r.get("nom_mun") or "").strip()
        if not nom:
            continue
        rows.append({
            "cve_mun": norm_cve_mun(r["cve_mun"]),
            "nom_mun": nom,
            mun_sort_alias: row_numeric(r, (mun_sort_alias,), None),
        })

    out = build_top_bottom_response(
        rows, mun_sort_alias, cve_selected, nom_sel_norm, row_formatter
    )
    out[response_key_states] = states
    if por_guerrero_col:
        out["por_entidad_guerrero"] = por_guerrero
    return out
