"""Login y sesión admin del Visor."""

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from auth.deps import require_admin_user
from auth.jwt_tokens import create_access_token
from auth.passwords import verify_password
from auth.users import get_user_by_username, touch_last_login

router = APIRouter(prefix="/api/admin", tags=["admin-auth"])


class LoginBody(BaseModel):
    username: str = Field(..., min_length=2, max_length=64)
    password: str = Field(..., min_length=4, max_length=128)


@router.post("/login")
def admin_login(body: LoginBody) -> Dict[str, Any]:
    user = get_user_by_username(body.username)
    if not user or not user.get("active"):
        raise HTTPException(
            status_code=401,
            detail={"ok": False, "error": "INVALID_CREDENTIALS", "message": "Usuario o contraseña incorrectos"},
        )
    if user.get("role") != "visor_admin":
        raise HTTPException(
            status_code=403,
            detail={"ok": False, "error": "FORBIDDEN", "message": "Sin permisos de administrador del visor"},
        )
    if not verify_password(body.password, user.get("password_hash") or ""):
        raise HTTPException(
            status_code=401,
            detail={"ok": False, "error": "INVALID_CREDENTIALS", "message": "Usuario o contraseña incorrectos"},
        )
    touch_last_login(int(user["id"]))
    token = create_access_token({"sub": int(user["id"]), "role": user["role"]})
    return {
        "ok": True,
        "token": token,
        "user": {
            "id": user["id"],
            "username": user["username"],
            "display_name": user.get("display_name") or user["username"],
            "role": user["role"],
        },
    }


@router.get("/me")
def admin_me(user: Dict[str, Any] = Depends(require_admin_user)) -> Dict[str, Any]:
    return {
        "ok": True,
        "user": {
            "id": user["id"],
            "username": user["username"],
            "display_name": user.get("display_name") or user["username"],
            "role": user["role"],
        },
    }
