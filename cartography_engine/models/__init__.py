"""Modelos Pydantic de request/response del Cartography Engine."""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


class GenerateMapRequest(BaseModel):
    template_id: str = Field(..., min_length=1, description="ID de plantilla JSON")
    params: dict[str, Any] = Field(default_factory=dict)
    paper: Optional[
        Literal[
            "letter",
            "a4",
            "plotter_90x60",
            "plotter_90x70",
            "plotter_90x120",
            "dcarta_42x28",
        ]
    ] = Field(
        default=None, description="Si se omite, usa layout de plantilla / preset"
    )
    orientation: Optional[Literal["portrait", "landscape"]] = Field(
        default=None, description="Si se omite, usa layout de plantilla / preset"
    )
    format: Literal["pdf", "svg", "geopdf"] = "pdf"


class HealthResponse(BaseModel):
    engine: str
    version: str
    enabled: bool
    templates: list[str] = Field(default_factory=list)
    formats: list[str] = Field(default_factory=list)
    capabilities: list[str] = Field(default_factory=list)
    cartography_db: Optional[dict[str, Any]] = None


class QgisSymbolRequest(BaseModel):
    xml: str = Field(..., min_length=1, description="Fragmento XML de símbolo QGIS")


class CartographyError(Exception):
    """Error de dominio del engine (mapeable a HTTP)."""

    def __init__(self, code: str, message: str, status_code: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
