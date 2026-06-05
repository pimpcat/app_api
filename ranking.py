"""Top5 / bottom5 / middle — patrón común de vistas comparativas."""

from typing import Any, Callable, Dict, List, Optional

from utils import row_matches_selection


def build_top_bottom_response(
    rows: List[Dict[str, Any]],
    sort_key: str,
    cve_selected: str,
    nom_sel_norm: str,
    format_row: Callable[[Dict[str, Any], bool], Dict[str, Any]],
    *,
    middle_extra: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    rows_sorted = sorted(
        rows,
        key=lambda r: (-float(r.get(sort_key) or 0), str(r.get("nom_mun", ""))),
    )
    top5 = rows_sorted[:5]
    bottom5 = rows_sorted[-5:] if len(rows_sorted) >= 5 else rows_sorted
    has_sel = bool(cve_selected or nom_sel_norm)

    in_top = any(
        has_sel and row_matches_selection(x, cve_selected, nom_sel_norm) for x in top5
    )
    in_bottom = any(
        has_sel and row_matches_selection(x, cve_selected, nom_sel_norm) for x in bottom5
    )

    middle = None
    if has_sel and not in_top and not in_bottom:
        for r in rows_sorted:
            if row_matches_selection(r, cve_selected, nom_sel_norm):
                middle = r
                break

    fmt_top = [format_row(r, has_sel and row_matches_selection(r, cve_selected, nom_sel_norm)) for r in top5]
    fmt_bot = [
        format_row(r, has_sel and row_matches_selection(r, cve_selected, nom_sel_norm)) for r in bottom5
    ]
    fmt_mid = None
    if middle is not None:
        fmt_mid = format_row(middle, True) if middle_extra is None else middle_extra(middle)

    return {
        "ok": True,
        "cve_mun_selected": cve_selected or None,
        "top5": fmt_top,
        "bottom5": fmt_bot,
        "middle": fmt_mid,
        "selected_in_top": in_top,
        "selected_in_bottom": in_bottom,
    }
