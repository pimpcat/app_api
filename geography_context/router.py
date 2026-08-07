"""API REST de GroSIG Geography Context."""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from auth.deps import require_admin_user
from config_json_errors import ConfigJsonSyntaxError
from geography_context.admin_service import (
    admin_meta,
    get_admin_catalog,
    list_table_columns,
    save_admin_catalog,
)
from geography_context.services import (
    get_contexto_all,
    get_contexto_row,
    health_payload,
    public_catalog_payload,
)
from tables import SCHEMA

router = APIRouter(tags=["geography-context"])


class CatalogBody(BaseModel):
    catalog: Dict[str, Any]


def _user_id(user: Dict[str, Any]) -> int:
    return int(user["id"])


@router.get("/geography-context/health")
@router.get("/api/geography-context/health")
def geography_health():
    return health_payload()


@router.get("/geography-context/catalog")
@router.get("/api/geography-context/catalog")
def geography_catalog_public():
    try:
        return public_catalog_payload()
    except FileNotFoundError as exc:
        raise HTTPException(
            503, detail={"ok": False, "error": "CATALOG_MISSING", "message": str(exc)}
        ) from exc
    except ConfigJsonSyntaxError as exc:
        raise HTTPException(
            503,
            detail={"ok": False, "error": "CONFIG_JSON_SYNTAX", "message": str(exc)},
        ) from exc
    except Exception as exc:
        raise HTTPException(
            500, detail={"ok": False, "error": "CATALOG_LOAD_FAILED", "message": str(exc)}
        ) from exc


@router.get("/geography-context/contexto")
@router.get("/api/geography-context/contexto")
def geography_contexto(cve_mun: str = Query(...)):
    row = get_contexto_row(cve_mun)
    return {"ok": True, "row": row}


@router.get("/geography-context/contexto/all")
@router.get("/api/geography-context/contexto/all")
def geography_contexto_all():
    return {"ok": True, "rows": get_contexto_all()}


@router.get("/api/geography-context/admin/meta")
def geography_admin_meta(_user: Dict[str, Any] = Depends(require_admin_user)):
    try:
        return admin_meta()
    except ConfigJsonSyntaxError as exc:
        raise HTTPException(
            503,
            detail={"ok": False, "error": "CONFIG_JSON_SYNTAX", "message": str(exc)},
        ) from exc
    except Exception as exc:
        raise HTTPException(500, detail={"ok": False, "message": str(exc)}) from exc


@router.get("/api/geography-context/admin/catalog")
def geography_admin_catalog_get(_user: Dict[str, Any] = Depends(require_admin_user)):
    try:
        return get_admin_catalog()
    except Exception as exc:
        raise HTTPException(500, detail={"ok": False, "message": str(exc)}) from exc


@router.put("/api/geography-context/admin/catalog")
def geography_admin_catalog_put(
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


@router.get("/api/geography-context/admin/tables/{table_name}/columns")
def geography_admin_table_columns(
    table_name: str, _user: Dict[str, Any] = Depends(require_admin_user)
):
    try:
        cols = list_table_columns(table_name)
        return {"ok": True, "table": table_name, "schema": SCHEMA, "columns": cols}
    except ValueError as exc:
        raise HTTPException(400, detail={"ok": False, "message": str(exc)}) from exc
    except Exception as exc:
        raise HTTPException(500, detail={"ok": False, "message": str(exc)}) from exc
