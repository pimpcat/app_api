"""API admin del catálogo de indicadores (Indicators Studio, Fase 11)."""

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from auth.deps import require_admin_user
from indicator_profiles import ProfileError
from config_json_errors import ConfigJsonSyntaxError
from indicators_admin_service import (
    admin_meta,
    delete_indicator,
    get_admin_catalog,
    list_indicators_audit,
    list_table_columns,
    save_admin_catalog,
    upsert_indicator,
)
from indicators_service import IndicatorError, build_indicator_payload

router = APIRouter(prefix="/api/indicators/admin", tags=["indicators-admin"])


class CatalogBody(BaseModel):
    catalog: Dict[str, Any]


class IndicatorBody(BaseModel):
    indicator: Dict[str, Any]


class PreviewBody(BaseModel):
    indicator_id: Optional[str] = Field(None, max_length=128)
    indicator: Optional[Dict[str, Any]] = None
    cve_mun: Optional[str] = None
    nom_mun: Optional[str] = None


def _user_id(user: Dict[str, Any]) -> int:
    return int(user["id"])


@router.get("/meta")
def indicators_admin_meta(_user=Depends(require_admin_user)):
    try:
        return admin_meta()
    except ConfigJsonSyntaxError as exc:
        raise HTTPException(
            503,
            detail={
                "ok": False,
                "error": "CONFIG_JSON_SYNTAX",
                "message": str(exc),
            },
        ) from exc
    except Exception as exc:
        raise HTTPException(500, detail={"ok": False, "message": str(exc)}) from exc


@router.get("/catalog")
def indicators_admin_catalog_get(_user=Depends(require_admin_user)):
    try:
        return get_admin_catalog()
    except Exception as exc:
        raise HTTPException(500, detail={"ok": False, "message": str(exc)}) from exc


@router.put("/catalog")
def indicators_admin_catalog_put(
    body: CatalogBody, user: Dict[str, Any] = Depends(require_admin_user)
):
    try:
        return save_admin_catalog(body.catalog, user_id=_user_id(user))
    except ValueError as exc:
        raise HTTPException(400, detail={"ok": False, "message": str(exc)}) from exc
    except OSError as exc:
        raise HTTPException(
            500,
            detail={
                "ok": False,
                "error": "CATALOG_WRITE_FAILED",
                "message": f"No se pudo escribir catalog.json (¿volumen rw?): {exc}",
            },
        ) from exc


@router.post("/indicators")
def indicators_admin_upsert(
    body: IndicatorBody, user: Dict[str, Any] = Depends(require_admin_user)
):
    try:
        return upsert_indicator(body.indicator, user_id=_user_id(user))
    except ValueError as exc:
        raise HTTPException(400, detail={"ok": False, "message": str(exc)}) from exc
    except OSError as exc:
        raise HTTPException(
            500,
            detail={"ok": False, "error": "CATALOG_WRITE_FAILED", "message": str(exc)},
        ) from exc


@router.delete("/indicators/{indicator_id}")
def indicators_admin_delete(
    indicator_id: str, user: Dict[str, Any] = Depends(require_admin_user)
):
    try:
        return delete_indicator(indicator_id, user_id=_user_id(user))
    except ValueError as exc:
        raise HTTPException(404, detail={"ok": False, "message": str(exc)}) from exc
    except OSError as exc:
        raise HTTPException(
            500,
            detail={"ok": False, "error": "CATALOG_WRITE_FAILED", "message": str(exc)},
        ) from exc


@router.get("/audit")
def indicators_admin_audit(
    limit: int = 50,
    offset: int = 0,
    action: Optional[str] = None,
    indicator_id: Optional[str] = None,
    _user: Dict[str, Any] = Depends(require_admin_user),
):
    try:
        result = list_indicators_audit(
            limit=limit,
            offset=offset,
            action=action,
            indicator_id=indicator_id,
        )
    except ValueError as exc:
        raise HTTPException(400, detail={"ok": False, "message": str(exc)}) from exc
    except Exception as exc:
        raise HTTPException(500, detail={"ok": False, "message": str(exc)}) from exc
    return {"ok": True, **result}


@router.get("/tables/{table_key}/columns")
def indicators_admin_columns(table_key: str, _user=Depends(require_admin_user)):
    try:
        return list_table_columns(table_key)
    except ValueError as exc:
        raise HTTPException(400, detail={"ok": False, "message": str(exc)}) from exc
    except Exception as exc:
        raise HTTPException(500, detail={"ok": False, "message": str(exc)}) from exc


@router.post("/preview")
def indicators_admin_preview(body: PreviewBody, _user=Depends(require_admin_user)):
    """Vista previa de datos: usa indicador ya publicado por id."""
    iid = (body.indicator_id or "").strip()
    if not iid:
        raise HTTPException(
            400,
            detail={"ok": False, "message": "Indique indicator_id para previsualizar"},
        )
    try:
        return build_indicator_payload(iid, body.cve_mun, body.nom_mun, allow_disabled=True)
    except IndicatorError as exc:
        raise HTTPException(
            exc.status,
            detail={"ok": False, "error": exc.code, "message": exc.message},
        ) from exc
    except ProfileError as exc:
        raise HTTPException(
            exc.status,
            detail={"ok": False, "error": exc.code, "message": exc.message},
        ) from exc
