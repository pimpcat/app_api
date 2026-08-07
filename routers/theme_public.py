"""API pública del catálogo de temas UI (portal)."""

from fastapi import APIRouter, HTTPException

from config_json_errors import ConfigJsonSyntaxError
from theme_catalog_loader import load_theme_schema_raw, public_theme_payload

router = APIRouter(tags=["theme"])


@router.get("/theme/catalog")
@router.get("/api/theme/catalog")
def theme_catalog_public():
    try:
        return public_theme_payload()
    except FileNotFoundError as exc:
        raise HTTPException(
            404,
            detail={"ok": False, "error": "THEME_CATALOG_MISSING", "message": str(exc)},
        ) from exc
    except ConfigJsonSyntaxError as exc:
        raise HTTPException(
            503,
            detail={"ok": False, "error": "CONFIG_JSON_SYNTAX", "message": str(exc)},
        ) from exc
    except Exception as exc:
        raise HTTPException(
            500, detail={"ok": False, "message": str(exc)}
        ) from exc


@router.get("/theme/schema")
@router.get("/api/theme/schema")
def theme_schema_public():
    """Schema (whitelist) también público: Theme Studio y docs lo usan."""
    try:
        return {"ok": True, "schema": load_theme_schema_raw()}
    except FileNotFoundError as exc:
        raise HTTPException(
            404,
            detail={"ok": False, "error": "THEME_SCHEMA_MISSING", "message": str(exc)},
        ) from exc
    except Exception as exc:
        raise HTTPException(
            500, detail={"ok": False, "message": str(exc)}
        ) from exc
