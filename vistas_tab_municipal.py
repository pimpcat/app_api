"""Vistas tab_municipal (+ tab_nacional) — port de *_vista.php."""

import re
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from column_resolver import resolve_column
from ranking import build_top_bottom_response
from tables import SCHEMA, T_TAB_MUNICIPAL, qualified
from utils import is_mun_cve3, norm_cve_mun, quote_ident
from vistas_educacion import (
    _ent_digits,
    _ent_out,
    _is_estatal_si,
    _is_nacional_nom,
    _load_tab_nacional,
)

MetricSpec = Tuple[str, Sequence[str], str]  # alias, candidates, parser: float|int


def _row_opt(row: Dict[str, Any], keys: Sequence[str], parser: str = "float") -> Any:
    for k in keys:
        if k not in row or row[k] is None or row[k] == "":
            continue
        try:
            v = float(row[k])
            return int(round(v)) if parser == "int" else v
        except (TypeError, ValueError):
            continue
    return None


def _load_tab_municipal(conn, metrics: Sequence[MetricSpec]) -> List[Dict[str, Any]]:
    col_nom = resolve_column(conn, SCHEMA, T_TAB_MUNICIPAL, ("nom_mun", "NOM_MUN"))
    col_cve = resolve_column(conn, SCHEMA, T_TAB_MUNICIPAL, ("cve_mun", "CVE_MUN"))
    if not col_nom or not col_cve:
        raise ValueError("Columnas nom_mun/cve_mun no encontradas en tab_municipal")

    select = [
        f"TRIM(BOTH FROM t.{quote_ident(col_nom)}::text) AS nom_mun",
        f"TRIM(BOTH FROM t.{quote_ident(col_cve)}::text) AS cve_mun",
    ]
    for alias, cands, _ in metrics:
        col = resolve_column(conn, SCHEMA, T_TAB_MUNICIPAL, cands)
        if not col:
            raise ValueError(f"Columna {alias} no encontrada en tab_municipal")
        select.append(f"t.{quote_ident(col)} AS {alias}")

    sql = f"SELECT {', '.join(select)} FROM {qualified(T_TAB_MUNICIPAL)} t"
    with conn.cursor() as cur:
        cur.execute(sql)
        return cur.fetchall()


def _split_municipal_rows(
    db_rows: Sequence[Dict[str, Any]],
    field_names: Sequence[str],
    parsers: Dict[str, str],
    *,
    cve_range: Optional[Tuple[int, int]] = None,
) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]], List[Dict[str, Any]]]:
    nacional: Optional[Dict[str, Any]] = None
    estatal: Optional[Dict[str, Any]] = None
    municipios: List[Dict[str, Any]] = []

    for r in db_rows:
        nom_raw = (r.get("nom_mun") or "").strip()
        nm_low = nom_raw.lower()
        if nm_low == "nacional":
            nacional = _parse_row(r, field_names, parsers, nom_raw, r.get("cve_mun"))
            continue
        if nm_low == "estatal":
            estatal = _parse_row(r, field_names, parsers, nom_raw, r.get("cve_mun"))
            continue

        if not is_mun_cve3(r.get("cve_mun")):
            continue
        cve = norm_cve_mun(r["cve_mun"])
        if cve_range:
            cve_num = int(cve)
            if cve_num < cve_range[0] or cve_num > cve_range[1]:
                continue
        if not nom_raw:
            continue
        municipios.append(_parse_row(r, field_names, parsers, nom_raw, cve))

    return nacional, estatal, municipios


def _parse_row(
    r: Dict[str, Any],
    field_names: Sequence[str],
    parsers: Dict[str, str],
    nom: str,
    cve: Any,
) -> Dict[str, Any]:
    row: Dict[str, Any] = {
        "cve_mun": norm_cve_mun(cve) if cve and is_mun_cve3(cve) else (str(cve).strip() if cve else None),
        "nom_mun": nom,
    }
    for f in field_names:
        row[f] = _row_opt(r, (f,), parsers.get(f, "float"))
    return row


def _fmt_row(
    r: Dict[str, Any],
    highlight: bool,
    display_override: Optional[str],
    fields: Sequence[str],
) -> Dict[str, Any]:
    disp = display_override if display_override else (r.get("nom_mun") or "")
    out: Dict[str, Any] = {
        "cve_mun": r.get("cve_mun"),
        "nom_mun": disp,
        "highlight": highlight,
    }
    for f in fields:
        out[f] = r.get(f)
    return out


