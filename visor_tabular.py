"""
Consulta tabular de capas del visor geográfico.

Por ahora expone la capa «Localidades» (atlas.c_loc_punto) filtrada por municipio.
"""

from __future__ import annotations

import io
import logging
from typing import Any, Dict, List, Optional, Sequence, Tuple

from column_resolver import resolve_column
from tables import SCHEMA, T_LOC_PUNTO, qualified
from utils import mun_where_sql, norm_cve_mun, quote_ident
from visor_layers import layer_config

logger = logging.getLogger(__name__)

MAX_TABULAR_ROWS = 25_000

TABULAR_ERRORS = {
    "UNKNOWN_LAYER": "Capa no disponible para consulta tabular.",
    "MISSING_CVE_MUN": "Selecciona un municipio en el explorador.",
    "NO_ROWS": "No hay registros para este municipio en la capa seleccionada.",
    "EXPORT_FAILED": "No se pudo generar el archivo Excel.",
}

# (clave_respuesta, candidatos_en_bd, etiqueta_columna)
_LOCSPUNTO_FIELD_SPECS: Sequence[Tuple[str, Sequence[str], str]] = (
    ("cvegeo", ("cvegeo", "CVEGEO"), "Clave geográfica"),
    ("cve_ent", ("cve_ent", "CVE_ENT"), "Clave de entidad"),
    ("nom_ent", ("nom_ent", "NOM_ENT"), "Nombre de entidad"),
    ("cve_mun", ("cve_mun", "CVE_MUN"), "Clave del municipio"),
    ("nom_mun", ("nom_mun", "NOM_MUN"), "Nombre del municipio"),
    ("cve_loc", ("cve_loc", "CVE_LOC"), "Clave de la localidad"),
    ("nom_loc", ("nom_loc", "NOM_LOC"), "Nombre de la localidad"),
    ("ambito", ("ambito", "AMBITO"), "Ámbito"),
    ("altitud", ("altitud", "ALTITUD"), "Altitud"),
    ("pob_total", ("pob_total", "POB_TOTAL"), "Población total"),
    ("pob_mascul", ("pob_mascul", "POB_MASCUL"), "Población masculina"),
    ("pob_femeni", ("pob_femeni", "POB_FEMENI"), "Población femenina"),
    (
        "total_viv",
        (
            "total de v",
            "total_de_v",
            "total_de_viv",
            "total_viv",
            "vivtot",
            "total_viviendas",
        ),
        "Total de viviendas",
    ),
)

_TABULAR_LAYERS = frozenset({"locspunto"})


def tabular_error_message(code: str) -> str:
    return TABULAR_ERRORS.get(code, code)


def list_tabular_layers() -> List[Dict[str, Any]]:
    """Capas habilitadas para el selector de consulta tabular."""
    out: List[Dict[str, Any]] = []
    for layer_id in sorted(_TABULAR_LAYERS):
        cfg = layer_config(layer_id)
        if not cfg:
            continue
        out.append(
            {
                "id": layer_id,
                "label": cfg.get("label") or layer_id,
                "table": cfg.get("table"),
            }
        )
    return out


def _resolve_locspunto_columns(conn) -> List[Dict[str, str]]:
    resolved: List[Dict[str, str]] = []
    for key, candidates, label in _LOCSPUNTO_FIELD_SPECS:
        col = resolve_column(conn, SCHEMA, T_LOC_PUNTO, candidates)
        if not col:
            logger.warning("Columna no encontrada en %s: %s", T_LOC_PUNTO, candidates)
            continue
        resolved.append({"field": key, "sql": col, "label": label})
    if not resolved:
        raise ValueError("NO_COLUMNS")
    return resolved


