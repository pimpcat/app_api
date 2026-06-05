"""Utilidades compartidas (equivalente a api/utils.php + helpers PHP)."""

import re
from typing import Any, Mapping, Optional, Sequence


def norm_cve_mun(raw: Any) -> str:
    if raw is None:
        return ""
    digits = re.sub(r"\D+", "", str(raw))
    if not digits:
        return ""
    if len(digits) >= 3:
        return digits[-3:]
    return digits.zfill(3)


def quote_ident(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def row_numeric(row: Mapping[str, Any], keys: Sequence[str], default: float = 0.0) -> float:
    for k in keys:
        if k not in row or row[k] is None or row[k] == "":
            continue
        try:
            return float(row[k])
        except (TypeError, ValueError):
            continue
    return default


def row_matches_selection(
    row: Mapping[str, Any],
    cve_selected: str,
    nom_sel_norm: str,
) -> bool:
    if cve_selected and row.get("cve_mun"):
        if str(row["cve_mun"]).strip() == cve_selected:
            return True
    if nom_sel_norm and row.get("nom_mun"):
        nm = str(row["nom_mun"]).strip().lower()
        if nm and nm == nom_sel_norm:
            return True
    return False


def is_mun_cve3(cve_raw: Any) -> bool:
    digits = re.sub(r"\D+", "", str(cve_raw or "").strip())
    return len(digits) == 3


def mun_where_sql(alias: str = "", with_cvegeo: bool = True) -> str:
    a = f"{alias.rstrip('.')}." if alias else ""
    by_cve = (
        f"lpad(right(regexp_replace(TRIM(COALESCE({a}cve_mun::text, '')), "
        f"'[^0-9]', '', 'g'), 3), 3, '0') = %(cve)s"
    )
    if not with_cvegeo:
        return f"({by_cve})"
    return (
        f"({by_cve} OR ({a}cvegeo IS NOT NULL "
        f"AND length(regexp_replace(TRIM({a}cvegeo::text), '[^0-9]', '', 'g')) >= 5 "
        f"AND substring(regexp_replace(TRIM({a}cvegeo::text), '[^0-9]', '', 'g') "
        f"from 3 for 3) = %(cve)s))"
    )
