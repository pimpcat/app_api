"""Conexión PostgreSQL/PostGIS (psycopg 3)."""

from contextlib import contextmanager
from typing import Any, Iterator

import psycopg
from psycopg.rows import dict_row

from config import get_settings


def connect():
    settings = get_settings()
    return psycopg.connect(
        settings["database_url"],
        row_factory=dict_row,
        options=f"-c search_path={settings['schema']},public",
    )


def connect_cartography():
    """Conexión a GroSIG_Cartography (schemas mgn/info50k/marco/aux)."""
    settings = get_settings()
    url = (settings.get("cartography_database_url") or "").strip()
    if not url:
        raise RuntimeError(
            "CARTOGRAPHY_DATABASE_URL no configurada (base GroSIG_Cartography)"
        )
    return psycopg.connect(
        url,
        row_factory=dict_row,
        options="-c search_path=mgn,info50k,marco,aux,public",
    )


@contextmanager
def get_db() -> Iterator[Any]:
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def get_cartography_db() -> Iterator[Any]:
    conn = connect_cartography()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def cartography_db_status() -> dict[str, Any]:
    """Ping + schemas presentes (para /health). Nunca lanza."""
    settings = get_settings()
    url = (settings.get("cartography_database_url") or "").strip()
    if not url:
        return {"ok": False, "configured": False, "error": "CARTOGRAPHY_DATABASE_URL vacía"}
    try:
        with get_cartography_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT nspname
                      FROM pg_namespace
                     WHERE nspname = ANY(%s)
                     ORDER BY 1
                    """,
                    (["mgn", "info50k", "marco", "aux"],),
                )
                schemas = [r["nspname"] for r in (cur.fetchall() or [])]
        return {
            "ok": True,
            "configured": True,
            "database": settings.get("cartography_database_name") or "GroSIG_Cartography",
            "schemas": schemas,
        }
    except Exception as exc:
        return {
            "ok": False,
            "configured": True,
            "error": str(exc)[:240],
        }
