"""Catálogo de capas exportables del visor (data-driven desde config/visor/catalog.json)."""

from typing import Any, Dict, Optional, Sequence

from visor_catalog_loader import (
    catalog_for_api,
    denue_codigos_from_config,
    layer_catalog_from_config,
)
from visor_search_loader import search_config_for_api


def layer_catalog() -> Dict[str, Dict[str, Any]]:
    return layer_catalog_from_config()


def layer_config(layer_id: str) -> Optional[Dict[str, Any]]:
    key = (layer_id or "").strip().lower()
    if not key:
        return None
    return layer_catalog().get(key)


def denue_codigos_for_layer(layer_id: str) -> Optional[Sequence[int]]:
    """Códigos SCIAN (codigo_act) asociados a una capa DENUE del visor."""
    key = (layer_id or "").strip().lower()
    return denue_codigos_from_config(key)


def visor_catalog_payload() -> Dict[str, Any]:
    """Catálogo completo para el frontend del visor geográfico."""
    payload = catalog_for_api()
    payload["search"] = search_config_for_api()
    return payload
