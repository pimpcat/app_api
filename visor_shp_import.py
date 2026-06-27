"""Importación de shapefile (.shp o .zip) a PostGIS para Visor Studio."""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from config import get_settings
from database import get_db


def _slug_table(value: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "_", (value or "").lower()).strip("_")
    if not base:
        raise ValueError("INVALID_TABLE_NAME")
    if not base.startswith("c_"):
        base = f"c_{base}"
    return base[:63]


def _pg_connection_string() -> str:
    settings = get_settings()
    url = settings.get("database_url") or ""
    if not url:
        raise RuntimeError("DATABASE_URL missing")
    parsed = urlparse(url.replace("postgresql://", "postgres://", 1))
    host = parsed.hostname or "db_mapas"
    port = parsed.port or 5432
    user = parsed.username or ""
    password = parsed.password or ""
    dbname = (parsed.path or "").lstrip("/") or settings.get("database_name") or "atlas"
    parts = [f"host={host}", f"port={port}", f"dbname={dbname}"]
    if user:
        parts.append(f"user={user}")
    if password:
        parts.append(f"password={password}")
    return "PG:" + " ".join(parts)


def _extract_shp_dir(archive_path: Path) -> Path:
    if archive_path.suffix.lower() == ".zip":
        dest = archive_path.parent / "shp_extract"
        if dest.exists():
            shutil.rmtree(dest)
        dest.mkdir(parents=True)
        with zipfile.ZipFile(archive_path, "r") as zf:
            zf.extractall(dest)
        shp_files = list(dest.rglob("*.shp"))
        if not shp_files:
            raise ValueError("ZIP_WITHOUT_SHP")
        return shp_files[0].parent
    if archive_path.suffix.lower() == ".shp":
        return archive_path.parent
    raise ValueError("UNSUPPORTED_FORMAT")


def _find_shp_file(folder: Path) -> Path:
    shp_files = sorted(folder.glob("*.shp"))
    if not shp_files:
        shp_files = sorted(folder.rglob("*.shp"))
    if not shp_files:
        raise ValueError("SHP_NOT_FOUND")
    return shp_files[0]


def _ogr2ogr_import(shp_path: Path, table: str, schema: str) -> None:
    conn = _pg_connection_string()
    qualified = f"{schema}.{table}"
    cmd = [
        "ogr2ogr",
        "-overwrite",
        "-f",
        "PostgreSQL",
        conn,
        "-lco",
        f"SCHEMA={schema}",
        "-lco",
        "GEOMETRY_NAME=the_geom",
        "-lco",
        "FID=gid",
        "-nlt",
        "PROMOTE_TO_MULTI",
        "-t_srs",
        "EPSG:3857",
        "-nln",
        qualified,
        str(shp_path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "ogr2ogr failed").strip()
        raise RuntimeError(f"OGR2OGR:{err[:500]}")


def _table_exists(schema: str, table: str) -> bool:
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT 1 FROM information_schema.tables
                 WHERE table_schema = %s AND table_name = %s
                """,
                (schema, table),
            )
            return cur.fetchone() is not None


def _geometry_type(schema: str, table: str) -> str:
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT type FROM geometry_columns
                 WHERE f_table_schema = %s AND f_table_name = %s AND f_geometry_column = 'the_geom'
                """,
                (schema, table),
            )
            row = cur.fetchone()
    if not row or not row.get("type"):
        return "point"
    gtype = str(row["type"]).upper()
    if "LINE" in gtype:
        return "line"
    if "POLYGON" in gtype:
        return "polygon"
    return "point"


def _list_columns(schema: str, table: str) -> List[Dict[str, str]]:
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT column_name, data_type, udt_name
                  FROM information_schema.columns
                 WHERE table_schema = %s AND table_name = %s
                 ORDER BY ordinal_position
                """,
                (schema, table),
            )
            rows = cur.fetchall()
    skip = {"the_geom", "geom", "wkb_geometry"}
    return [
        {
            "name": r["column_name"],
            "type": r["data_type"],
            "udt": r["udt_name"],
        }
        for r in rows
        if (r["column_name"] or "").lower() not in skip
        and r["udt_name"] not in ("geometry", "geography")
    ]


def _feature_count(schema: str, table: str) -> int:
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(f'SELECT COUNT(*) AS n FROM "{schema}"."{table}"')
            row = cur.fetchone()
    return int(row["n"]) if row else 0


def import_shapefile(
    content: bytes,
    filename: str,
    table_hint: Optional[str] = None,
) -> Dict[str, Any]:
    if not content:
        raise ValueError("EMPTY_FILE")
    settings = get_settings()
    schema = settings.get("schema") or "atlas"
    safe_name = _slug_table(table_hint or Path(filename or "upload").stem)
    if _table_exists(schema, safe_name):
        raise ValueError(f"TABLE_EXISTS:{safe_name}")

    suffix = Path(filename or "").suffix.lower()
    if suffix not in (".shp", ".zip"):
        raise ValueError("UNSUPPORTED_FORMAT")

    with tempfile.TemporaryDirectory(prefix="visor_shp_") as tmp:
        tmp_path = Path(tmp)
        archive = tmp_path / f"upload{suffix}"
        archive.write_bytes(content)
        folder = _extract_shp_dir(archive)
        shp_path = _find_shp_file(folder)
        _ogr2ogr_import(shp_path, safe_name, schema)

    geometry = _geometry_type(schema, safe_name)
    columns = _list_columns(schema, safe_name)
    count = _feature_count(schema, safe_name)
    return {
        "table": safe_name,
        "geometry": geometry,
        "columns": columns,
        "feature_count": count,
        "needs_martin_restart": True,
        "in_martin": False,
    }
