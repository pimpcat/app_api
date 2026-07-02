"""Rate limiting en memoria para endpoints sensibles (p. ej. login admin)."""

from __future__ import annotations

import os
import time
from collections import defaultdict
from threading import Lock
from typing import DefaultDict, List

from fastapi import HTTPException, Request


class SlidingWindowRateLimiter:
    def __init__(self, max_attempts: int, window_seconds: float) -> None:
        self.max_attempts = max(1, int(max_attempts))
        self.window_seconds = max(1.0, float(window_seconds))
        self._events: DefaultDict[str, List[float]] = defaultdict(list)
        self._lock = Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            bucket = [t for t in self._events[key] if t > now - self.window_seconds]
            if len(bucket) >= self.max_attempts:
                self._events[key] = bucket
                return False
            bucket.append(now)
            self._events[key] = bucket
            return True


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


admin_login_limiter = SlidingWindowRateLimiter(
    max_attempts=_env_int("ADMIN_LOGIN_RATE_LIMIT", 5),
    window_seconds=float(_env_int("ADMIN_LOGIN_RATE_WINDOW_SEC", 60)),
)


def client_ip(request: Request) -> str:
    forwarded = (request.headers.get("x-forwarded-for") or "").strip()
    if forwarded:
        return forwarded.split(",")[0].strip() or "unknown"
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


def enforce_admin_login_rate_limit(request: Request) -> None:
    ip = client_ip(request)
    if admin_login_limiter.allow(ip):
        return
    raise HTTPException(
        status_code=429,
        detail={
            "ok": False,
            "error": "RATE_LIMITED",
            "message": "Demasiados intentos de inicio de sesión. Espere un minuto e intente de nuevo.",
        },
    )
