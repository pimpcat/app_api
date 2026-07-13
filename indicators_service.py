"""Servicio unificado de indicadores del dashboard.

Fase 9: despacha por ``api.response_profile`` (y ``api.handler`` opcional),
no por ``indicator.id``. Las rutas legacy siguen intactas.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from column_resolver import resolve_column
from database import get_db
from indicator_profiles import (
    PROFILE_HANDLERS,
    SPECIAL_HANDLERS,
    ProfileError,
    build_from_catalog_indicator,
    list_handlers,
    list_profiles,
)
from indicators_catalog_loader import indicator_by_id, load_indicators_catalog_raw
from tables import SCHEMA, T_MUN, T_TAB_MUNICIPAL, T_TAB_NACIONAL, qualified
from utils import norm_cve_mun, quote_ident


class IndicatorError(Exception):
    """Error de negocio al resolver un indicador."""

    def __init__(self, code: str, message: str, status: int = 500):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def _sel(cve_mun: Optional[str], nom_mun: Optional[str]):
    return norm_cve_mun(cve_mun or ""), (nom_mun or "").strip().lower()


def _meta_from_catalog(ind: Dict[str, Any]) -> Dict[str, Any]:
    pres = ind.get("presentation") or {}
    api = ind.get("api") or {}
    return {
        "indicator_id": ind.get("id"),
        "label": ind.get("label"),
        "subtitle": ind.get("subtitle"),
        "unit": ind.get("unit") or "",
        "group_id": ind.get("group_id"),
        "response_profile": api.get("response_profile"),
        "template": pres.get("template"),
        "legacy_path": api.get("path"),
        "handler": api.get("handler"),
    }


def _indicator_resolvable(ind: Dict[str, Any]) -> bool:
    api = ind.get("api") or {}
    handler = (api.get("handler") or "").strip()
    if handler:
        return handler in SPECIAL_HANDLERS
    profile = (api.get("response_profile") or "").strip()
    return profile in PROFILE_HANDLERS


def build_indicator_payload(
    indicator_id: str,
    cve_mun: Optional[str] = None,
    nom_mun: Optional[str] = None,
    *,
    allow_disabled: bool = False,
) -> Dict[str, Any]:
    """Resuelve un indicador del catálogo vía perfil declarativo (Fase 9)."""
    key = (indicator_id or "").strip()
    if not key:
        raise IndicatorError("MISSING_ID", "Falta indicator_id.", status=400)

    if key in ("catalog", "validate", "presentation-presets"):
        raise IndicatorError("UNKNOWN_INDICATOR", f"Indicador desconocido: {key}", status=404)

    ind = indicator_by_id(key)
    if not ind:
        raise IndicatorError("UNKNOWN_INDICATOR", f"Indicador desconocido: {key}", status=404)

    if ind.get("enabled") is False and not allow_disabled:
        pass

    cve, nom = _sel(cve_mun, nom_mun)
    try:
        data = build_from_catalog_indicator(ind, cve, nom)
    except ProfileError as exc:
        raise IndicatorError(exc.code, exc.message, status=exc.status) from exc

    if not isinstance(data, dict):
        raise IndicatorError("QUERY_FAILED", "Respuesta de perfil inválida.")

    meta = _meta_from_catalog(ind)
    out = {**data}
    for k, v in meta.items():
        if k not in out:
            out[k] = v
    if "ok" not in out:
        out["ok"] = True
    return out


def build_habitantes_policia_response(cve: str, nom: str) -> Dict[str, Any]:
    """Compatibilidad rutas legacy habitantes-por-policia."""
    return build_indicator_payload("gov_habitantes_por_policia", cve, nom)


# --- Validación catálogo ↔ BD ---

_TABLE_MAP = {
    "tab_municipal": T_TAB_MUNICIPAL,
    "tab_nacional": T_TAB_NACIONAL,
    "c_mun": T_MUN,
}


def _field_candidates(field: Dict[str, Any]) -> Sequence[str]:
    col = field.get("column") or field.get("key")
    aliases = field.get("column_aliases") or []
    cands: List[str] = []
    if col:
        cands.append(str(col))
    for a in aliases:
        if a and str(a) not in cands:
            cands.append(str(a))
    return cands


def _count_municipal_nulls(conn, column_name: str) -> Optional[int]:
    col = resolve_column(conn, SCHEMA, T_TAB_MUNICIPAL, (column_name,))
    col_cve = resolve_column(conn, SCHEMA, T_TAB_MUNICIPAL, ("cve_mun", "CVE_MUN"))
    if not col or not col_cve:
        return None
    sql = f"""
      SELECT COUNT(*)::int AS n
        FROM {qualified(T_TAB_MUNICIPAL)} t
       WHERE TRIM(BOTH FROM t.{quote_ident(col_cve)}::text) ~ '^[0-9]{{3}}$'
         AND t.{quote_ident(col)} IS NULL
    """
    with conn.cursor() as cur:
        cur.execute(sql)
        row = cur.fetchone() or {}
    return int(row.get("n") or 0)


def _etl_status_for_computed(conn, cf: Dict[str, Any]) -> Dict[str, Any]:
    etl_col = (cf.get("etl_column_proposal") or "").strip()
    entry: Dict[str, Any] = {
        "key": cf.get("key"),
        "migrate_to_etl": bool(cf.get("migrate_to_etl")),
        "etl_column": etl_col or None,
        "formula": cf.get("formula"),
        "status": "not_required",
        "null_municipal_rows": None,
    }
    if not cf.get("migrate_to_etl"):
        return entry
    if not etl_col:
        entry["status"] = "missing_proposal"
        return entry

    resolved = resolve_column(conn, SCHEMA, T_TAB_MUNICIPAL, (etl_col,))
    if not resolved:
        entry["status"] = "column_missing"
        return entry

    nulls = _count_municipal_nulls(conn, resolved)
    entry["null_municipal_rows"] = nulls
    entry["resolved"] = resolved
    if nulls is None:
        entry["status"] = "unknown"
    elif nulls > 0:
        entry["status"] = "needs_backfill"
    else:
        entry["status"] = "ready"
    return entry


def validate_indicators_catalog() -> Dict[str, Any]:
    """Cruza fields del catálogo con BD, ETL y perfiles (Fase 9)."""
    catalog = load_indicators_catalog_raw()
    indicators_report: List[Dict[str, Any]] = []
    total_fields = 0
    total_ok = 0
    total_missing = 0
    total_computed = 0
    etl_items: List[Dict[str, Any]] = []
    etl_ready_count = 0
    etl_pending_count = 0

    with get_db() as conn:
        for ind in catalog.get("indicators", []):
            iid = ind.get("id")
            fields_ok: List[Dict[str, Any]] = []
            fields_missing: List[Dict[str, Any]] = []
            computed: List[Dict[str, Any]] = []

            for field in ind.get("fields") or []:
                total_fields += 1
                table_key = field.get("source_table") or "tab_municipal"
                table = _TABLE_MAP.get(table_key)
                cands = _field_candidates(field)
                if not table or not cands:
                    fields_missing.append({
                        "key": field.get("key"),
                        "source_table": table_key,
                        "candidates": list(cands),
                        "reason": "unknown_table" if not table else "no_candidates",
                    })
                    total_missing += 1
                    continue
                resolved = resolve_column(conn, SCHEMA, table, cands)
                entry = {
                    "key": field.get("key"),
                    "source_table": table_key,
                    "candidates": list(cands),
                    "resolved": resolved,
                }
                if resolved:
                    fields_ok.append(entry)
                    total_ok += 1
                else:
                    fields_missing.append(entry)
                    total_missing += 1

            for cf in ind.get("computed_fields") or []:
                total_computed += 1
                etl_entry = _etl_status_for_computed(conn, cf)
                etl_entry["indicator_id"] = iid
                computed.append(etl_entry)
                if etl_entry.get("migrate_to_etl"):
                    etl_items.append(etl_entry)
                    if etl_entry.get("status") == "ready":
                        etl_ready_count += 1
                    else:
                        etl_pending_count += 1

            api = ind.get("api") or {}
            profile = api.get("response_profile")
            handler = api.get("handler")
            resolvable = _indicator_resolvable(ind)
            ind_etl_ok = all(
                c.get("status") == "ready"
                for c in computed
                if c.get("migrate_to_etl")
            )
            indicators_report.append({
                "id": iid,
                "enabled": ind.get("enabled"),
                "has_builder": resolvable,
                "has_profile": resolvable,
                "response_profile": profile,
                "handler": handler,
                "fields_ok": fields_ok,
                "fields_missing": fields_missing,
                "computed_fields": computed,
                "etl_ok": ind_etl_ok,
                "ok": len(fields_missing) == 0 and ind_etl_ok and resolvable,
            })

    fields_ok_all = total_missing == 0
    profiles_ok = all(
        r.get("has_profile") for r in indicators_report if r.get("enabled") is not False
    )
    etl_ready = etl_pending_count == 0

    presentation_summary = {
        "presets_total": 0,
        "presets_implemented": 0,
        "presets_catalog_only": 0,
        "indicators_by_preset": {},
    }
    try:
        from presentation_presets_loader import load_presentation_presets_raw, preset_by_id

        presets_data = load_presentation_presets_raw()
        presentation_summary["presets_total"] = len(presets_data.get("presets") or [])
        for p in presets_data.get("presets") or []:
            if p.get("status") == "implemented":
                presentation_summary["presets_implemented"] += 1
            elif p.get("status") == "catalog_only":
                presentation_summary["presets_catalog_only"] += 1
        for ind in catalog.get("indicators", []):
            tpl = (ind.get("presentation") or {}).get("template")
            if not tpl:
                continue
            bucket = presentation_summary["indicators_by_preset"].setdefault(tpl, [])
            bucket.append(ind.get("id"))
            for r in indicators_report:
                if r.get("id") == ind.get("id"):
                    preset = preset_by_id(tpl) or {}
                    r["presentation_template"] = tpl
                    r["presentation_status"] = preset.get("status")
                    break
    except (FileNotFoundError, ValueError, OSError):
        presentation_summary["error"] = "PRESETS_NOT_LOADED"

    profiles_summary = {
        "profiles_registered": list_profiles(),
        "handlers_registered": list_handlers(),
        "indicators_by_profile": {},
        "indicators_by_handler": {},
    }
    for ind in catalog.get("indicators", []):
        api = ind.get("api") or {}
        prof = api.get("response_profile")
        hand = api.get("handler")
        if prof:
            profiles_summary["indicators_by_profile"].setdefault(prof, []).append(ind.get("id"))
        if hand:
            profiles_summary["indicators_by_handler"].setdefault(hand, []).append(ind.get("id"))

    all_ok = fields_ok_all and profiles_ok and etl_ready

    return {
        "ok": True,
        "valid": all_ok,
        "etl_ready": etl_ready,
        "catalog_path": None,
        "summary": {
            "indicators": len(indicators_report),
            "fields_total": total_fields,
            "fields_ok": total_ok,
            "fields_missing": total_missing,
            "computed_fields": total_computed,
            "etl_required": len(etl_items),
            "etl_ready": etl_ready_count,
            "etl_pending": etl_pending_count,
            "profiles_registered": len(PROFILE_HANDLERS),
            "handlers_registered": len(SPECIAL_HANDLERS),
            "builders_registered": len(PROFILE_HANDLERS),
            "presentation": presentation_summary,
            "profiles": profiles_summary,
        },
        "etl": etl_items,
        "presentation": presentation_summary,
        "profiles": profiles_summary,
        "indicators": indicators_report,
    }


def list_registered_builders() -> List[str]:
    """Compat: lista perfiles (+ handlers) en lugar de ids de indicador."""
    return list_profiles() + [f"handler:{h}" for h in list_handlers()]
