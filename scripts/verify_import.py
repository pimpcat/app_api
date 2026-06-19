#!/usr/bin/env python3
"""Comprueba que FastAPI arranca (ejecutar desde app_api/)."""
from main import app

print("OK", app.title, "rutas:", len(app.routes))
