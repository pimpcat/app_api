"""Explorador municipal (panel Inicio) — port de explorador_municipal.php."""

from typing import Any, Dict, List, Optional, Tuple

from column_resolver import resolve_column
from tables import SCHEMA, T_DENUE, T_LOC_PUNTO, T_MUN, T_MUN_CONTEOS, T_TAB_MUNICIPAL, T_TAB_NACIONAL, qualified
from utils import is_mun_cve3, norm_cve_mun, quote_ident, row_numeric

_base_cache: Optional[Dict[str, Any]] = None
_selected_cache: Dict[str, Dict[str, Any]] = {}
_counts_cache: Dict[str, Tuple[int, int]] = {}
_conteos_map_cache: Optional[Dict[str, Tuple[int, int]]] = None
_conteos_table_ok: Optional[bool] = None
_explorador_all_cache: Optional[Dict[str, Any]] = None


def _norm_grad(val: Any) -> str:
    if val is None:
        return ""
    s = str(val).strip()
    return s


def rank_desc(rows: List[Dict], field: str, cve_selected: str) -> Optional[int]:
    sorted_rows = sorted(
        rows,
        key=lambda r: (-float(r.get(field) or 0), str(r.get("nom_mun", ""))),
    )
    rank = 0
    prev = None
    for pos, r in enumerate(sorted_rows, 1):
        val = r.get(field)
        if prev is None or val != prev:
            rank = pos
            prev = val
        if r.get("cve_mun") == cve_selected:
            return rank
    return None


def bar_window(rows: List[Dict], field: str, cve_selected: str, window: int = 5) -> List[Dict]:
    sorted_rows = sorted(
        rows,
        key=lambda r: (-float(r.get(field) or 0), str(r.get("nom_mun", ""))),
    )
    idx = next((i for i, r in enumerate(sorted_rows) if r.get("cve_mun") == cve_selected), -1)
    if idx < 0:
        return sorted_rows[:window]
    start = max(0, idx - 2)
    end = min(len(sorted_rows), start + window)
    if end - start < window:
        start = max(0, end - window)
    return [
        {
            "nom_mun": r["nom_mun"],
            "value": r.get(field),
            "highlight": r.get("cve_mun") == cve_selected,
        }
        for r in sorted_rows[start:end]
    ]


def cve_set_c_mun(conn) -> Dict[str, bool]:
    col = resolve_column(conn, SCHEMA, T_MUN, ("cve_mun", "CVE_MUN"))
    if not col:
        return {}
    sql = f"SELECT TRIM(BOTH FROM {quote_ident(col)}::text) AS cve_mun FROM {qualified(T_MUN)}"
    with conn.cursor() as cur:
        cur.execute(sql)
        rows = cur.fetchall()
    out = {}
    for r in rows:
        if is_mun_cve3(r.get("cve_mun")):
            cve = norm_cve_mun(r["cve_mun"])
            if cve:
                out[cve] = True
    return out


def _load_municipios_rows(conn) -> List[Dict[str, Any]]:
    cols = {
        "cve_mun": ("cve_mun", "CVE_MUN"),
        "nom_mun": ("nom_mun", "NOM_MUN"),
        "pop_tot": ("pop_tot", "POP_TOT", "pob_tot"),
        "sup_km2": ("sup_km2", "SUP_KM2"),
        "vivpar_hab": ("vivpar_hab", "VIVPAR_HAB"),
        "grad_marg": ("grad_marg", "GRAD_MARG"),
        "grad_rezsoc": ("grad_rezsoc", "GRAD_REZSOC"),
        "region": ("region", "REGION"),
        "tvivpar": ("tvivpar", "TVIVPAR"),
        "ocupada": ("ocupada", "OCUPADA"),
        "graproes": ("graproes", "GRAPROES"),
        "pob_pobre": ("pob_pobre", "POB_POBRE"),
    }
    resolved = {}
    for alias, cands in cols.items():
        c = resolve_column(conn, SCHEMA, T_TAB_MUNICIPAL, cands)
        if c:
            resolved[alias] = c

    if "nom_mun" not in resolved:
        raise ValueError("nom_mun no encontrado en tab_municipal")

    select = [
        f"TRIM(BOTH FROM {quote_ident(resolved['nom_mun'])}::text) AS nom_mun",
    ]
    if "cve_mun" in resolved:
        select.append(
            f"TRIM(BOTH FROM {quote_ident(resolved['cve_mun'])}::text) AS cve_mun"
        )
    else:
        select.append("NULL::text AS cve_mun")
    for alias in (
        "pop_tot", "sup_km2", "vivpar_hab", "grad_marg", "grad_rezsoc",
        "region", "tvivpar", "ocupada", "graproes", "pob_pobre",
    ):
        if alias in resolved:
            select.append(f"{quote_ident(resolved[alias])} AS {alias}")

    sql = f"SELECT {', '.join(select)} FROM {qualified(T_TAB_MUNICIPAL)}"
    with conn.cursor() as cur:
        cur.execute(sql)
        db_rows = cur.fetchall()

    municipios = []
    for r in db_rows:
        if not is_mun_cve3(r.get("cve_mun")):
            continue
        cve = norm_cve_mun(r["cve_mun"])
        nom = (r.get("nom_mun") or "").strip()
        if not cve or not nom:
            continue
        pop = row_numeric(r, ("pop_tot",), 0.0)
        sup = row_numeric(r, ("sup_km2",), 0.0)
        municipios.append({
            "cve_mun": cve,
            "nom_mun": nom,
            "pop_tot": pop,
            "sup_km2": sup,
            "densidad": (pop / sup) if sup > 0 else 0.0,
            "vivpar_hab": row_numeric(r, ("vivpar_hab",), 0.0),
            "grad_marg": _norm_grad(r.get("grad_marg")),
            "grad_rezsoc": _norm_grad(r.get("grad_rezsoc")),
            "region": (r.get("region") or "").strip(),
            "tvivpar": row_numeric(r, ("tvivpar",), 0.0),
            "ocupada": row_numeric(r, ("ocupada",), 0.0),
            "graproes": row_numeric(r, ("graproes",), 0.0),
            "pob_pobre": row_numeric(r, ("pob_pobre",), 0.0),
        })
    return municipios


