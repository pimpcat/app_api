"""Top / bottom / middle — patrón común de vistas comparativas."""

from typing import Any, Callable, Dict, List, Optional

from utils import row_matches_selection

DEFAULT_RANKING_SIZE = 5
MIN_RANKING_SIZE = 1
MAX_RANKING_SIZE = 50


def normalize_ranking_size(
    value: Any,
    *,
    default: int = DEFAULT_RANKING_SIZE,
    minimum: int = MIN_RANKING_SIZE,
    maximum: int = MAX_RANKING_SIZE,
) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, n))


def build_top_bottom_response(
    rows: List[Dict[str, Any]],
    sort_key: str,
    cve_selected: str,
    nom_sel_norm: str,
    format_row: Callable[[Dict[str, Any], bool], Dict[str, Any]],
    *,
    ranking_size: int = DEFAULT_RANKING_SIZE,
    middle_extra: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    n = normalize_ranking_size(ranking_size)
    rows_sorted = sorted(
        rows,
        key=lambda r: (-float(r.get(sort_key) or 0), str(r.get("nom_mun", ""))),
    )
    top_rows = rows_sorted[:n]
    bottom_rows = rows_sorted[-n:] if len(rows_sorted) >= n else list(rows_sorted)
    has_sel = bool(cve_selected or nom_sel_norm)

    in_top = any(
        has_sel and row_matches_selection(x, cve_selected, nom_sel_norm) for x in top_rows
    )
    in_bottom = any(
        has_sel and row_matches_selection(x, cve_selected, nom_sel_norm) for x in bottom_rows
    )

    middle = None
    if has_sel and not in_top and not in_bottom:
        for r in rows_sorted:
            if row_matches_selection(r, cve_selected, nom_sel_norm):
                middle = r
                break

    fmt_top = [
        format_row(r, has_sel and row_matches_selection(r, cve_selected, nom_sel_norm))
        for r in top_rows
    ]
    fmt_bot = [
        format_row(r, has_sel and row_matches_selection(r, cve_selected, nom_sel_norm))
        for r in bottom_rows
    ]
    fmt_mid = None
    if middle is not None:
        fmt_mid = format_row(middle, True) if middle_extra is None else middle_extra(middle)

    return {
        "ok": True,
        "cve_mun_selected": cve_selected or None,
        "ranking_size": n,
        "top5": fmt_top,
        "bottom5": fmt_bot,
        "middle": fmt_mid,
        "selected_in_top": in_top,
        "selected_in_bottom": in_bottom,
    }
