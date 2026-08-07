"""Escritura atómica de config/theme/catalog.json."""

from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

from config_json_errors import verify_json_roundtrip
from theme_catalog_loader import (
    invalidate_theme_catalog_cache,
    load_theme_catalog_raw,
    theme_catalog_path,
)
from theme_catalog_validate import validate_theme_catalog


def load_catalog_mutable() -> Dict[str, Any]:
    data = load_theme_catalog_raw()
    return json.loads(json.dumps(data))


def _write_backup(path: Path) -> None:
    if not path.is_file():
        return
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = path.with_name(f"catalog.json.bak.{stamp}")
    shutil.copy2(path, backup)
    rolling = path.with_name("catalog.json.bak")
    shutil.copy2(path, rolling)


def save_theme_catalog(data: Dict[str, Any]) -> Path:
    validated = validate_theme_catalog(data)
    path = theme_catalog_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    _write_backup(path)
    verify_json_roundtrip(validated)
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(validated, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    os.replace(tmp, path)
    invalidate_theme_catalog_cache()
    return path
