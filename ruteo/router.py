"""
Endpoints HTTP del módulo de ruteo (RNC / pgRouting).
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from ruteo import RuteoError, buscar_localidades_rnc, calcular_ruta_rnc

router = APIRouter(tags=["ruteo"])


@router.get("/ruteo/localidades")
@router.get("/api/ruteo/localidades")
def ruteo_localidades(
    q: str = Query("", min_length=0),
    cve_mun: Optional[str] = Query(None),
    limit: int = Query(60, ge=1, le=200),
):
    """Catálogo de localidades sobre la RNC para combos origen/destino."""
    try:
        rows = buscar_localidades_rnc(q=q, cve_mun=cve_mun, limit=limit)
        return {"ok": True, "count": len(rows), "rows": rows}
    except RuteoError as exc:
        raise HTTPException(
            status_code=400,
            detail={"ok": False, "error": exc.code, "message": exc.message},
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"ok": False, "error": "QUERY_FAILED", "message": str(exc)},
        ) from exc


@router.get("/ruteo")
@router.get("/api/ruteo")
def ruteo_calcular(
    cvegeo_origen: str = Query(..., min_length=1),
    cvegeo_destino: str = Query(..., min_length=1),
    usar_peajes: bool = Query(
        True,
        description="Si es false, evita carreteras de peaje en el cálculo.",
    ),
):
    """Ruta óptima entre dos localidades (GeoJSON FeatureCollection)."""
    try:
        return calcular_ruta_rnc(cvegeo_origen, cvegeo_destino, usar_peajes=usar_peajes)
    except RuteoError as exc:
        status = 404 if exc.code in (
            "LOC_NOT_FOUND",
            "NO_ROUTE",
            "VERTEX_NOT_FOUND",
            "TOLL_ROUTE",
        ) else 400
        raise HTTPException(
            status_code=status,
            detail={"ok": False, "error": exc.code, "message": exc.message},
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"ok": False, "error": "QUERY_FAILED", "message": str(exc)},
        ) from exc
