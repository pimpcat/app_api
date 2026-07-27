"""API REST del Cartography Engine (sin lógica de dibujo)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from cartography_engine.models import (
    CartographyError,
    GenerateMapRequest,
    HealthResponse,
    QgisSymbolRequest,
)

router = APIRouter(tags=["cartography"])


@router.get("/cartography/health", response_model=HealthResponse)
@router.get("/api/cartography/health", response_model=HealthResponse)
def cartography_health():
    from cartography_engine.services import health_payload

    return health_payload()


@router.post("/cartography/generate")
@router.post("/api/cartography/generate")
def cartography_generate(body: GenerateMapRequest):
    try:
        from cartography_engine.services import generate_map

        payload, filename, media_type = generate_map(body)
    except CartographyError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"ok": False, "error": exc.code, "message": exc.message},
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"ok": False, "error": "GENERATE_FAILED", "message": str(exc)},
        ) from exc

    return Response(
        content=payload,
        media_type=media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-GroSIG-Engine": "grosig-cartography",
            "X-GroSIG-Format": body.format,
        },
    )


@router.post("/cartography/symbols/from-qgis")
@router.post("/api/cartography/symbols/from-qgis")
def cartography_symbol_from_qgis(body: QgisSymbolRequest):
    """Importa un símbolo QGIS XML y lo convierte a GroSIG Symbol JSON."""
    try:
        from cartography_engine.qgis_parser import qgis_symbol_to_grosig

        symbol = qgis_symbol_to_grosig(body.xml)
        return {"ok": True, "symbol": symbol}
    except CartographyError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"ok": False, "error": exc.code, "message": exc.message},
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"ok": False, "error": "QGIS_PARSE_FAILED", "message": str(exc)},
        ) from exc
