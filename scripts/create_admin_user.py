#!/usr/bin/env python3
"""Crea el primer usuario administrador del Visor (esquema atlas_admin)."""

from __future__ import annotations

import argparse
import getpass
import sys

from auth.passwords import hash_password
from auth.users import create_user, get_user_by_username


def main() -> int:
    parser = argparse.ArgumentParser(description="Alta de usuario admin del Visor geográfico")
    parser.add_argument("--username", "-u", required=True, help="Nombre de usuario")
    parser.add_argument("--display-name", "-d", default="", help="Nombre para mostrar")
    parser.add_argument(
        "--role",
        choices=("visor_admin", "viewer"),
        default="visor_admin",
        help="Rol (solo visor_admin puede publicar capas)",
    )
    args = parser.parse_args()

    if get_user_by_username(args.username):
        print(f"Error: el usuario '{args.username}' ya existe.", file=sys.stderr)
        return 1

    pwd1 = getpass.getpass("Contraseña: ")
    pwd2 = getpass.getpass("Repetir contraseña: ")
    if not pwd1 or pwd1 != pwd2:
        print("Error: las contraseñas no coinciden o están vacías.", file=sys.stderr)
        return 1
    if len(pwd1) < 8:
        print("Error: use al menos 8 caracteres.", file=sys.stderr)
        return 1

    user_id = create_user(
        args.username,
        hash_password(pwd1),
        display_name=args.display_name or args.username,
        role=args.role,
    )
    print(f"Usuario creado: id={user_id} username={args.username} role={args.role}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
