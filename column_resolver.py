"""Resolución de nombres de columnas vía information_schema (caché por tabla)."""

from typing import Dict, List, Optional, Sequence

_cache: Dict[str, Dict[str, str]] = {}


def clear_column_cache(schema: str, table: Optional[str] = None) -> None:
    """Invalida caché de columnas (p. ej. tras ALTER TABLE en runtime)."""
    if table is None:
        prefix = f"{schema}."
        for key in list(_cache):
            if key.startswith(prefix):
                del _cache[key]
        return
    _cache.pop(f"{schema}.{table}", None)


def resolve_column(
    conn,
    schema: str,
    table: str,
    candidates: Sequence[str],
) -> Optional[str]:
    key = f"{schema}.{table}"
    if key not in _cache:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT column_name
                  FROM information_schema.columns
                 WHERE table_schema = %s AND table_name = %s
                """,
                (schema, table),
            )
            cols = {}
            for row in cur.fetchall():
                cn = row["column_name"]
                if cn:
                    cols[cn.lower()] = cn
            _cache[key] = cols
    cols = _cache[key]
    for c in candidates:
        lc = c.lower()
        if lc in cols:
            return cols[lc]
    return None
