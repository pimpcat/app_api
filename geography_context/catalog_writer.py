"""Escritura atómica del catálogo Geography Context."""

from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from config_json_errors import verify_json_roundtrip
from geography_context.catalog_loader import (
    geography_catalog_path,
    invalidate_geography_catalog_cache,
    load_geography_catalog_raw,
    validate_geography_catalog,
)
from visor_catalog_admin_service import record_audit


def _write_backup(path: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = path.with_name(f"catalog.json.bak.{stamp}")
    if path.is_file():
        shutil.copy2(path, backup)
        rolling = path.with_name("catalog.json.bak")
        shutil.copy2(path, rolling)
    return backup


def _audit_snapshot(catalog: Dict[str, Any]) -> Dict[str, Any]:
    tabs = catalog.get("tabs") or []
    return {
        "tabs_count": len(tabs),
        "tab_ids": [t.get("id") for t in tabs if isinstance(t, dict)],
        "menu": catalog.get("menu") or {},
    }


def save_geography_catalog(
    data: Dict[str, Any], *, user_id: Optional[int] = None
) -> Dict[str, Any]:
    before = load_geography_catalog_raw()
    validated = validate_geography_catalog(data)
    path = geography_catalog_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    backup = _write_backup(path)
    verify_json_roundtrip(validated)
    tmp = path.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(validated, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    os.replace(tmp, path)
    invalidate_geography_catalog_cache()
    if user_id is not None:
        record_audit(
            int(user_id),
            "replace_geography_catalog",
            "geography_context",
            _audit_snapshot(before),
            _audit_snapshot(validated),
        )
    return {
        "ok": True,
        "path": str(path),
        "tabs_count": len(validated.get("tabs") or []),
        "backup": str(backup) if backup.exists() else None,
        "catalog": validated,
    }