def _build_ranking_vista(
    conn,
    cve_selected: str,
    nom_sel_norm: str,
    *,
    metrics: Sequence[MetricSpec],
    sort_key: str,
    field_names: Sequence[str],
    require_nacional: bool = True,
    require_estatal: bool = True,
    nacional_label: str = "Estados Unidos Mexicanos",
    estatal_label: str = "Entidad Federativa",
    cve_range: Optional[Tuple[int, int]] = None,
    skip_nacional_in_mun: bool = False,
) -> Dict[str, Any]:
    db_rows = _load_tab_municipal(conn, metrics)
    parsers = {alias: parser for alias, _, parser in metrics}
    nacional, estatal, municipios = _split_municipal_rows(
        db_rows, field_names, parsers, cve_range=cve_range
    )

    if require_nacional and nacional is None:
        raise ValueError("No se encontró fila nom_mun = Nacional en tab_municipal")
    if require_estatal and estatal is None:
        raise ValueError("No se encontró fila nom_mun = Estatal en tab_municipal")
    if not municipios:
        raise ValueError("No hay filas municipales con cve_mun de 3 dígitos")

    def fmt(r: Dict[str, Any], h: bool) -> Dict[str, Any]:
        return _fmt_row(r, h, None, field_names)

    out = build_top_bottom_response(municipios, sort_key, cve_selected, nom_sel_norm, fmt)

    if nacional is not None and not skip_nacional_in_mun:
        out["tabla_nacional"] = _fmt_row(nacional, False, nacional_label, field_names)
    if estatal is not None:
        out["tabla_entidad"] = _fmt_row(estatal, False, estatal_label, field_names)
    return out


def build_vivienda_participacion_response(conn, cve_selected: str, nom_sel_norm: str) -> Dict[str, Any]:
    metrics: Sequence[MetricSpec] = [
        ("part_por_vivh", ("part_por_vivh", "PART_POR_VIVH", "partporvivh"), "float"),
        ("creci_00_10", ("creci_00_10", "CRECI_00_10", "pcreci_00_10", "PCRECI_00_10", "creci_0010"), "float"),
        ("creci_10_20", ("creci_10_20", "CRECI_10_20", "pcreci_10_20", "PCRECI_10_20", "creci_1020"), "float"),
    ]
    fields = ("part_por_vivh", "creci_00_10", "creci_10_20")
    db_rows = _load_tab_municipal(conn, metrics)
    parsers = {a: p for a, _, p in metrics}
    nacional, estatal, municipios = _split_municipal_rows(
        db_rows, fields, parsers, cve_range=(1, 85)
    )
    if not municipios:
        raise ValueError("No hay filas municipales 001–085 para el gráfico")

    def fmt(r: Dict[str, Any], h: bool) -> Dict[str, Any]:
        return _fmt_row(r, h, None, fields)

    out = build_top_bottom_response(municipios, "part_por_vivh", cve_selected, nom_sel_norm, fmt)
    if nacional:
        out["nacional"] = {
            "nom_mun": nacional.get("nom_mun") or "Nacional",
            "creci_00_10": nacional.get("creci_00_10"),
            "creci_10_20": nacional.get("creci_10_20"),
        }
    else:
        out["nacional"] = None
    if estatal:
        out["estatal"] = {
            "nom_mun": estatal.get("nom_mun") or "Estatal",
            "creci_00_10": estatal.get("creci_00_10"),
            "creci_10_20": estatal.get("creci_10_20"),
        }
    else:
        out["estatal"] = None
    return out


