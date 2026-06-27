"""JWT para sesiones admin (python-jose — MIT)."""

import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from jose import JWTError, jwt

from config import get_settings


def _jwt_secret() -> str:
    """Lee el secreto en caliente (evita caché obsoleta si cambió el entorno)."""
    return os.getenv("JWT_SECRET", "").strip() or (get_settings().get("jwt_secret") or "")


def create_access_token(payload: Dict[str, Any], expires_hours: Optional[int] = None) -> str:
    settings = get_settings()
    secret = _jwt_secret()
    if not secret:
        raise RuntimeError("JWT_SECRET no configurado en el entorno")
    hours = expires_hours if expires_hours is not None else int(settings.get("jwt_expire_hours") or 8)
    data = dict(payload)
    if data.get("sub") is not None:
        data["sub"] = str(data["sub"])
    now = datetime.now(timezone.utc)
    data["iat"] = int(now.timestamp())
    data["exp"] = int((now + timedelta(hours=hours)).timestamp())
    token = jwt.encode(data, secret, algorithm="HS256")
    return token if isinstance(token, str) else token.decode("utf-8")


def decode_access_token(token: str) -> Optional[Dict[str, Any]]:
    secret = _jwt_secret()
    if not secret or not token:
        return None
    try:
        return jwt.decode(token, secret, algorithms=["HS256"])
    except JWTError:
        return None
