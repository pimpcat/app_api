"""Gestión de usuarios admin del Visor (alta, edición, contraseñas)."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from auth.deps import require_admin_user
from auth.passwords import hash_password, verify_password
from auth.users import (
    count_active_admins,
    create_user,
    get_user_by_id,
    get_user_by_username,
    list_users,
    update_user_fields,
    update_user_password,
)
from visor_catalog_admin_service import record_audit

router = APIRouter(prefix="/api/admin", tags=["admin-users"])

_USERNAME_RE = re.compile(r"^[a-zA-Z0-9_]{2,64}$")
_VALID_ROLES = frozenset({"visor_admin", "viewer"})


class CreateUserBody(BaseModel):
    username: str = Field(..., min_length=2, max_length=64)
    password: str = Field(..., min_length=8, max_length=128)
    display_name: Optional[str] = Field(None, max_length=120)
    role: str = Field(default="visor_admin", max_length=32)


class PatchUserBody(BaseModel):
    display_name: Optional[str] = Field(None, max_length=120)
    role: Optional[str] = Field(None, max_length=32)
    active: Optional[bool] = None


class ChangePasswordBody(BaseModel):
    current_password: str = Field(..., min_length=4, max_length=128)
    new_password: str = Field(..., min_length=8, max_length=128)


class ResetPasswordBody(BaseModel):
    new_password: str = Field(..., min_length=8, max_length=128)


def _validate_username(username: str) -> str:
    uname = (username or "").strip()
    if not _USERNAME_RE.match(uname):
        raise HTTPException(
            status_code=400,
            detail={
                "ok": False,
                "error": "INVALID_USERNAME",
                "message": "Usuario: 2–64 caracteres (letras, números y guión bajo).",
            },
        )
    return uname


def _validate_role(role: str) -> str:
    value = (role or "").strip()
    if value not in _VALID_ROLES:
        raise HTTPException(
            status_code=400,
            detail={
                "ok": False,
                "error": "INVALID_ROLE",
                "message": "Rol inválido. Use visor_admin o viewer.",
            },
        )
    return value


def _serialize_user(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": row.get("id"),
        "username": row.get("username"),
        "display_name": row.get("display_name") or row.get("username"),
        "role": row.get("role"),
        "active": bool(row.get("active")),
        "created_at": row.get("created_at").isoformat() if row.get("created_at") else None,
        "last_login": row.get("last_login").isoformat() if row.get("last_login") else None,
    }


def _ensure_not_last_admin(user_id: int, *, new_role: Optional[str] = None, deactivate: bool = False) -> None:
    target = get_user_by_id(user_id)
    if not target:
        return
    if str(target.get("role") or "") != "visor_admin":
        return
    will_lose_admin = deactivate or (new_role is not None and new_role != "visor_admin")
    if not will_lose_admin:
        return
    if count_active_admins(exclude_user_id=user_id) < 1:
        raise HTTPException(
            status_code=400,
            detail={
                "ok": False,
                "error": "LAST_ADMIN",
                "message": "No puede quitar el último administrador activo del visor.",
            },
        )


@router.get("/users")
def admin_list_users(_user: Dict[str, Any] = Depends(require_admin_user)) -> Dict[str, Any]:
    rows = list_users()
    return {"ok": True, "users": [_serialize_user(r) for r in rows]}


@router.post("/users")
def admin_create_user(
    body: CreateUserBody,
    actor: Dict[str, Any] = Depends(require_admin_user),
) -> Dict[str, Any]:
    username = _validate_username(body.username)
    role = _validate_role(body.role)
    if get_user_by_username(username):
        raise HTTPException(
            status_code=409,
            detail={
                "ok": False,
                "error": "USERNAME_EXISTS",
                "message": f"El usuario «{username}» ya existe.",
            },
        )
    display = (body.display_name or username).strip()[:120] or username
    user_id = create_user(username, hash_password(body.password), display_name=display, role=role)
    created = get_user_by_id(user_id)
    record_audit(
        int(actor["id"]),
        "create_admin_user",
        username,
        None,
        {"username": username, "display_name": display, "role": role},
    )
    return {
        "ok": True,
        "user": _serialize_user(created or {"id": user_id, "username": username, "role": role, "active": True}),
        "message": "Usuario creado.",
    }


@router.patch("/users/{user_id}")
def admin_patch_user(
    user_id: int,
    body: PatchUserBody,
    actor: Dict[str, Any] = Depends(require_admin_user),
) -> Dict[str, Any]:
    target = get_user_by_id(user_id)
    if not target:
        raise HTTPException(
            status_code=404,
            detail={"ok": False, "error": "USER_NOT_FOUND", "message": "Usuario no encontrado."},
        )
    actor_id = int(actor["id"])
    target_id = int(user_id)
    if body.active is False and actor_id == target_id:
        raise HTTPException(
            status_code=400,
            detail={
                "ok": False,
                "error": "SELF_DEACTIVATE",
                "message": "No puede desactivar su propia cuenta.",
            },
        )
    new_role = _validate_role(body.role) if body.role is not None else None
    if new_role is not None and actor_id == target_id and new_role != "visor_admin":
        raise HTTPException(
            status_code=400,
            detail={
                "ok": False,
                "error": "SELF_DEMOTE",
                "message": "No puede cambiar su propio rol fuera de visor_admin desde aquí.",
            },
        )
    _ensure_not_last_admin(target_id, new_role=new_role, deactivate=body.active is False)
    before = _serialize_user(target)
    updated = update_user_fields(
        target_id,
        display_name=body.display_name,
        role=new_role,
        active=body.active,
    )
    if not updated:
        raise HTTPException(
            status_code=400,
            detail={"ok": False, "error": "NO_CHANGES", "message": "Sin cambios que aplicar."},
        )
    after_row = get_user_by_id(target_id)
    after = _serialize_user(after_row or target)
    record_audit(
        actor_id,
        "update_admin_user",
        str(target.get("username") or target_id),
        before,
        after,
    )
    return {"ok": True, "user": after, "message": "Usuario actualizado."}


@router.post("/me/password")
def admin_change_my_password(
    body: ChangePasswordBody,
    actor: Dict[str, Any] = Depends(require_admin_user),
) -> Dict[str, Any]:
    if body.current_password == body.new_password:
        raise HTTPException(
            status_code=400,
            detail={
                "ok": False,
                "error": "SAME_PASSWORD",
                "message": "La contraseña nueva debe ser distinta a la actual.",
            },
        )
    actor_id = int(actor["id"])
    row = get_user_by_id(actor_id, with_password=True)
    if not row or not verify_password(body.current_password, row.get("password_hash") or ""):
        raise HTTPException(
            status_code=401,
            detail={
                "ok": False,
                "error": "INVALID_PASSWORD",
                "message": "La contraseña actual no es correcta.",
            },
        )
    if not update_user_password(actor_id, hash_password(body.new_password)):
        raise HTTPException(
            status_code=500,
            detail={"ok": False, "error": "UPDATE_FAILED", "message": "No se pudo actualizar la contraseña."},
        )
    record_audit(
        actor_id,
        "change_password",
        str(actor.get("username") or actor_id),
        None,
        {"self": True},
    )
    return {"ok": True, "message": "Contraseña actualizada."}


@router.post("/users/{user_id}/password")
def admin_reset_user_password(
    user_id: int,
    body: ResetPasswordBody,
    actor: Dict[str, Any] = Depends(require_admin_user),
) -> Dict[str, Any]:
    target = get_user_by_id(user_id)
    if not target:
        raise HTTPException(
            status_code=404,
            detail={"ok": False, "error": "USER_NOT_FOUND", "message": "Usuario no encontrado."},
        )
    if not update_user_password(int(user_id), hash_password(body.new_password)):
        raise HTTPException(
            status_code=500,
            detail={"ok": False, "error": "UPDATE_FAILED", "message": "No se pudo actualizar la contraseña."},
        )
    username = str(target.get("username") or user_id)
    record_audit(
        int(actor["id"]),
        "reset_password",
        username,
        None,
        {"target_user_id": int(user_id), "target_username": username},
    )
    return {"ok": True, "message": f"Contraseña de «{username}» actualizada."}
