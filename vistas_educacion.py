"""Escolaridad y analfabetismo — port de escolaridad_vista.php / analfabetismo_vista.php."""

import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

from column_resolver import resolve_column
from ranking import build_top_bottom_response
from tables import SCHEMA, T_TAB_MUNICIPAL, T_TAB_NACIONAL, qualified
from utils import is_mun_cve3, norm_cve_mun, quote_ident, row_numeric


def _ent_digits(ent_raw: Any) -> str:
    return re.sub(r"\D+", "", str(ent_raw or "").strip()) or ""


def _is_nacional_nom(nom: str) -> bool:
    n = re.sub(r"\s+", " ", (nom or "").strip().lower())
    return n in ("nacional", "estados unidos mexicanos")


def _is_estatal_si(r: Dict[str, Any], has_estatal_col: bool) -> bool:
    if not has_estatal_col:
        return False
    return (str(r.get("estatal") or "").strip().lower()) == "si"


def _ent_out(ent_digits: str) -> str:
    if not ent_digits:
        return ""
    if len(ent_digits) <= 2 and ent_digits.isdigit():
        return ent_digits.zfill(2)
    return ent_digits


def _load_tab_nacional(
    conn,
    metric_cols: Sequence[Tuple[str, Sequence[str]]],
) -> Tuple[List[Dict[str, Any]], bool]:
    col_ent = resolve_column(conn, SCHEMA, T_TAB_NACIONAL, ("ent", "ENT", "cve_ent", "CVE_ENT"))
    col_nom_ent = resolve_column(conn, SCHEMA, T_TAB_NACIONAL, ("nom_ent", "NOM_ENT", "nomgeo", "NOMGEO"))
    col_estatal = resolve_column(conn, SCHEMA, T_TAB_NACIONAL, ("estatal", "ESTATAL"))
    if not col_ent or not col_nom_ent:
        raise ValueError("Columnas ent/nom_ent no encontradas en tab_nacional")

    select = [
        f"TRIM(BOTH FROM t.{quote_ident(col_ent)}::text) AS ent",
        f"TRIM(BOTH FROM t.{quote_ident(col_nom_ent)}::text) AS nom_ent",
    ]
    for alias, cands in metric_cols:
        col = resolve_column(conn, SCHEMA, T_TAB_NACIONAL, cands)
        if not col:
            raise ValueError(f"Columna nacional {alias} no encontrada")
        select.append(f"t.{quote_ident(col)} AS {alias}")
    if col_estatal:
        select.append(f"TRIM(BOTH FROM t.{quote_ident(col_estatal)}::text) AS estatal")
    else:
        select.append("''::text AS estatal")

    sql = f"SELECT {', '.join(select)} FROM {qualified(T_TAB_NACIONAL)} t"
    with conn.cursor() as cur:
        cur.execute(sql)
        return cur.fetchall(), bool(col_estatal)


def _load_municipal_metric(
    conn,
    alias: str,
    col_candidates: Sequence[str],
) -> List[Dict[str, Any]]:
    col_nom = resolve_column(conn, SCHEMA, T_TAB_MUNICIPAL, ("nom_mun", "NOM_MUN"))
    col_cve = resolve_column(conn, SCHEMA, T_TAB_MUNICIPAL, ("cve_mun", "CVE_MUN"))
    col_metric = resolve_column(conn, SCHEMA, T_TAB_MUNICIPAL, col_candidates)
    if not col_nom or not col_cve or not col_metric:
        raise ValueError(f"Columnas municipales no encontradas ({alias})")

    sql = f"""
      SELECT TRIM(BOTH FROM t.{quote_ident(col_nom)}::text) AS nom_mun,
             TRIM(BOTH FROM t.{quote_ident(col_cve)}::text) AS cve_mun,
             t.{quote_ident(col_metric)} AS {alias}
        FROM {qualified(T_TAB_MUNICIPAL)} t
    """
    with conn.cursor() as cur:
        cur.execute(sql)
        mun_db = cur.fetchall()

    rows: List[Dict[str, Any]] = []
    for r in mun_db:
        if not is_mun_cve3(r.get("cve_mun")):
            continue
        nom = (r.get("nom_mun") or "").strip()
        if not nom:
            continue
        rows.append({
            "cve_mun": norm_cve_mun(r["cve_mun"]),
            "nom_mun": nom,
            alias: row_numeric(r, (alias,), None),
        })
    return rows


