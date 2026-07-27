"""GeoPDF básico (Adobe Geospatial PDF): Viewport + Measure/GPTS/LPTS.

Sin GDAL: se etiqueta un PDF ReportLab existente con metadatos que Acrobat
y varios GIS leen para coordenadas al cursor sobre el marco del mapa.
"""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from typing import Optional, Sequence

from cartography_engine.layouts import Box
from cartography_engine.models import CartographyError


@dataclass(frozen=True)
class GeoViewport:
    """Un marco de mapa georreferenciado en una página del PDF (0-based)."""

    page_index: int
    map_frame: Box
    bounds_xy: tuple[float, float, float, float]  # minx, miny, maxx, maxy en CRS
    crs: str = "EPSG:32614"
    page_width: float = 612.0
    page_height: float = 792.0


def bounds_corners_xy(
    bounds: tuple[float, float, float, float],
) -> list[tuple[float, float]]:
    """Esquinas LL, LR, UR, UL en CRS proyectado."""
    minx, miny, maxx, maxy = bounds
    return [
        (minx, miny),
        (maxx, miny),
        (maxx, maxy),
        (minx, maxy),
    ]


def project_corners_to_wgs84(
    bounds: tuple[float, float, float, float],
    crs: str,
) -> list[tuple[float, float]]:
    """
    Transforma esquinas del bounds a WGS84.
    Devuelve lista de (lat, lon) en orden LL, LR, UR, UL.
    """
    try:
        from pyproj import Transformer
    except ImportError as exc:
        raise CartographyError(
            "GEOPDF_NO_PYPROJ",
            "pyproj es requerido para GeoPDF",
            status_code=500,
        ) from exc

    src = str(crs or "EPSG:32614").strip()
    if src.upper() in ("EPSG:4326", "WGS84", "CRS:84"):
        # Ya geográfico: bounds como lon/lat → (lat, lon)
        corners = bounds_corners_xy(bounds)
        return [(y, x) for x, y in corners]

    transformer = Transformer.from_crs(src, "EPSG:4326", always_xy=True)
    out: list[tuple[float, float]] = []
    for x, y in bounds_corners_xy(bounds):
        lon, lat = transformer.transform(x, y)
        out.append((float(lat), float(lon)))
    return out


def build_gpts_lpts(
    bounds: tuple[float, float, float, float],
    crs: str,
) -> tuple[list[float], list[float]]:
    """
    GPTS: lat,lon × 4 esquinas. LPTS: relativas al BBox del viewport (0–1).
    Orden: LL, LR, UR, UL.
    """
    latlon = project_corners_to_wgs84(bounds, crs)
    gpts: list[float] = []
    for lat, lon in latlon:
        gpts.extend([lat, lon])
    # Relativo al BBox del mapa (Adobe): esquina inferior-izq → (0,0)
    lpts = [0.0, 0.0, 1.0, 0.0, 1.0, 1.0, 0.0, 1.0]
    return gpts, lpts


def _bbox_from_frame(frame: Box) -> list[float]:
    return [float(frame.x), float(frame.y), float(frame.x2), float(frame.y2)]


def tag_pdf(pdf_bytes: bytes, viewports: Sequence[GeoViewport]) -> bytes:
    """Inyecta Viewport+Measure (GEO) en las páginas indicadas."""
    if not viewports:
        raise CartographyError(
            "GEOPDF_NO_VIEWPORTS",
            "GeoPDF requiere al menos un viewport con bounds",
        )
    try:
        import pikepdf
    except ImportError as exc:
        raise CartographyError(
            "GEOPDF_NO_PIKEPDF",
            "pikepdf es requerido para GeoPDF (pip install pikepdf)",
            status_code=500,
        ) from exc

    # Agrupar por página
    by_page: dict[int, list[GeoViewport]] = {}
    for vp in viewports:
        by_page.setdefault(int(vp.page_index), []).append(vp)

    with pikepdf.open(BytesIO(pdf_bytes)) as pdf:
        n_pages = len(pdf.pages)
        for page_index, vps in by_page.items():
            if page_index < 0 or page_index >= n_pages:
                raise CartographyError(
                    "GEOPDF_BAD_PAGE",
                    f"page_index {page_index} fuera de rango (0..{n_pages - 1})",
                )
            page = pdf.pages[page_index]
            vp_array = pikepdf.Array()
            for vp in vps:
                gpts, lpts = build_gpts_lpts(vp.bounds_xy, vp.crs)
                gcs = pikepdf.Dictionary(
                    {
                        "/Type": pikepdf.Name("/GEOGCS"),
                        "/EPSG": 4326,
                    }
                )
                measure = pikepdf.Dictionary(
                    {
                        "/Type": pikepdf.Name("/Measure"),
                        "/Subtype": pikepdf.Name("/GEO"),
                        "/Bounds": pikepdf.Array([0, 0, 0, 1, 1, 1, 1, 0]),
                        "/GPTS": pikepdf.Array(gpts),
                        "/LPTS": pikepdf.Array(lpts),
                        "/GCS": gcs,
                        "/PDU": pikepdf.Array(
                            [
                                pikepdf.Name("/m"),
                                pikepdf.Name("/km"),
                                pikepdf.Name("/deg"),
                            ]
                        ),
                    }
                )
                viewport = pikepdf.Dictionary(
                    {
                        "/Type": pikepdf.Name("/Viewport"),
                        "/BBox": pikepdf.Array(_bbox_from_frame(vp.map_frame)),
                        "/Name": "GroSIG map frame",
                        "/Measure": measure,
                    }
                )
                vp_array.append(viewport)
            page[pikepdf.Name("/VP")] = vp_array

        out = BytesIO()
        pdf.save(out)
        return out.getvalue()


def geopdf_available() -> bool:
    try:
        import pikepdf  # noqa: F401
        import pyproj  # noqa: F401

        return True
    except ImportError:
        return False