def _nom_ent(conn) -> str:
    nom_ent = "Guerrero"
    col_nom_ent = resolve_column(conn, SCHEMA, T_TAB_NACIONAL, ("nom_ent", "NOM_ENT"))
    col_estatal = resolve_column(conn, SCHEMA, T_TAB_NACIONAL, ("estatal", "ESTATAL"))
    if col_nom_ent and col_estatal:
        sql_ent = f"""
          SELECT TRIM(BOTH FROM {quote_ident(col_nom_ent)}::text) AS nom_ent
            FROM {qualified(T_TAB_NACIONAL)}
           WHERE LOWER(TRIM(BOTH FROM {quote_ident(col_estatal)}::text)) = 'si'
           LIMIT 1
        """
        with conn.cursor() as cur:
            cur.execute(sql_ent)
            ent_row = cur.fetchone()
            if ent_row and ent_row.get("nom_ent"):
                nom_ent = str(ent_row["nom_ent"]).strip()
    return nom_ent


def _load_base(conn) -> Dict[str, Any]:
    global _base_cache
    if _base_cache is not None:
        return _base_cache

    municipios = _load_municipios_rows(conn)
    cve_mun_set = cve_set_c_mun(conn)
    municipios = [m for m in municipios if m["cve_mun"] in cve_mun_set]
    _base_cache = {
        "municipios": municipios,
        "context": {
            "nom_ent": _nom_ent(conn),
            "municipio_count": len(cve_mun_set),
        },
    }
    return _base_cache


def _conteos_table_exists(conn) -> bool:
    global _conteos_table_ok
    if _conteos_table_ok is not None:
        return _conteos_table_ok
    sql = """
      SELECT 1
        FROM information_schema.tables
       WHERE table_schema = %(schema)s AND table_name = %(table)s
       LIMIT 1
    """
    with conn.cursor() as cur:
        cur.execute(sql, {"schema": SCHEMA, "table": T_MUN_CONTEOS})
        _conteos_table_ok = cur.fetchone() is not None
    return _conteos_table_ok


def _load_conteos_map(conn) -> Dict[str, Tuple[int, int]]:
    """Lee atlas.municipio_conteos (precalculado). Vacío si la tabla no existe."""
    global _conteos_map_cache
    if _conteos_map_cache is not None:
        return _conteos_map_cache

    out: Dict[str, Tuple[int, int]] = {}
    if not _conteos_table_exists(conn):
        _conteos_map_cache = out
        return out

    sql = f"""
      SELECT TRIM(cve_mun::text) AS cve_mun,
             COALESCE(n_localidades, 0)::int AS n_localidades,
             COALESCE(n_denue, 0)::int AS n_denue
        FROM {qualified(T_MUN_CONTEOS)}
    """
    with conn.cursor() as cur:
        cur.execute(sql)
        for row in cur.fetchall():
            cve = norm_cve_mun(row.get("cve_mun"))
            if cve:
                out[cve] = (int(row["n_localidades"] or 0), int(row["n_denue"] or 0))
    _conteos_map_cache = out
    return out


def _counts_for_mun_legacy(conn, cve_selected: str) -> Tuple[int, int]:
    """Fallback: COUNT en caliente (lento). Se usa si municipio_conteos no está instalada."""
    loc_count = 0
    col_loc = resolve_column(conn, SCHEMA, T_LOC_PUNTO, ("cve_mun", "CVE_MUN"))
    if col_loc:
        sql_loc = f"""
          SELECT COUNT(*)::int AS n FROM {qualified(T_LOC_PUNTO)}
           WHERE TRIM(BOTH FROM {quote_ident(col_loc)}::text) = %(cve)s
              OR RIGHT(TRIM(BOTH FROM {quote_ident(col_loc)}::text), 3) = %(cve)s
        """
        with conn.cursor() as cur:
            cur.execute(sql_loc, {"cve": cve_selected})
            lr = cur.fetchone()
            if lr:
                loc_count = int(lr["n"] or 0)

    denue_count = 0
    col_den = resolve_column(conn, SCHEMA, T_DENUE, ("cve_mun", "CVE_MUN"))
    if col_den:
        sql_den = f"""
          SELECT COUNT(*)::int AS n FROM {qualified(T_DENUE)}
           WHERE TRIM(BOTH FROM {quote_ident(col_den)}::text) = %(cve)s
              OR RIGHT(TRIM(BOTH FROM {quote_ident(col_den)}::text), 3) = %(cve)s
        """
        with conn.cursor() as cur:
            cur.execute(sql_den, {"cve": cve_selected})
            dr = cur.fetchone()
            if dr:
                denue_count = int(dr["n"] or 0)

    return loc_count, denue_count


