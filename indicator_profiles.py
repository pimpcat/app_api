"""Perfiles de consulta declarativos (Fase 9).

Un indicador nuevo con un ``response_profile`` existente no requiere Python:
solo entradas en ``catalog.json`` (fields, sort_by, aliases).

Handlers opcionales (``api.handler``) cubren payloads aún no generalizados
(analfabetismo, escolaridad, población ocupada).
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from column_resolver import resolve_column
from database import get_db
from ranking import build_top_bottom_response, normalize_ranking_size
from tab_municipal import fetch_nacional_estatal_municipio, load_tab_municipal_rows
from tables import SCHEMA, T_MUN, T_TAB_MUNICIPAL, T_TAB_NACIONAL, qualified
from utils import is_mun_cve3, norm_cve_mun, quote_ident, row_numeric
from vistas_educacion import build_analfabetismo_response, build_escolaridad_response
from vistas_nacional import ent_key_to_int
from vistas_tab_municipal import (
    build_poblacion_ocupada_response,
)


class ProfileError(Exception):
    def __init__(self, code: str, message: str, status: int = 500):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def _fields_for_table(ind: Dict[str, Any], table: str) -> List[Dict[str, Any]]:
    return [
        f
        for f in (ind.get("fields") or [])
        if (f.get("source_table") or "tab_municipal") == table
    ]


def _field_candidates(field: Dict[str, Any]) -> Tuple[str, ...]:
    key = field.get("key") or field.get("column")
    col = field.get("column") or key
    aliases = list(field.get("column_aliases") or [])
    cands: List[str] = []
    # Probar alias de BD antes que el nombre lógico (p. ej. pop_tot → pob_tot).
    for c in (*aliases, col, key):
        if not c:
            continue
        s = str(c)
        if s not in cands:
            cands.append(s)
        up = s.upper()
        if up not in cands:
            cands.append(up)
    return tuple(cands)


def _extra_columns(fields: Sequence[Dict[str, Any]]) -> List[Tuple[str, Tuple[str, ...], str]]:
    out: List[Tuple[str, Tuple[str, ...], str]] = []
    for f in fields:
        key = f.get("key")
        if not key:
            continue
        out.append((key, _field_candidates(f), ""))
    return out


def _sort_key(ind: Dict[str, Any]) -> str:
    pres = ind.get("presentation") or {}
    sk = pres.get("sort_by")
    if sk:
        return str(sk)
    fields = ind.get("fields") or []
    if fields and fields[0].get("key"):
        return str(fields[0]["key"])
    raise ProfileError("CONFIG_ERROR", "Indicador sin presentation.sort_by ni fields.")


def _ranking_size(ind: Dict[str, Any]) -> int:
    pres = ind.get("presentation") or {}
    return normalize_ranking_size(pres.get("ranking_size"))


def _fmt_factory(field_keys: Sequence[str]):
    def fmt(r: Dict[str, Any], h: bool) -> Dict[str, Any]:
        row = {
            "cve_mun": r.get("cve_mun"),
            "nom_mun": r.get("nom_mun"),
            "highlight": h,
        }
        for k in field_keys:
            row[k] = r.get(k)
        return row

    return fmt


def _to_float(row: Dict[str, Any], key: str) -> Optional[float]:
    if key not in row or row[key] is None or row[key] == "":
        return None
    try:
        return float(row[key])
    except (TypeError, ValueError):
        return None


def _primary_table(ind: Dict[str, Any]) -> str:
    src = ind.get("source") or {}
    primary = str(src.get("primary_table") or "atlas.tab_municipal")
    if "c_mun" in primary and "tab_municipal" not in primary:
        return "c_mun"
    return "tab_municipal"


# --- Perfiles genéricos ---


def profile_ranking_municipal(ind: Dict[str, Any], cve: str, nom: str) -> Dict[str, Any]:
    """top5 / middle / bottom5 desde tab_municipal o c_mun."""
    table = _primary_table(ind)
    fields = _fields_for_table(ind, "tab_municipal" if table == "tab_municipal" else "c_mun")
    if not fields and table == "c_mun":
        fields = ind.get("fields") or []
    if not fields:
        fields = ind.get("fields") or []
    field_keys = [f["key"] for f in fields if f.get("key")]
    sort_key = _sort_key(ind)
    if sort_key not in field_keys:
        field_keys = [sort_key, *field_keys]

    if table == "c_mun":
        return _ranking_from_c_mun(ind, cve, nom, field_keys, sort_key)

    extras = _extra_columns(fields)
    # Asegurar sort_key en extras (con alias de catálogo si aplica)
    if not any(a == sort_key for a, _, _ in extras):
        sk_field = next((f for f in fields if f.get("key") == sort_key), None)
        if sk_field:
            extras.insert(0, (sort_key, _field_candidates(sk_field), ""))
        else:
            extras.insert(0, (sort_key, (sort_key, sort_key.upper()), ""))

    with get_db() as conn:
        rows = load_tab_municipal_rows(conn, extras)
    if not rows:
        raise ProfileError("NO_DATA", "No hay filas municipales para el indicador.")

    # Fallback habxpol (ETL debería haberlo poblado)
    if "habxpol" in field_keys:
        for r in rows:
            if _to_float(r, "habxpol") is None:
                pol = row_numeric(r, ("pol_prev",), 0)
                pob = row_numeric(r, ("pob_tot", "pop_tot"), 0)
                r["habxpol"] = (pob / pol) if pol and pol > 0 else 0

    return build_top_bottom_response(
        rows, sort_key, cve, nom, _fmt_factory(field_keys), ranking_size=_ranking_size(ind)
    )


def _ranking_from_c_mun(
    ind: Dict[str, Any], cve: str, nom: str, field_keys: Sequence[str], sort_key: str
) -> Dict[str, Any]:
    fields = ind.get("fields") or []
    with get_db() as conn:
        col_cve = resolve_column(conn, SCHEMA, T_MUN, ("cve_mun",))
        col_nom = resolve_column(conn, SCHEMA, T_MUN, ("nomgeo", "nom_mun"))
        resolved = {}
        for f in fields:
            key = f.get("key")
            if not key:
                continue
            col = resolve_column(conn, SCHEMA, T_MUN, _field_candidates(f))
            if col:
                resolved[key] = col
        if not col_cve or not col_nom or sort_key not in resolved:
            raise ProfileError("COLUMNS_NOT_FOUND", "Faltan columnas en c_mun.")
        select = [
            f"TRIM({quote_ident(col_cve)}::text) AS cve_mun",
            f"TRIM({quote_ident(col_nom)}::text) AS nom_mun",
        ]
        for key, col in resolved.items():
            select.append(f"{quote_ident(col)} AS {key}")
        sql = f"SELECT {', '.join(select)} FROM {qualified(T_MUN)}"
        with conn.cursor() as cur:
            cur.execute(sql)
            db = cur.fetchall()
    rows = []
    for r in db:
        if not is_mun_cve3(r.get("cve_mun")):
            continue
        nm = (r.get("nom_mun") or "").strip()
        if not nm:
            continue
        row = {"cve_mun": norm_cve_mun(r["cve_mun"]), "nom_mun": nm}
        for key in field_keys:
            row[key] = row_numeric(r, (key,), 0) if key in r else r.get(key)
        rows.append(row)
    if not rows:
        raise ProfileError("NO_DATA", "No hay filas en c_mun.")
    return build_top_bottom_response(
        rows, sort_key, cve, nom, _fmt_factory(field_keys), ranking_size=_ranking_size(ind)
    )


def profile_national_state_municipio(ind: Dict[str, Any], cve: str, nom: str) -> Dict[str, Any]:
    keys = [f["key"] for f in (ind.get("fields") or []) if f.get("key")]
    if not keys:
        raise ProfileError("CONFIG_ERROR", "Indicador sin fields para national_state_municipio.")
    with get_db() as conn:
        data = fetch_nacional_estatal_municipio(conn, cve, nom, keys)
    return {"ok": True, **data, "cve_mun_selected": cve or None}


def _load_mun_nacional_estatal(
    ind: Dict[str, Any],
) -> Tuple[List[Dict[str, Any]], Optional[Dict[str, Any]], Optional[Dict[str, Any]], List[str]]:
    fields = _fields_for_table(ind, "tab_municipal") or (ind.get("fields") or [])
    field_keys = [f["key"] for f in fields if f.get("key")]
    extras = _extra_columns(fields)
    sort_key = _sort_key(ind)
    if not any(a == sort_key for a, _, _ in extras):
        extras.insert(0, (sort_key, (sort_key, sort_key.upper()), ""))

    with get_db() as conn:
        # Cargar todas las filas (incluye Nacional/Estatal por nom_mun)
        col_nom = resolve_column(conn, SCHEMA, T_TAB_MUNICIPAL, ("nom_mun", "NOM_MUN"))
        col_cve = resolve_column(conn, SCHEMA, T_TAB_MUNICIPAL, ("cve_mun", "CVE_MUN"))
        if not col_nom:
            raise ProfileError("COLUMNS_NOT_FOUND", "nom_mun no encontrado.")
        select = [f"TRIM(BOTH FROM t.{quote_ident(col_nom)}::text) AS nom_mun"]
        if col_cve:
            select.append(f"TRIM(BOTH FROM t.{quote_ident(col_cve)}::text) AS cve_mun")
        else:
            select.append("NULL::text AS cve_mun")
        for alias, cands, _ in extras:
            col = resolve_column(conn, SCHEMA, T_TAB_MUNICIPAL, cands)
            if not col:
                raise ProfileError("COLUMNS_NOT_FOUND", f"Columna no encontrada: {cands}")
            select.append(f"t.{quote_ident(col)} AS {alias}")
        sql = f"SELECT {', '.join(select)} FROM {qualified(T_TAB_MUNICIPAL)} t"
        with conn.cursor() as cur:
            cur.execute(sql)
            db_rows = cur.fetchall()

    nacional = estatal = None
    municipios: List[Dict[str, Any]] = []
    for r in db_rows:
        nom_raw = (r.get("nom_mun") or "").strip()
        low = nom_raw.lower()
        row = {"nom_mun": nom_raw, "cve_mun": r.get("cve_mun")}
        for k in field_keys:
            row[k] = r.get(k)
        if low == "nacional":
            nacional = row
            continue
        if low == "estatal":
            estatal = row
            continue
        if not is_mun_cve3(r.get("cve_mun")):
            continue
        row["cve_mun"] = norm_cve_mun(r["cve_mun"])
        municipios.append(row)
    return municipios, nacional, estatal, field_keys


def profile_ranking_with_national_state(ind: Dict[str, Any], cve: str, nom: str) -> Dict[str, Any]:
    municipios, nacional, estatal, field_keys = _load_mun_nacional_estatal(ind)
    if not municipios:
        raise ProfileError("NO_DATA", "No hay filas municipales.")
    sort_key = _sort_key(ind)
    out = build_top_bottom_response(
        municipios, sort_key, cve, nom, _fmt_factory(field_keys), ranking_size=_ranking_size(ind)
    )
    if nacional is not None:
        nat = _fmt_factory(field_keys)(
            {**nacional, "cve_mun": nacional.get("cve_mun"), "nom_mun": "Estados Unidos Mexicanos"},
            False,
        )
        out["tabla_nacional"] = nat
        out["nacional"] = {**nat, "nom_mun": nacional.get("nom_mun") or "Nacional"}
    if estatal is not None:
        ent = _fmt_factory(field_keys)(
            {**estatal, "cve_mun": estatal.get("cve_mun"), "nom_mun": "Entidad Federativa"},
            False,
        )
        out["tabla_entidad"] = ent
        out["estatal"] = {**ent, "nom_mun": estatal.get("nom_mun") or "Estatal"}
    return out


def profile_ranking_entity_only(ind: Dict[str, Any], cve: str, nom: str) -> Dict[str, Any]:
    municipios, _nacional, estatal, field_keys = _load_mun_nacional_estatal(ind)
    if not municipios:
        raise ProfileError("NO_DATA", "No hay filas municipales.")
    sort_key = _sort_key(ind)
    out = build_top_bottom_response(
        municipios, sort_key, cve, nom, _fmt_factory(field_keys), ranking_size=_ranking_size(ind)
    )
    if estatal is not None:
        out["entidad"] = _fmt_factory(field_keys)(
            {
                **estatal,
                "cve_mun": estatal.get("cve_mun") or "12",
                "nom_mun": "Entidad Federativa",
            },
            False,
        )
    return out


def profile_ranking_with_states(ind: Dict[str, Any], cve: str, nom: str) -> Dict[str, Any]:
    """states desde tab_nacional + ranking municipal desde tab_municipal."""
    nat_fields = _fields_for_table(ind, "tab_nacional")
    mun_fields = _fields_for_table(ind, "tab_municipal")
    if not mun_fields:
        raise ProfileError("CONFIG_ERROR", "ranking_with_states requiere fields en tab_municipal.")
    if not nat_fields:
        raise ProfileError("CONFIG_ERROR", "ranking_with_states requiere fields en tab_nacional.")

    mun_keys = [f["key"] for f in mun_fields if f.get("key")]
    sort_key = _sort_key(ind)
    # Métrica de barra estatal: primer field nacional que no sea % de participación
    state_metric = (ind.get("presentation") or {}).get("state_metric")
    if not state_metric:
        state_metric = nat_fields[0].get("key")
    share_key = None
    for f in nat_fields:
        k = f.get("key") or ""
        if k.startswith("por_") or "por" in k:
            share_key = k
            break

    with get_db() as conn:
        col_ent = resolve_column(conn, SCHEMA, T_TAB_NACIONAL, ("ent", "ENT", "cve_ent", "CVE_ENT"))
        col_nom = resolve_column(
            conn, SCHEMA, T_TAB_NACIONAL, ("nom_ent", "NOM_ENT", "nomgeo", "NOMGEO")
        )
        col_est = resolve_column(conn, SCHEMA, T_TAB_NACIONAL, ("estatal", "ESTATAL"))
        if not col_ent or not col_nom:
            raise ProfileError("COLUMNS_NOT_FOUND", "Faltan ent/nom_ent en tab_nacional.")

        select = [
            f"TRIM(t.{quote_ident(col_ent)}::text) AS ent",
            f"TRIM(t.{quote_ident(col_nom)}::text) AS nom_ent",
        ]
        if col_est:
            select.append(f"TRIM(t.{quote_ident(col_est)}::text) AS estatal")
        else:
            select.append("''::text AS estatal")

        for f in nat_fields:
            key = f["key"]
            col = resolve_column(conn, SCHEMA, T_TAB_NACIONAL, _field_candidates(f))
            if not col:
                raise ProfileError("COLUMNS_NOT_FOUND", f"Columna nacional no encontrada: {key}")
            select.append(f"t.{quote_ident(col)} AS {key}")

        sql = f"SELECT {', '.join(select)} FROM {qualified(T_TAB_NACIONAL)} t"
        with conn.cursor() as cur:
            cur.execute(sql)
            nat_rows = cur.fetchall()

        mun_extras = _extra_columns(mun_fields)
        mun_rows = load_tab_municipal_rows(conn, mun_extras)

    states = []
    por_guerrero = None
    for r in nat_rows:
        ek = ent_key_to_int(r.get("ent"))
        if ek < 1:
            continue
        nm = (r.get("nom_ent") or "").strip()
        if not nm:
            continue
        metric = _to_float(r, state_metric)
        if metric is None:
            continue
        est_raw = (r.get("estatal") or "").strip().lower()
        estatal_si = est_raw == "si" if col_est else ek == 12
        entry = {
            "ent": str(ek).zfill(2),
            "nom_ent": nm,
            state_metric: metric,
            "estatal_si": estatal_si,
        }
        for f in nat_fields:
            k = f["key"]
            if k != state_metric:
                entry[k] = _to_float(r, k)
        states.append(entry)
        if estatal_si and por_guerrero is None and share_key:
            por_guerrero = _to_float(r, share_key)

    states.sort(key=lambda s: (-(s.get(state_metric) or 0), s.get("nom_ent", "")))
    if not states:
        raise ProfileError("NO_DATA", "No hay filas válidas en tab_nacional.")
    if not mun_rows:
        raise ProfileError("NO_DATA", "No hay filas municipales.")

    out = build_top_bottom_response(
        mun_rows, sort_key, cve, nom, _fmt_factory(mun_keys), ranking_size=_ranking_size(ind)
    )
    out["states"] = states
    if por_guerrero is not None:
        out["por_entidad_guerrero"] = por_guerrero
    return out


# --- Handlers especiales (declarados en catalog api.handler) ---


def _handler_analfabetismo(ind: Dict[str, Any], cve: str, nom: str) -> Dict[str, Any]:
    try:
        with get_db() as conn:
            return build_analfabetismo_response(
                conn, cve, nom, ranking_size=_ranking_size(ind)
            )
    except ValueError as exc:
        raise ProfileError("QUERY_FAILED", str(exc)) from exc


def _handler_escolaridad(ind: Dict[str, Any], cve: str, nom: str) -> Dict[str, Any]:
    try:
        with get_db() as conn:
            return build_escolaridad_response(
                conn, cve, nom, ranking_size=_ranking_size(ind)
            )
    except ValueError as exc:
        raise ProfileError("QUERY_FAILED", str(exc)) from exc


def _handler_poblacion_ocupada(ind: Dict[str, Any], cve: str, nom: str) -> Dict[str, Any]:
    try:
        with get_db() as conn:
            return build_poblacion_ocupada_response(
                conn, cve, nom, ranking_size=_ranking_size(ind)
            )
    except ValueError as exc:
        raise ProfileError("QUERY_FAILED", str(exc)) from exc


SPECIAL_HANDLERS: Dict[str, Callable[[Dict[str, Any], str, str], Dict[str, Any]]] = {
    "analfabetismo": _handler_analfabetismo,
    "escolaridad": _handler_escolaridad,
    "poblacion_ocupada": _handler_poblacion_ocupada,
}

PROFILE_HANDLERS: Dict[str, Callable[[Dict[str, Any], str, str], Dict[str, Any]]] = {
    "ranking_municipal": profile_ranking_municipal,
    "ranking_with_national_state": profile_ranking_with_national_state,
    "ranking_entity_only": profile_ranking_entity_only,
    "national_state_municipio": profile_national_state_municipio,
    "ranking_with_states": profile_ranking_with_states,
}


def build_from_catalog_indicator(ind: Dict[str, Any], cve: str, nom: str) -> Dict[str, Any]:
    """Punto de entrada Fase 9: perfil (+ handler opcional)."""
    api = ind.get("api") or {}
    handler_name = (api.get("handler") or "").strip()
    if handler_name:
        handler = SPECIAL_HANDLERS.get(handler_name)
        if not handler:
            raise ProfileError(
                "UNKNOWN_HANDLER",
                f"api.handler desconocido: {handler_name}",
                status=500,
            )
        return handler(ind, cve, nom)

    profile = (api.get("response_profile") or "").strip()
    if not profile:
        raise ProfileError("CONFIG_ERROR", "Indicador sin api.response_profile.", status=400)
    fn = PROFILE_HANDLERS.get(profile)
    if not fn:
        raise ProfileError(
            "UNKNOWN_PROFILE",
            f"response_profile desconocido: {profile}",
            status=501,
        )
    return fn(ind, cve, nom)


def list_profiles() -> List[str]:
    return sorted(PROFILE_HANDLERS.keys())


def list_handlers() -> List[str]:
    return sorted(SPECIAL_HANDLERS.keys())