def build_poblacion_ocupada_response(conn, cve_selected: str, nom_sel_norm: str) -> Dict[str, Any]:
    nat_rows, has_estatal = _load_tab_nacional(
        conn, [("pea_ocup", ("pea_ocup", "PEA_OCUP", "pea_ocupada", "PEA_OCUPADA"))]
    )

    states: List[Dict[str, Any]] = []
    for r in nat_rows:
        nom = (r.get("nom_ent") or "").strip()
        if not nom:
            continue
        pea = _row_opt(r, ("pea_ocup",), "int")
        if pea is None:
            continue
        ent_digits = _ent_digits(r.get("ent"))
        estatal_si = _is_estatal_si(r, has_estatal)
        ent_all_zeros = ent_digits.isdigit() and int(ent_digits) == 0 if ent_digits else False
        states.append({
            "ent": _ent_out(ent_digits),
            "nom_ent": nom,
            "pea_ocup": int(pea),
            "nacional": _is_nacional_nom(nom) or ent_all_zeros,
            "estatal_si": estatal_si,
        })

    if not states:
        raise ValueError("No hay filas válidas (pea_ocup) en tab_nacional")
    states.sort(key=lambda s: (-s["pea_ocup"], s.get("nom_ent", "")))

    metrics: Sequence[MetricSpec] = [
        ("ocupada", ("ocupada", "OCUPADA", "pea_ocup", "PEA_OCUP"), "int"),
        ("sin_escol", ("sin_escol", "SIN_ESCOL"), "int"),
        ("primaria", ("primaria", "PRIMARIA"), "int"),
        ("secund", ("secund", "SECUND", "secundaria", "SECUNDARIA"), "int"),
        ("med_sup", ("med_sup", "MED_SUP"), "int"),
        ("superior", ("superior", "SUPERIOR"), "int"),
        ("no_esp", ("no_esp", "NO_ESP"), "int"),
    ]
    fields = tuple(m[0] for m in metrics)
    db_rows = _load_tab_municipal(conn, metrics)
    parsers = {a: p for a, _, p in metrics}
    nacional, estatal, municipios = _split_municipal_rows(db_rows, fields, parsers)

    if not municipios:
        raise ValueError("No hay filas municipales con cve_mun de 3 dígitos")

    def fmt(r: Dict[str, Any], h: bool) -> Dict[str, Any]:
        return _fmt_row(r, h, None, fields)

    out = build_top_bottom_response(municipios, "ocupada", cve_selected, nom_sel_norm, fmt)
    out["states"] = states
    if nacional:
        out["tabla_nacional"] = _fmt_row(nacional, False, "Estados Unidos Mexicanos", fields)
    else:
        out["tabla_nacional"] = None
    if estatal:
        out["tabla_entidad"] = _fmt_row(estatal, False, "Entidad Federativa", fields)
    else:
        out["tabla_entidad"] = None
    return out


def build_caracteristicas_economicas_response(conn, cve_selected: str, nom_sel_norm: str) -> Dict[str, Any]:
    return _build_ranking_vista(
        conn, cve_selected, nom_sel_norm,
        metrics=[
            ("ue", ("ue", "UE", "unidades_economicas", "UNIDADES_ECONOMICAS"), "int"),
            ("pers_ocup", ("pers_ocup", "PERS_OCUP", "personal_ocupado", "PERSONAL_OCUPADO"), "int"),
            ("prod_brut", ("prod_brut", "PROD_BRUT", "prod_bruta", "PROD_BRUTA"), "float"),
        ],
        sort_key="prod_brut",
        field_names=("ue", "pers_ocup", "prod_brut"),
    )


def build_superficie_agricultura_response(conn, cve_selected: str, nom_sel_norm: str) -> Dict[str, Any]:
    out = _build_ranking_vista(
        conn, cve_selected, nom_sel_norm,
        metrics=[
            ("sup_cieloabtot", ("sup_cieloabtot", "SUP_CIELOABTOT"), "float"),
            ("sup_sembtot", ("sup_sembtot", "SUP_SEMBTOT"), "float"),
            ("sup_sembtemp", ("sup_sembtemp", "SUP_SEMBTEMP"), "float"),
            ("sup_sembrieg", ("sup_sembrieg", "SUP_SEMBRIEG"), "float"),
        ],
        sort_key="sup_sembrieg",
        field_names=("sup_cieloabtot", "sup_sembtot", "sup_sembtemp", "sup_sembrieg"),
        estatal_label="Entidad federativa",
    )
    return out


