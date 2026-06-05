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
