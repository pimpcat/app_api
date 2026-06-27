"""Hash de contraseñas con bcrypt (Apache 2.0 — sin passlib)."""

import bcrypt


def hash_password(plain: str) -> str:
    data = plain.encode("utf-8")
    return bcrypt.hashpw(data, bcrypt.gensalt(rounds=12)).decode("utf-8")


def verify_password(plain: str, password_hash: str) -> bool:
    if not plain or not password_hash:
        return False
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), password_hash.encode("utf-8"))
    except Exception:
        return False
