"""Índices sugeridos y aplicación segura para tablas PostGIS del visor."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Set, Tuple

from psycopg import sql

from config import get_settings
from database import get_db

_IDENT_RE = re.compile(r"^[a-z_][a-z0-9_]*$", re.I)

BY_ATTRIBUTE_PRESETS = frozenset(
    {
        "point_by_attribute",
        "line_by_attribute",
        "polygon_by_attribute",
        "point_symbol_by_attribute",
    }
)


def _validate_table(table: str) -> str:
    name = (table or "").strip()
    if not name or not name.replace("_", "").isalnum():
        raise ValueError("INVALID_TABLE")
    return name


def _validate_ident(name: str) -> str:
    ident = (name or "").strip()
    if not ident or not _IDENT_RE.match(ident):
        raise ValueError("INVALID_IDENTIFIER")
    return ident


def _schema() -> str:
    return get_settings().get("schema") or "atlas"


def _assert_publishable_table(table: str) -> None:
    from visor_catalog_admin_service import catalog_table_names, fetch_martin_table_ids

    name = table.lower()
    try:
        martin_ids = {t.lower() for t in fetch_martin_table_ids()}
    except RuntimeError:
        martin_ids = set()
    in_catalog = name in catalog_table_names()
    if name not in martin_ids and not in_catalog:
        raise ValueError("TABLE_NOT_PUBLISHABLE")


def _table_has_column(table: str, column: str) -> bool:
    schema = _schema()
    col = (column or "").strip()
    if not col:
        return False
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT 1
                  FROM information_schema.columns
                 WHERE table_schema = %s
                   AND table_name = %s
                   AND column_name = %s
                 LIMIT 1
                """,
                (schema, table, col),
            )
            return cur.fetchone() is not None


def suggested_index_name(table: str, column: str, gist: bool = False) -> str:
    slug = re.sub(r"[^a-z0-9_]+", "_", f"{table}_{column}", flags=re.I)[:48]
    return f"idx_{slug}_gist" if gist else f"idx_{slug}"


def _parse_indexdef(index_def: Optional[str]) -> Tuple[str, List[str]]:
    """Extrae método (gist/btree/…) y columnas desde pg_indexes.indexdef."""
    if not index_def:
        return "btree", []
    text = index_def.strip()
    method_m = re.search(r"USING\s+(\w+)", text, re.I)
    method = (method_m.group(1) if method_m else "btree").lower()

    inner: Optional[str] = None
    using_m = re.search(r"USING\s+\w+\s*\(([^)]+)\)", text, re.I)
    if using_m:
        inner = using_m.group(1)
    else:
        plain_m = re.search(r'ON\s+[\w."`]+\s*\(([^)]+)\)\s*$', text, re.I)
        if plain_m:
            inner = plain_m.group(1)

    cols: List[str] = []
    if inner:
        for part in inner.split(","):
            token = part.strip().strip('"')
            if re.match(r"^[a-z_][a-z0-9_]*$", token, re.I):
                cols.append(token)
            else:
                col_m = re.search(r"\b([a-z_][a-z0-9_]*)\s*$", token, re.I)
                if col_m:
                    cols.append(col_m.group(1))
    return method, cols


def _column_in_indexdef(index_def: str, column: str) -> bool:
    if not index_def or not column:
        return False
    col = column.strip()
    if not col:
        return False
    return bool(re.search(rf'["\']?{re.escape(col)}["\']?', index_def, re.I))