def _counts_for_mun(
    conn,
    cve_selected: str,
    conteos_map: Optional[Dict[str, Tuple[int, int]]] = None,
) -> Tuple[int, int]:
    if cve_selected in _counts_cache:
        return _counts_cache[cve_selected]

    if conteos_map is None:
        conteos_map = _load_conteos_map(conn)

    if cve_selected in conteos_map:
        result = conteos_map[cve_selected]
    else:
        result = _counts_for_mun_legacy(conn, cve_selected)

    _counts_cache[cve_selected] = result
    return result


def _build_selected_payload(
    sel: Dict[str, Any],
    municipios: List[Dict[str, Any]],
    cve_selected: str,
    loc_count: int,
    denue_count: int,
) -> Dict[str, Any]:
    return {
        "cve_mun": sel["cve_mun"],
        "nom_mun": sel["nom_mun"],
        "panel": {
            "pop_tot": sel["pop_tot"],
            "sup_km2": sel["sup_km2"],
            "densidad": sel["densidad"],
            "localidades": loc_count,
            "region": sel["region"],
            "grad_rezsoc": sel["grad_rezsoc"],
        },
        "kpi1": {
            "poblacion_rank": rank_desc(municipios, "pop_tot", cve_selected),
            "densidad_rank": rank_desc(municipios, "densidad", cve_selected),
        },
        "kpi2": {"value": sel["pop_tot"], "bars": bar_window(municipios, "pop_tot", cve_selected)},
        "kpi3": {"value": sel["vivpar_hab"], "bars": bar_window(municipios, "vivpar_hab", cve_selected)},
        "kpi4": {"grad_marg": sel["grad_marg"]},
        "kpi5": {
            "tvivpar": sel["tvivpar"],
            "ocupada": sel["ocupada"],
            "graproes": sel["graproes"],
            "unidades_economicas": denue_count,
            "pob_pobre": sel["pob_pobre"],
        },
    }


def _build_selected(
    conn,
    municipios: List[Dict[str, Any]],
    cve_selected: str,
    conteos_map: Optional[Dict[str, Tuple[int, int]]] = None,
) -> Optional[Dict[str, Any]]:
    sel = next((m for m in municipios if m["cve_mun"] == cve_selected), None)
    if not sel:
        return None

    loc_count, denue_count = _counts_for_mun(conn, cve_selected, conteos_map)
    return _build_selected_payload(sel, municipios, cve_selected, loc_count, denue_count)


def build_explorador_response(conn, cve_selected: str) -> Dict[str, Any]:
    base = _load_base(conn)
    payload: Dict[str, Any] = {
        "ok": True,
        "context": base["context"],
        "selected": None,
    }
    if not cve_selected:
        return payload

    if cve_selected in _selected_cache:
        payload["selected"] = _selected_cache[cve_selected]
        return payload

    conteos_map = _load_conteos_map(conn)
    selected = _build_selected(conn, base["municipios"], cve_selected, conteos_map)
    if selected:
        _selected_cache[cve_selected] = selected
        payload["selected"] = selected
    return payload


def build_explorador_all_response(conn) -> Dict[str, Any]:
    """Payload completo para los ~85 municipios (una lectura de conteos + CPU ligera)."""
    global _explorador_all_cache
    if _explorador_all_cache is not None:
        return _explorador_all_cache

    base = _load_base(conn)
    conteos_map = _load_conteos_map(conn)
    municipios = base["municipios"]
    selected_by_cve: Dict[str, Dict[str, Any]] = {}

    for m in municipios:
        cve = m["cve_mun"]
        loc_count, denue_count = _counts_for_mun(conn, cve, conteos_map)
        selected_by_cve[cve] = _build_selected_payload(
            m, municipios, cve, loc_count, denue_count
        )
        _selected_cache[cve] = selected_by_cve[cve]

    _explorador_all_cache = {
        "ok": True,
        "context": base["context"],
        "selected": selected_by_cve,
    }
    return _explorador_all_cache


def invalidate_explorador_caches() -> None:
    """Tras refresh de municipio_conteos en BDD, reiniciar el proceso API o llamar esto."""
    global _base_cache, _conteos_map_cache, _conteos_table_ok, _explorador_all_cache
    _base_cache = None
    _conteos_map_cache = None
    _conteos_table_ok = None
    _explorador_all_cache = None
    _selected_cache.clear()
    _counts_cache.clear()