def build_inversion_publica_response(conn, cve_selected: str, nom_sel_norm: str) -> Dict[str, Any]:
    metrics: Sequence[MetricSpec] = [
        ("total_inv", ("total_inv", "TOTAL_INV"), "float"),
        ("gob_inv", ("gob_inv", "GOB_INV"), "float"),
        ("desoc_inv", ("desoc_inv", "DESOC_INV"), "float"),
        ("desec_inv", ("desec_inv", "DESEC_INV"), "float"),
        ("otras_inv", ("otras_inv", "OTRAS_INV"), "float"),
    ]
    fields = tuple(m[0] for m in metrics)
    db_rows = _load_tab_municipal(conn, metrics)
    parsers = {a: p for a, _, p in metrics}
    _, estatal, municipios = _split_municipal_rows(db_rows, fields, parsers)

    if estatal is None:
        raise ValueError("No se encontró fila nom_mun = Estatal en tab_municipal")
    if not municipios:
        raise ValueError("No hay filas municipales con cve_mun de 3 dígitos")

    def fmt(r: Dict[str, Any], h: bool) -> Dict[str, Any]:
        return _fmt_row(r, h, None, fields)

    out = build_top_bottom_response(municipios, "total_inv", cve_selected, nom_sel_norm, fmt)
    out["tabla_entidad"] = _fmt_row(estatal, False, "Entidad federativa", fields)
    return out


def build_instituciones_admin_response(conn, cve_selected: str, nom_sel_norm: str) -> Dict[str, Any]:
    return _build_ranking_vista(
        conn, cve_selected, nom_sel_norm,
        metrics=[
            ("total_inst", ("total_inst", "TOTAL_INST"), "float"),
            ("inst_central", ("inst_central", "INST_CENTRAL"), "float"),
            ("inst_parampal", ("inst_parampal", "INST_PARAMPAL", "inst_paramuni", "INST_PARAMUNI"), "float"),
            ("personal", ("personal", "PERSONAL"), "float"),
        ],
        sort_key="total_inst",
        field_names=("total_inst", "inst_central", "inst_parampal", "personal"),
    )


def build_unidades_medicas_response(conn, cve_selected: str, nom_sel_norm: str) -> Dict[str, Any]:
    metrics: Sequence[MetricSpec] = [
        ("imss", ("imss", "IMSS"), "float"),
        ("issste", ("issste", "ISSSTE"), "float"),
        ("semar", ("semar", "SEMAR"), "float"),
        ("imb", ("imb", "IMB"), "float"),
        ("sesa", ("sesa", "SESA"), "float"),
        ("ssa", ("ssa", "SSA"), "float"),
    ]
    fields = ("imss", "issste", "semar", "imb", "sesa", "ssa")
    db_rows = _load_tab_municipal(conn, metrics)
    entity_row: Optional[Dict[str, Any]] = None
    municipios: List[Dict[str, Any]] = []

    for r in db_rows:
        nom = (r.get("nom_mun") or "").strip()
        imss = _row_opt(r, ("imss",), "float") or 0.0
        issste = _row_opt(r, ("issste",), "float") or 0.0
        semar = _row_opt(r, ("semar",), "float") or 0.0
        imb = _row_opt(r, ("imb",), "float") or 0.0
        sesa = _row_opt(r, ("sesa",), "float") or 0.0
        ssa = _row_opt(r, ("ssa",), "float") or 0.0
        total = imss + issste + semar + imb + sesa + ssa
        row_data = {
            "imss": imss, "issste": issste, "semar": semar,
            "imb": imb, "sesa": sesa, "ssa": ssa, "total": total,
        }

        if entity_row is None and nom.lower() == "estatal":
            entity_row = {
                "cve_mun": re.sub(r"\D+", "", str(r.get("cve_mun") or "")),
                "nom_mun": "Entidad Federativa",
                **row_data,
            }
            continue

        if not is_mun_cve3(r.get("cve_mun")) or not nom:
            continue
        municipios.append({
            "cve_mun": norm_cve_mun(r["cve_mun"]),
            "nom_mun": nom,
            **row_data,
        })

    if not municipios:
        raise ValueError("No hay filas municipales con cve_mun de 3 dígitos")

    def fmt(r: Dict[str, Any], h: bool) -> Dict[str, Any]:
        return {
            "cve_mun": r["cve_mun"],
            "nom_mun": r["nom_mun"],
            "total": r["total"],
            "imss": r["imss"],
            "issste": r["issste"],
            "semar": r["semar"],
            "imb": r["imb"],
            "sesa": r["sesa"],
            "ssa": r["ssa"],
            "highlight": h,
        }

    out = build_top_bottom_response(municipios, "total", cve_selected, nom_sel_norm, fmt)
    out["entidad"] = entity_row
    return out
