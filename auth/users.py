"""Usuarios admin (esquema atlas_admin)."""

import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from database import get_db

ADMIN_SCHEMA = os.getenv("ATLAS_ADMIN_SCHEMA", "atlas_admin").strip() or "atlas_admin"


def get_user_by_username(username: str) -> Optional[Dict[str, Any]]:
    uname = (username or "").strip()
    if not uname:
        return None
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT id, username, password_hash, display_name, role, active
                  FROM {ADMIN_SCHEMA}.users
                 WHERE lower(username) = lower(%s)
                 LIMIT 1
                """,
                (uname,),
            )
            return cur.fetchone()


def get_user_by_id(user_id: int) -> Optional[Dict[str, Any]]:
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT id, username, display_name, role, active
                  FROM {ADMIN_SCHEMA}.users
                 WHERE id = %s
                 LIMIT 1
                """,
                (int(user_id),),
            )
            return cur.fetchone()


def touch_last_login(user_id: int) -> None:
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE {ADMIN_SCHEMA}.users
                   SET last_login = %s
                 WHERE id = %s
                """,
                (datetime.now(timezone.utc), int(user_id)),
            )


def create_user(username: str, password_hash: str, display_name: str = "", role: str = "visor_admin") -> int:
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO {ADMIN_SCHEMA}.users (username, password_hash, display_name, role)
                VALUES (%s, %s, %s, %s)
                RETURNING id
                """,
                (username.strip(), password_hash, display_name or None, role),
            )
            row = cur.fetchone()
            return int(row["id"])
