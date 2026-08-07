"""API admin del catálogo de temas (Theme Studio)."""

from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from auth.deps import require_admin_user
from config_json_errors import ConfigJsonSyntaxError
from theme_catalog_loader import (
    load_theme_catalog_raw,
    load_theme_defaults_raw,
    load_theme_schema_raw,
    theme_catalog_path,
)
from theme_catalog_writer import save_theme_catalog

router = APIRouter(prefix="/api/theme/admin", tags=["theme-admin"])


class CatalogBody(BaseModel):
    catalog: Dict[str, Any]


@router.get("/meta")
def theme_admin_meta(_user=Depends(require_admin_user)):
    try:
        path = theme_catalog_path()
        defaults = None
        try:
            defaults = load_theme_defaults_raw()
        except FileNotFoundError:
            defaults = None
        return {
            "ok": True,
            "path": str(path),
            "schema": load_theme_schema_raw(),
            "defaults": defaults,
        }
    except FileNotFoundError as exc:
        raise HTTPException(
            404, detail={"ok": False, "error": "THEME_MISSING", "message": str(exc)}
        ) from exc
    except ConfigJsonSyntaxError as exc:
        raise HTTPException(
            503,
            detail={"ok": False, "error": "CONFIG_JSON_SYNTAX", "message": str(exc)},
        ) from exc


@router.get("/catalog")
def theme_admin_catalog_get(_user=Depends(require_admin_user)):
    try:
        return {"ok": True, "catalog": load_theme_catalog_raw()}
    except Exception as exc:
        raise HTTPException(500, detail={"ok": False, "message": str(exc)}) from exc


@router.put("/catalog")
def theme_admin_catalog_put(
    body: CatalogBody, _user=Depends(require_admin_user)
):
    try:
        path = save_theme_catalog(body.catalog)
        return {
            "ok": True,
            "path": str(path),
            "catalog": load_theme_catalog_raw(),
        }
    except ValueError as exc:
        raise HTTPException(
            400, detail={"ok": False, "error": "VALIDATION", "message": str(exc)}
        ) from exc
    except OSError as exc:
        raise HTTPException(
            500,
            detail={
                "ok": False,
                "error": "WRITE_FAILED",
                "message": f"No se pudo escribir el catálogo: {exc}",
            },
        ) from exc
    except Exception as exc:
        raise HTTPException(500, detail={"ok": False, "message": str(exc)}) from exc
