"""Usuarios admin (esquema atlas_admin)."""

import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

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


def get_user_by_id(user_id: int, *, with_password: bool = False) -> Optional[Dict[str, Any]]:
    cols = "id, username, display_name, role, active, created_at, last_login"
    if with_password:
        cols = "id, username, password_hash, display_name, role, active, created_at, last_login"
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT {cols}
                  FROM {ADMIN_SCHEMA}.users
                 WHERE id = %s
                 LIMIT 1
                """,
                (int(user_id),),
            )
            return cur.fetchone()


def list_users() -> List[Dict[str, Any]]:
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT id, username, display_name, role, active, created_at, last_login
                  FROM {ADMIN_SCHEMA}.users
                 ORDER BY lower(username)
                """
            )
            return cur.fetchall()


def count_active_admins(exclude_user_id: Optional[int] = None) -> int:
    with get_db() as conn:
        with conn.cursor() as cur:
            if exclude_user_id is not None:
                cur.execute(
                    f"""
                    SELECT COUNT(*)::int AS n
                      FROM {ADMIN_SCHEMA}.users
                     WHERE role = 'visor_admin'
                       AND active = TRUE
                       AND id <> %s
                    """,
                    (int(exclude_user_id),),
                )
            else:
                cur.execute(
                    f"""
                    SELECT COUNT(*)::int AS n
                      FROM {ADMIN_SCHEMA}.users
                     WHERE role = 'visor_admin'
                       AND active = TRUE
                    """
                )
            row = cur.fetchone()
    return int((row or {}).get("n") or 0)


def update_user_password(user_id: int, password_hash: str) -> bool:
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE {ADMIN_SCHEMA}.users
                   SET password_hash = %s
                 WHERE id = %s
                """,
                (password_hash, int(user_id)),
            )
            return cur.rowcount > 0


def update_user_fields(
    user_id: int,
    *,
    display_name: Optional[str] = None,
    role: Optional[str] = None,
    active: Optional[bool] = None,
) -> bool:
    sets: List[str] = []
    params: List[Any] = []
    if display_name is not None:
        sets.append("display_name = %s")
        params.append(display_name.strip() or None)
    if role is not None:
        sets.append("role = %s")
        params.append(role)
    if active is not None:
        sets.append("active = %s")
        params.append(bool(active))
    if not sets:
        return False
    params.append(int(user_id))
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE {ADMIN_SCHEMA}.users
                   SET {", ".join(sets)}
                 WHERE id = %s
                """,
                params,
            )
            return cur.rowcount > 0


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
