"""Carga filas municipales desde atlas.tab_municipal (con JOIN opcional a c_mun)."""

from typing import Any, Dict, List, Optional, Sequence, Tuple

from column_resolver import resolve_column
from tables import SCHEMA, T_MUN, T_TAB_MUNICIPAL, qualified
from utils import is_mun_cve3, norm_cve_mun, quote_ident


def load_tab_municipal_rows(
    conn,
    extra_columns: Sequence[Tuple[str, Sequence[str], str]],
    *,
    join_c_mun_if_no_cve: bool = True,
) -> List[Dict[str, Any]]:
    """
    extra_columns: lista de (alias_salida, candidatos_columna, alias_sql opcional en SELECT)
    Devuelve filas con cve_mun (3 dígitos), nom_mun y columnas extra.
    """
    col_nom = resolve_column(conn, SCHEMA, T_TAB_MUNICIPAL, ("nom_mun", "NOM_MUN", "nomgeo", "NOMGEO"))
    col_cve = resolve_column(conn, SCHEMA, T_TAB_MUNICIPAL, ("cve_mun", "CVE_MUN", "cvemun", "CVEMUN"))
    if not col_nom:
        raise ValueError("No se encontró columna nom_mun en tab_municipal")

    select_parts = [
        f"TRIM(BOTH FROM t.{quote_ident(col_nom)}::text) AS nom_mun",
    ]
    join_clause = ""

    if col_cve:
        select_parts.append(
            f"TRIM(BOTH FROM t.{quote_ident(col_cve)}::text) AS cve_mun"
        )
    elif join_c_mun_if_no_cve:
        col_cve_m = resolve_column(conn, SCHEMA, T_MUN, ("cve_mun", "CVE_MUN"))
        col_nom_m = resolve_column(conn, SCHEMA, T_MUN, ("nomgeo", "NOMGEO", "nom_mun"))
        if col_cve_m and col_nom_m:
            select_parts.append(
                f"NULLIF(TRIM(BOTH FROM mun.{quote_ident(col_cve_m)}::text), '') AS cve_mun"
            )
            join_clause = f"""
              LEFT JOIN {qualified(T_MUN)} mun
                ON LOWER(TRIM(BOTH FROM t.{quote_ident(col_nom)}::text))
                 = LOWER(TRIM(BOTH FROM mun.{quote_ident(col_nom_m)}::text))
            """
        else:
            select_parts.append("NULL::text AS cve_mun")
    else:
        select_parts.append("NULL::text AS cve_mun")

    for alias, candidates, _ in extra_columns:
        col = resolve_column(conn, SCHEMA, T_TAB_MUNICIPAL, candidates)
        if not col:
            raise ValueError(f"Columna no encontrada en tab_municipal: {candidates}")
        select_parts.append(f"t.{quote_ident(col)} AS {alias}")

    sql = f"""
      SELECT {', '.join(select_parts)}
        FROM {qualified(T_TAB_MUNICIPAL)} t
        {join_clause}
    """
    with conn.cursor() as cur:
        cur.execute(sql)
        db_rows = cur.fetchall()

    rows: List[Dict[str, Any]] = []
    for r in db_rows:
        cve_raw = r.get("cve_mun")
        if not is_mun_cve3(cve_raw):
            continue
        nom = (r.get("nom_mun") or "").strip()
        if not nom:
            continue
        row = {
            "cve_mun": norm_cve_mun(cve_raw),
            "nom_mun": nom,
        }
        for alias, _, _ in extra_columns:
            row[alias] = r.get(alias)
        rows.append(row)
    return rows


def fetch_nacional_estatal_municipio(
    conn,
    cve_selected: str,
    nom_sel_norm: str,
    servicio_keys: Sequence[str],
) -> Dict[str, Any]:
    """Filas Nacional / Estatal / municipio por nom_mun (vivienda servicios, etc.)."""
    cols = [("nom_mun", ("nom_mun", "NOM_MUN"))]
    for k in servicio_keys:
        cols.append((k, (k, k.upper())))
    col_nom = resolve_column(conn, SCHEMA, T_TAB_MUNICIPAL, ("nom_mun", "NOM_MUN"))
    if not col_nom:
        raise ValueError("nom_mun no encontrado")

    select = [f"TRIM(BOTH FROM {quote_ident(col_nom)}::text) AS nom_mun"]
    col_cve = resolve_column(conn, SCHEMA, T_TAB_MUNICIPAL, ("cve_mun", "CVE_MUN"))
    if col_cve:
        select.append(f"TRIM(BOTH FROM {quote_ident(col_cve)}::text) AS cve_mun")
    else:
        select.append("NULL::text AS cve_mun")
    for k, cands in [(x[0], x[1]) for x in cols[1:]]:
        c = resolve_column(conn, SCHEMA, T_TAB_MUNICIPAL, cands)
        if c:
            select.append(f"{quote_ident(c)} AS {k}")

    sql = f"SELECT {', '.join(select)} FROM {qualified(T_TAB_MUNICIPAL)}"
    with conn.cursor() as cur:
        cur.execute(sql)
        all_rows = cur.fetchall()

    def pick_servicios(lr):
        return {k: lr.get(k) for k in servicio_keys}

    nacional = estatal = municipio = None
    for r in all_rows:
        nom = (r.get("nom_mun") or "").strip().lower()
        if nom == "nacional":
            nacional = pick_servicios(r)
        elif nom == "estatal":
            estatal = pick_servicios(r)
    if cve_selected or nom_sel_norm:
        for r in all_rows:
            cve = norm_cve_mun(r.get("cve_mun"))
            nm = (r.get("nom_mun") or "").strip().lower()
            if (cve_selected and cve == cve_selected) or (nom_sel_norm and nm == nom_sel_norm):
                municipio = pick_servicios(r)
                break

    return {"nacional": nacional, "estatal": estatal, "municipio": municipio}