def _index_coverage(table: str) -> List[Dict[str, Any]]:
    """Índices reales de la tabla (pg_indexes); no depende del nombre sugerido por el wizard."""
    schema = _schema()
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT indexname AS index_name, indexdef AS index_def
                  FROM pg_indexes
                 WHERE schemaname = %s AND tablename = %s
                 ORDER BY indexname
                """,
                (schema, table),
            )
            rows = cur.fetchall()
    out: List[Dict[str, Any]] = []
    for row in rows:
        index_def = str(row.get("index_def") or "")
        method, cols = _parse_indexdef(index_def)
        out.append(
            {
                "index_name": row.get("index_name"),
                "method": method,
                "columns": cols,
                "index_def": index_def,
            }
        )
    return out


def list_table_indexes(table: str) -> Dict[str, Any]:
    name = _validate_table(table)
    _assert_publishable_table(name)
    schema = _schema()
    existing = [
        {
            "index_name": item.get("index_name"),
            "method": item.get("method"),
            "columns": item.get("columns") or [],
            "index_def": item.get("index_def"),
        }
        for item in _index_coverage(name)
    ]
    return {"table": name, "schema": schema, "existing_indexes": existing}


def _find_covering_index(
    coverage: List[Dict[str, Any]],
    column: str,
    method: str,
) -> Optional[Dict[str, Any]]:
    """True si ya hay un índice compatible en la columna (cualquier nombre)."""
    col_l = column.lower()
    method_l = (method or "btree").lower()
    for item in coverage:
        cols = [str(c).lower() for c in (item.get("columns") or [])]
        index_def = str(item.get("index_def") or "")
        idx_method = str(item.get("method") or "btree").lower()

        col_match = col_l in cols or _column_in_indexdef(index_def, col_l)
        if not col_match:
            continue

        if method_l == "gist":
            if idx_method == "gist" or re.search(r"USING\s+gist", index_def, re.I):
                return item
            continue

        if idx_method in ("gist", "spgist"):
            continue
        return item
    return None


def _build_sql(schema: str, table: str, column: str, index_name: str, method: str) -> str:
    sch = _validate_ident(schema)
    tbl = _validate_ident(table)
    idx = _validate_ident(index_name)
    col = _validate_ident(column)
    fq = f"{sch}.{tbl}"
    if method == "gist":
        return f"CREATE INDEX IF NOT EXISTS {idx} ON {fq} USING GIST ({col});"
    return f"CREATE INDEX IF NOT EXISTS {idx} ON {fq} ({col});"


def suggest_indexes(table: str, payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    name = _validate_table(table)
    schema = _schema()
    entries: List[Dict[str, Any]] = []
    seen: Set[str] = set()

    def add(column: Optional[str], reason: str, gist: bool = False) -> None:
        col = (column or "").strip()
        if not col:
            return
        try:
            _validate_ident(col)
        except ValueError:
            return
        key = col.lower()
        if key in seen:
            return
        seen.add(key)
        method = "gist" if gist else "btree"
        idx_name = suggested_index_name(name, col, gist=gist)
        entries.append(
            {
                "column": col,
                "method": method,
                "reason": reason,
                "index_name": idx_name,
                "sql": _build_sql(schema, name, col, idx_name, method),
            }
        )

    add("the_geom", "Intersección espacial (análisis, identify, exportación)", True)

    data = payload.get("data") or {}
    if data.get("mun_filter") is not False:
        add("cve_mun", "Filtro municipal del visor (mun_filter: cve_mun)")

    filt = data.get("filter") or {}
    if isinstance(filt, dict) and filt.get("field"):
        add(str(filt["field"]), "Filtro de atributo en catálogo")

    style = payload.get("style") or {}
    preset = str(payload.get("style_preset") or "")
    if preset in BY_ATTRIBUTE_PRESETS and style.get("field"):
        add(str(style["field"]), "Estilo por atributo")

    search = payload.get("search") or {}
    if isinstance(search, dict) and search.get("enabled"):
        if search.get("name_column"):
            add(str(search["name_column"]), "Buscador (columna nombre)")
        if search.get("id_column"):
            add(str(search["id_column"]), "Buscador (columna id)")
        for col in search.get("search_columns") or []:
            add(str(col), "Buscador (columna de texto)")

    labels = payload.get("labels") or {}
    if isinstance(labels, dict) and labels.get("field"):
        add(str(labels["field"]), "Etiquetas en mapa")

    if name.lower() == "c_denue":
        add("codigo_act", "Filtro DENUE por actividad económica")

    caps = payload.get("capabilities") or {}
    spatial = payload.get("spatial_analysis") or {}
    if caps.get("spatial_analysis") and isinstance(spatial, dict):
        if spatial.get("modo") == "agregacion":
            for sec in spatial.get("sections") or []:
                if not isinstance(sec, dict):
                    continue
                for field in sec.get("campos") or []:
                    if isinstance(field, dict):
                        add(field.get("columna") or field.get("column"), "Análisis espacial (agregación)")
        if spatial.get("detail_table"):
            for field in spatial.get("detail_columns") or []:
                if isinstance(field, dict):
                    add(field.get("columna") or field.get("column"), "Análisis espacial (tabla detalle)")

    for col in data.get("export_columns") or []:
        add(str(col), "Exportación tabular")

    return entries


def build_index_plan(table: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    name = _validate_table(table)
    _assert_publishable_table(name)
    coverage = _index_coverage(name)
    suggestions = suggest_indexes(name, payload)
    for item in suggestions:
        match = _find_covering_index(coverage, item["column"], item["method"])
        item["exists"] = match is not None
        item["existing_index"] = match.get("index_name") if match else None
    base = list_table_indexes(name)
    base["suggestions"] = suggestions
    base["missing_count"] = sum(1 for s in suggestions if not s.get("exists"))
    return base


def apply_table_indexes(
    table: str,
    items: List[Dict[str, Any]],
    *,
    only_missing: bool = True,
) -> Dict[str, Any]:
    name = _validate_table(table)
    _assert_publishable_table(name)
    schema = _schema()
    coverage = _index_coverage(name)

    created: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []

    for raw in items or []:
        col = (raw.get("column") or "").strip()
        method = str(raw.get("method") or "btree").lower()
        reason = str(raw.get("reason") or "")
        idx_name = (raw.get("index_name") or "").strip() or suggested_index_name(
            name, col, gist=method == "gist"
        )
        try:
            _validate_ident(col)
            _validate_ident(idx_name)
        except ValueError:
            errors.append({"column": col, "message": "Identificador inválido"})
            continue
        if method not in ("gist", "btree"):
            errors.append({"column": col, "message": f"Método no permitido: {method}"})
            continue
        if not _table_has_column(name, col):
            errors.append({"column": col, "message": "Columna no encontrada en la tabla"})
            continue

        if only_missing and _find_covering_index(coverage, col, method):
            skipped.append(
                {
                    "column": col,
                    "method": method,
                    "index_name": idx_name,
                    "reason": reason,
                    "message": "Ya existe un índice compatible",
                }
            )
            continue

        try:
            with get_db() as conn:
                with conn.cursor() as cur:
                    if method == "gist":
                        stmt = sql.SQL("CREATE INDEX IF NOT EXISTS {} ON {}.{} USING GIST ({})").format(
                            sql.Identifier(idx_name),
                            sql.Identifier(schema),
                            sql.Identifier(name),
                            sql.Identifier(col),
                        )
                    else:
                        stmt = sql.SQL("CREATE INDEX IF NOT EXISTS {} ON {}.{} ({})").format(
                            sql.Identifier(idx_name),
                            sql.Identifier(schema),
                            sql.Identifier(name),
                            sql.Identifier(col),
                        )
                    cur.execute(stmt)
        except Exception as exc:  # noqa: BLE001 — devolver detalle al admin
            errors.append({"column": col, "index_name": idx_name, "message": str(exc)})
            continue

        created.append(
            {
                "column": col,
                "method": method,
                "index_name": idx_name,
                "reason": reason,
                "sql": _build_sql(schema, name, col, idx_name, method),
            }
        )
        coverage = _index_coverage(name)

    return {
        "table": name,
        "schema": schema,
        "created": created,
        "skipped": skipped,
        "errors": errors,
    }
