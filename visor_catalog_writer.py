"""Lectura / escritura atómica de config/visor/catalog.json."""

from __future__ import annotations

import json
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from visor_catalog_loader import catalog_path, load_visor_catalog_raw


def _catalog_file() -> Path:
    return Path(catalog_path())


def load_catalog_mutable() -> Dict[str, Any]:
    data = load_visor_catalog_raw()
    return json.loads(json.dumps(data))


def invalidate_visor_catalog_cache() -> None:
    load_visor_catalog_raw.cache_clear()


def _write_backup(path: Path) -> None:
    if not path.is_file():
        return
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = path.with_name(f"catalog.json.bak.{stamp}")
    shutil.copy2(path, backup)
    rolling = path.with_name("catalog.json.bak")
    shutil.copy2(path, rolling)


def save_catalog(data: Dict[str, Any]) -> Path:
    path = _catalog_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    _write_backup(path)
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    os.replace(tmp, path)
    invalidate_visor_catalog_cache()
    return path


def catalog_table_names(catalog: Optional[Dict[str, Any]] = None) -> set[str]:
    raw = catalog or load_visor_catalog_raw()
    layers = raw.get("layers") or {}
    out: set[str] = set()
    if not isinstance(layers, dict):
        return out
    for entry in layers.values():
        if not isinstance(entry, dict):
            continue
        data = entry.get("data") or {}
        for key in ("table", "export_table", "gid_table"):
            val = data.get(key)
            if val:
                out.add(str(val).strip().lower())
    return out


def catalog_layer_ids(catalog: Optional[Dict[str, Any]] = None) -> set[str]:
    raw = catalog or load_visor_catalog_raw()
    layers = raw.get("layers") or {}
    if not isinstance(layers, dict):
        return set()
    return {str(k).strip().lower() for k in layers.keys()}


def _slug_layer_id(value: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", (value or "").strip().lower()).strip("_")
    return s[:64] or "capa"


def _overlay_key_from_layer_id(layer_id: str) -> str:
    parts = layer_id.split("_")
    return parts[0] + "".join(p[:1].upper() + p[1:] for p in parts[1:] if p)


def _checkbox_id_from_layer_id(layer_id: str) -> str:
    ok = _overlay_key_from_layer_id(layer_id)
    return "visor" + ok[:1].upper() + ok[1:]


def append_layer_to_group(catalog: Dict[str, Any], group_id: str, layer_id: str) -> None:
    groups = catalog.setdefault("groups", [])
    if not isinstance(groups, list):
        raise ValueError("INVALID_CATALOG: groups debe ser array")
    target = None
    for grp in groups:
        if isinstance(grp, dict) and grp.get("id") == group_id:
            target = grp
            break
    if target is None:
        raise ValueError(f"UNKNOWN_GROUP:{group_id}")
    layers = target.setdefault("layers", [])
    if layer_id not in layers:
        layers.append(layer_id)


def find_layer_group_id(catalog: Dict[str, Any], layer_id: str) -> Optional[str]:
    groups = catalog.get("groups") or []
    if not isinstance(groups, list):
        return None
    for grp in groups:
        if not isinstance(grp, dict):
            continue
        layers = grp.get("layers") or []
        if layer_id in layers:
            return str(grp.get("id") or "")
    return None


def remove_layer_from_groups(catalog: Dict[str, Any], layer_id: str) -> None:
    groups = catalog.get("groups") or []
    if not isinstance(groups, list):
        return
    for grp in groups:
        if not isinstance(grp, dict):
            continue
        layers = grp.get("layers")
        if not isinstance(layers, list):
            continue
        grp["layers"] = [lid for lid in layers if lid != layer_id]


def replace_layer_entry(
    catalog: Dict[str, Any],
    layer_id: str,
    entry: Dict[str, Any],
    group_id: str,
) -> Tuple[Dict[str, Any], str, Optional[Dict[str, Any]]]:
    lid = _slug_layer_id(layer_id)
    layers = catalog.setdefault("layers", {})
    if not isinstance(layers, dict):
        raise ValueError("INVALID_CATALOG: layers debe ser objeto")
    before = layers.get(lid)
    if before is None:
        raise ValueError(f"LAYER_NOT_FOUND:{lid}")

    entry = dict(entry)
    entry.setdefault("overlay_key", before.get("overlay_key") or _overlay_key_from_layer_id(lid))
    entry.setdefault("checkbox_id", before.get("checkbox_id") or _checkbox_id_from_layer_id(lid))
    layers[lid] = entry

    current_group = find_layer_group_id(catalog, lid)
    if current_group != group_id:
        remove_layer_from_groups(catalog, lid)
        append_layer_to_group(catalog, group_id, lid)

    return catalog, lid, before


def delete_layer_entry(catalog: Dict[str, Any], layer_id: str) -> Tuple[Dict[str, Any], str, Optional[Dict[str, Any]]]:
    lid = _slug_layer_id(layer_id)
    layers = catalog.get("layers") or {}
    if not isinstance(layers, dict) or lid not in layers:
        raise ValueError(f"LAYER_NOT_FOUND:{lid}")
    before = layers.pop(lid)
    remove_layer_from_groups(catalog, lid)
    return catalog, lid, before


def merge_layer_entry(
    catalog: Dict[str, Any],
    layer_id: str,
    entry: Dict[str, Any],
    group_id: str,
) -> Tuple[Dict[str, Any], str, Optional[Dict[str, Any]]]:
    lid = _slug_layer_id(layer_id)
    layers = catalog.setdefault("layers", {})
    if not isinstance(layers, dict):
        raise ValueError("INVALID_CATALOG: layers debe ser objeto")
    before = layers.get(lid)
    if before is not None:
        raise ValueError(f"LAYER_EXISTS:{lid}")

    entry = dict(entry)
    entry.setdefault("overlay_key", _overlay_key_from_layer_id(lid))
    entry.setdefault("checkbox_id", _checkbox_id_from_layer_id(lid))
    layers[lid] = entry
    append_layer_to_group(catalog, group_id, lid)
    return catalog, lid, before