def _fetch_nom_mun(conn, cve: str) -> Optional[str]:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT nomgeo
              FROM {qualified("c_mun")}
             WHERE lpad(right(regexp_replace(TRIM(COALESCE(cve_mun::text, '')), '[^0-9]', '', 'g'), 3), 3, '0') = %(cve)s
             LIMIT 1
            """,
            {"cve": cve},
        )
        row = cur.fetchone()
    if not row:
        return None
    val = row.get("nomgeo") if isinstance(row, dict) else row[0]
    return str(val).strip() if val is not None else None


def _serialize_cell(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (int, float, bool)):
        return value
    return str(value).strip()


def _numeric_sort_expr(sql_col: str) -> str:
    """Orden numérico para claves alfanuméricas (p. ej. cve_loc 0001…9999)."""
    q = quote_ident(sql_col)
    return (
        f"NULLIF(regexp_replace(TRIM(COALESCE({q}::text, '')), '[^0-9]', '', 'g'), '')::bigint"
    )


def fetch_locspunto_table(conn, cve_mun: str) -> Dict[str, Any]:
    cve = norm_cve_mun(cve_mun)
    if not cve:
        raise ValueError("MISSING_CVE_MUN")

    columns = _resolve_locspunto_columns(conn)
    select_parts = [f"{quote_ident(c['sql'])} AS {quote_ident(c['field'])}" for c in columns]
    order_cve_loc = next((c["sql"] for c in columns if c["field"] == "cve_loc"), None)
    order_cvegeo = next((c["sql"] for c in columns if c["field"] == "cvegeo"), columns[0]["sql"])
    if order_cve_loc:
        order_by = (
            f"{_numeric_sort_expr(order_cve_loc)} ASC NULLS LAST, "
            f"{quote_ident(order_cvegeo)} ASC"
        )
    else:
        order_by = f"{quote_ident(order_cvegeo)} ASC"

    sql = f"""
        SELECT {", ".join(select_parts)}
          FROM {qualified(T_LOC_PUNTO)}
         WHERE {mun_where_sql("", with_cvegeo=True)}
         ORDER BY {order_by}
         LIMIT {MAX_TABULAR_ROWS + 1}
    """

    with conn.cursor() as cur:
        cur.execute(sql, {"cve": cve})
        raw_rows = cur.fetchall()

    if len(raw_rows) > MAX_TABULAR_ROWS:
        raw_rows = raw_rows[:MAX_TABULAR_ROWS]

    rows: List[Dict[str, Any]] = []
    for row in raw_rows:
        item: Dict[str, Any] = {}
        for col in columns:
            key = col["field"]
            item[key] = _serialize_cell(row.get(key) if isinstance(row, dict) else None)
        rows.append(item)

    nom_mun = _fetch_nom_mun(conn, cve)
    if not rows:
        raise ValueError("NO_ROWS")

    return {
        "layer": "locspunto",
        "layer_label": (layer_config("locspunto") or {}).get("label", "Localidades"),
        "table": T_LOC_PUNTO,
        "cve_mun": cve,
        "nom_mun": nom_mun,
        "total_registros": len(rows),
        "columns": [{"field": c["field"], "label": c["label"]} for c in columns],
        "rows": rows,
    }


def fetch_tabular_data(conn, layer_id: str, cve_mun: str) -> Dict[str, Any]:
    key = (layer_id or "").strip().lower()
    if key not in _TABULAR_LAYERS:
        raise ValueError("UNKNOWN_LAYER")
    if key == "locspunto":
        return fetch_locspunto_table(conn, cve_mun)
    raise ValueError("UNKNOWN_LAYER")


def build_tabular_xlsx(payload: Dict[str, Any]) -> bytes:
    """Genera libro Excel con openpyxl (encabezados legibles y resumen municipal)."""
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
        from openpyxl.utils import get_column_letter
    except ImportError as exc:
        raise ValueError("EXPORT_FAILED") from exc

    wb = Workbook()
    ws = wb.active
    ws.title = "Localidades"

    title_font = Font(name="Calibri", size=14, bold=True, color="FFFFFF")
    header_font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
    meta_font = Font(name="Calibri", size=11, bold=True)
    body_font = Font(name="Calibri", size=11)
    title_fill = PatternFill("solid", fgColor="0D8A8A")
    header_fill = PatternFill("solid", fgColor="1565C0")
    meta_fill = PatternFill("solid", fgColor="E8F4F8")
    thin = Side(style="thin", color="B0BEC5")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)

    columns = payload.get("columns") or []
    rows = payload.get("rows") or []
    nom_mun = payload.get("nom_mun") or ""
    cve_mun = payload.get("cve_mun") or ""
    total = payload.get("total_registros", len(rows))

    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=max(len(columns), 2))
    title_cell = ws.cell(row=1, column=1, value=f"Localidades — {nom_mun} ({cve_mun})")
    title_cell.font = title_font
    title_cell.fill = title_fill
    title_cell.alignment = Alignment(horizontal="left", vertical="center")
    ws.row_dimensions[1].height = 28

    meta_rows = [
        ("Municipio", nom_mun or "—"),
        ("Clave municipal", cve_mun),
        ("Total de localidades", total),
        ("Capa", payload.get("layer_label") or "Localidades"),
        ("Tabla origen", f"atlas.{payload.get('table', T_LOC_PUNTO)}"),
    ]
    r = 3
    for label, value in meta_rows:
        c1 = ws.cell(row=r, column=1, value=label)
        c2 = ws.cell(row=r, column=2, value=value)
        for c in (c1, c2):
            c.font = meta_font if c.column == 1 else body_font
            c.fill = meta_fill
            c.border = border
        r += 1

    header_row = r + 1
    for ci, col in enumerate(columns, start=1):
        cell = ws.cell(row=header_row, column=ci, value=col.get("label") or col.get("field"))
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = border
    ws.row_dimensions[header_row].height = 36

    data_row = header_row + 1
    for ri, row in enumerate(rows, start=data_row):
        for ci, col in enumerate(columns, start=1):
            val = row.get(col["field"])
            cell = ws.cell(row=ri, column=ci, value=val)
            cell.font = body_font
            cell.border = border
            if isinstance(val, (int, float)) and col["field"] != "cvegeo":
                cell.alignment = Alignment(horizontal="right")

    for ci, col in enumerate(columns, start=1):
        letter = get_column_letter(ci)
        max_len = len(str(col.get("label") or ""))
        for row in rows[:200]:
            v = row.get(col["field"])
            if v is not None:
                max_len = max(max_len, len(str(v)))
        ws.column_dimensions[letter].width = min(max(max_len + 2, 12), 48)

    ws.freeze_panes = ws.cell(row=data_row, column=1)

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