def build_escolaridad_response(conn, cve_selected: str, nom_sel_norm: str) -> Dict[str, Any]:
    nat_rows, has_estatal = _load_tab_nacional(
        conn,
        [("graproes", ("graproes", "GRAPROES", "gra_proes", "GRA_PROES"))],
    )

    states: List[Dict[str, Any]] = []
    grap_nacional: Optional[float] = None
    grap_entidad: Optional[float] = None

    for r in nat_rows:
        nom = (r.get("nom_ent") or "").strip()
        if not nom:
            continue
        grap = row_numeric(r, ("graproes",), None)
        if grap is None:
            continue

        ent_digits = _ent_digits(r.get("ent"))
        estatal_si = _is_estatal_si(r, has_estatal)
        if estatal_si and grap_entidad is None:
            grap_entidad = float(grap)
        if _is_nacional_nom(nom) and grap_nacional is None:
            grap_nacional = float(grap)

        ent_all_zeros = ent_digits.isdigit() and int(ent_digits) == 0 if ent_digits else False
        nacional = _is_nacional_nom(nom) or ent_all_zeros

        states.append({
            "ent": _ent_out(ent_digits),
            "nom_ent": nom,
            "graproes": float(grap),
            "nacional": bool(nacional),
            "estatal_si": bool(estatal_si),
        })

    if not states:
        raise ValueError("No hay filas válidas en atlas.tab_nacional")

    states.sort(key=lambda s: (-s["graproes"], s.get("nom_ent", "")))

    rows = _load_municipal_metric(
        conn, "graproes", ("graproes", "GRAPROES", "gra_proes", "GRA_PROES")
    )
    if not rows:
        raise ValueError("No hay filas en atlas.tab_municipal con cve_mun de 3 dígitos")

    def fmt(r: Dict[str, Any], h: bool) -> Dict[str, Any]:
        return {
            "cve_mun": r["cve_mun"],
            "nom_mun": r["nom_mun"],
            "graproes": r["graproes"],
            "highlight": h,
        }

    out = build_top_bottom_response(rows, "graproes", cve_selected, nom_sel_norm, fmt)
    out["states"] = states
    out["grap_nacional"] = grap_nacional
    out["grap_entidad"] = grap_entidad
    return out


def build_analfabetismo_response(conn, cve_selected: str, nom_sel_norm: str) -> Dict[str, Any]:
    nat_rows, has_estatal = _load_tab_nacional(
        conn,
        [
            ("tasa_an2010", ("tasa_an2010", "TASA_AN2010", "tasa_an_2010")),
            ("tasa_an2020", ("tasa_an2020", "TASA_AN2020", "tasa_an_2020")),
        ],
    )

    states: List[Dict[str, Any]] = []
    tasa_nacional_2010: Optional[float] = None
    tasa_nacional_2020: Optional[float] = None
    tasa_entidad_2010: Optional[float] = None
    tasa_entidad_2020: Optional[float] = None
    nom_ent_estatal: Optional[str] = None

    for r in nat_rows:
        nom = (r.get("nom_ent") or "").strip()
        if not nom:
            continue
        t2010 = row_numeric(r, ("tasa_an2010",), None)
        t2020 = row_numeric(r, ("tasa_an2020",), None)
        if t2020 is None:
            continue

        ent_digits = _ent_digits(r.get("ent"))
        estatal_si = _is_estatal_si(r, has_estatal)
        if estatal_si and tasa_entidad_2020 is None:
            nom_ent_estatal = nom
            tasa_entidad_2020 = float(t2020)
            if t2010 is not None:
                tasa_entidad_2010 = float(t2010)

        ent_all_zeros = ent_digits.isdigit() and int(ent_digits) == 0 if ent_digits else False
        if ent_all_zeros or _is_nacional_nom(nom):
            if tasa_nacional_2020 is None:
                tasa_nacional_2020 = float(t2020)
                if t2010 is not None:
                    tasa_nacional_2010 = float(t2010)

        nacional = _is_nacional_nom(nom) or ent_all_zeros
        states.append({
            "ent": _ent_out(ent_digits),
            "nom_ent": nom,
            "tasa_an2020": float(t2020),
            "nacional": bool(nacional),
            "estatal_si": bool(estatal_si),
        })

    if not states:
        raise ValueError("No hay filas válidas (tasa_an2020) en atlas.tab_nacional")

    states.sort(key=lambda s: (s["tasa_an2020"], s.get("nom_ent", "")))

    mun_db = _load_municipal_metric(
        conn, "tasa_an_red", ("tasa_an_red", "TASA_AN_RED", "tasa_an_redu", "TASA_AN_REDU")
    )
    rows = [r for r in mun_db if r.get("tasa_an_red") is not None]
    if not rows:
        raise ValueError("No hay filas en atlas.tab_municipal con cve_mun de 3 dígitos")

    def fmt(r: Dict[str, Any], h: bool) -> Dict[str, Any]:
        return {
            "cve_mun": r["cve_mun"],
            "nom_mun": r["nom_mun"],
            "tasa_an_red": r["tasa_an_red"],
            "highlight": h,
        }

    out = build_top_bottom_response(rows, "tasa_an_red", cve_selected, nom_sel_norm, fmt)
    out["states"] = states
    out["tasa_nacional_2010"] = tasa_nacional_2010
    out["tasa_nacional_2020"] = tasa_nacional_2020
    out["tasa_entidad_2010"] = tasa_entidad_2010
    out["tasa_entidad_2020"] = tasa_entidad_2020
    out["nom_ent_estatal"] = nom_ent_estatal
    return out
