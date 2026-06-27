"""Dependencias FastAPI — usuario admin autenticado."""

from typing import Any, Dict, Optional

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from auth.jwt_tokens import decode_access_token
from auth.users import get_user_by_id

_bearer = HTTPBearer(auto_error=False)


def _extract_bearer_token(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials],
) -> Optional[str]:
    if credentials and credentials.scheme.lower() == "bearer":
        token = (credentials.credentials or "").strip()
        if token:
            return token
    auth_hdr = (
        request.headers.get("authorization")
        or request.headers.get("Authorization")
        or request.headers.get("x-atlas-authorization")
        or request.headers.get("X-Atlas-Authorization")
    )
    if not auth_hdr:
        return None
    parts = auth_hdr.strip().split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    token = parts[1].strip()
    return token or None


def _resolve_admin_user(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials],
) -> tuple[Optional[Dict[str, Any]], Optional[str]]:
    token = _extract_bearer_token(request, credentials)
    if not token:
        return None, "MISSING_AUTH"
    payload = decode_access_token(token)
    if not payload:
        return None, "INVALID_TOKEN"
    user_id = payload.get("sub")
    if user_id is None:
        return None, "INVALID_TOKEN"
    user = get_user_by_id(int(user_id))
    if not user:
        return None, "USER_NOT_FOUND"
    if not user.get("active"):
        return None, "USER_INACTIVE"
    if str(user.get("role") or "").strip() != "visor_admin":
        return None, "FORBIDDEN_ROLE"
    return user, None


async def optional_admin_user(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
) -> Optional[Dict[str, Any]]:
    user, _reason = _resolve_admin_user(request, credentials)
    return user


async def require_admin_user(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
) -> Dict[str, Any]:
    user, reason = _resolve_admin_user(request, credentials)
    if user:
        return user
    messages = {
        "MISSING_AUTH": "Falta el encabezado Authorization (revisar proxy Nginx)",
        "INVALID_TOKEN": "Token JWT inválido o expirado",
        "USER_NOT_FOUND": "Usuario admin no encontrado en la base de datos",
        "USER_INACTIVE": "Usuario admin desactivado",
        "FORBIDDEN_ROLE": "El usuario no tiene rol visor_admin",
    }
    raise HTTPException(
        status_code=401,
        detail={
            "ok": False,
            "error": reason or "UNAUTHORIZED",
            "message": messages.get(reason or "", "Sesión admin requerida"),
        },
    )
