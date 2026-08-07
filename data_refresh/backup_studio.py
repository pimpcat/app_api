"""Backup Studio: ZIP de dumps + config (+ MBTiles opcional async)."""

from __future__ import annotations

import json
import logging
import os
import secrets
import shutil
import subprocess
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from config import get_settings

logger = logging.getLogger(__name__)

BACKUP_ROOT = Path(os.getenv("BACKUP_STUDIO_DIR", "/data/backups"))
KEEP_BACKUPS = int(os.getenv("BACKUP_STUDIO_KEEP", "5") or 5)
CONFIG_DIRS = [
    Path("/config/visor"),
    Path("/config/theme"),
    Path("/config/geography"),
    Path("/config/indicators"),
]
MARTIN_YAML = Path(os.getenv("MARTIN_YAML_PATH", "/config/martin.yaml"))
MBTILES_CANDIDATES = [
    Path(os.getenv("MBTILES_PATH", "/tiles/vector/mexico.mbtiles")),
    Path("/tiles/vector/mexico.mbtiles"),
]


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return f"{stamp}_{secrets.token_hex(4)}"


def _job_dir(backup_id: str) -> Path:
    return BACKUP_ROOT / backup_id


def _status_path(backup_id: str) -> Path:
    return _job_dir(backup_id) / "status.json"


def _write_status(backup_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
    d = _job_dir(backup_id)
    d.mkdir(parents=True, exist_ok=True)
    data = dict(data)
    data["id"] = backup_id
    data["updated_at"] = _iso_now()
    _status_path(backup_id).write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return data


def _read_status(backup_id: str) -> Optional[Dict[str, Any]]:
    p = _status_path(backup_id)
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _parse_db_url(url: str) -> Dict[str, str]:
    u = urlparse(url)
    return {
        "host": u.hostname or "db_mapas",
        "port": str(u.port or 5432),
        "user": u.username or "postgres",
        "password": u.password or "",
        "dbname": (u.path or "/atlas").lstrip("/") or "atlas",
    }


def _pg_dump_env(db: Dict[str, str]) -> Dict[str, str]:
    env = os.environ.copy()
    if db.get("password"):
        env["PGPASSWORD"] = db["password"]
    return env


def _run_pg_dump(db: Dict[str, str], out_file: Path, *, fmt: str = "custom") -> None:
    out_file.parent.mkdir(parents=True, exist_ok=True)
    args = [
        "pg_dump",
        "-h",
        db["host"],
        "-p",
        db["port"],
        "-U",
        db["user"],
        "-d",
        db["dbname"],
        "-F",
        "c" if fmt == "custom" else "p",
        "-f",
        str(out_file),
    ]
    proc = subprocess.run(
        args,
        env=_pg_dump_env(db),
        capture_output=True,
        text=True,
        timeout=3600,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"pg_dump {db['dbname']} falló: {(proc.stderr or proc.stdout or '')[:400]}"
        )


def _sanitize_martin_yaml(src: Path, dest: Path) -> None:
    text = src.read_text(encoding="utf-8", errors="replace")
    # Oculta passwords en connection strings
    import re

    text = re.sub(
        r"(postgresql://[^:]+:)([^@]+)(@)",
        r"\1***\3",
        text,
        flags=re.I,
    )
    dest.write_text(text, encoding="utf-8")


def _zip_dir(zf: zipfile.ZipFile, src: Path, arc_prefix: str) -> int:
    n = 0
    if not src.exists():
        return 0
    if src.is_file():
        zf.write(src, arcname=f"{arc_prefix}/{src.name}")
        return 1
    for p in src.rglob("*"):
        if p.is_file():
            zf.write(p, arcname=f"{arc_prefix}/{p.relative_to(src).as_posix()}")
            n += 1
    return n


def prune_old_backups(keep: int = KEEP_BACKUPS) -> List[str]:
    if not BACKUP_ROOT.is_dir():
        return []
    dirs = sorted(
        [p for p in BACKUP_ROOT.iterdir() if p.is_dir()],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    dropped: List[str] = []
    for p in dirs[keep:]:
        try:
            shutil.rmtree(p, ignore_errors=True)
            dropped.append(p.name)
        except Exception:
            pass
    return dropped


def list_backups(limit: int = 20) -> List[Dict[str, Any]]:
    if not BACKUP_ROOT.is_dir():
        return []
    items: List[Dict[str, Any]] = []
    for p in sorted(BACKUP_ROOT.iterdir(), key=lambda x: x.name, reverse=True):
        if not p.is_dir():
            continue
        st = _read_status(p.name)
        if st:
            items.append(st)
        if len(items) >= limit:
            break
    return items


def get_backup(backup_id: str) -> Optional[Dict[str, Any]]:
    return _read_status(backup_id)


def download_path(backup_id: str) -> Optional[Path]:
    st = _read_status(backup_id)
    if not st or st.get("status") != "ready":
        return None
    zip_name = st.get("zip_name") or "backup.zip"
    p = _job_dir(backup_id) / zip_name
    return p if p.is_file() else None


def enqueue_backup(
    *,
    include_atlas: bool = True,
    include_cartography: bool = True,
    include_config: bool = True,
    include_mbtiles: bool = False,
    user_id: Optional[int] = None,
) -> Dict[str, Any]:
    bid = _new_id()
    opts = {
        "include_atlas": bool(include_atlas),
        "include_cartography": bool(include_cartography),
        "include_config": bool(include_config),
        "include_mbtiles": bool(include_mbtiles),
    }
    if not any(opts.values()):
        raise ValueError("BACKUP_EMPTY_SELECTION")
    st = _write_status(
        bid,
        {
            "status": "queued",
            "created_at": _iso_now(),
            "user_id": user_id,
            "options": opts,
            "progress": 0,
            "label": "En cola",
            "error": None,
            "zip_name": None,
            "size_bytes": None,
        },
    )
    return st


def run_backup_job(backup_id: str) -> Dict[str, Any]:
    st = _read_status(backup_id)
    if not st:
        raise ValueError("BACKUP_NOT_FOUND")
    opts = st.get("options") or {}
    work = _job_dir(backup_id) / "work"
    work.mkdir(parents=True, exist_ok=True)
    artifacts: List[str] = []

    try:
        _write_status(
            backup_id,
            {**st, "status": "running", "progress": 5, "label": "Iniciando"},
        )
        settings = get_settings()

        if opts.get("include_atlas"):
            _write_status(
                backup_id,
                {
                    **(_read_status(backup_id) or st),
                    "status": "running",
                    "progress": 20,
                    "label": "pg_dump atlas",
                },
            )
            db = _parse_db_url(settings["database_url"])
            out = work / "atlas.dump"
            _run_pg_dump(db, out)
            artifacts.append(str(out))

        if opts.get("include_cartography"):
            carto_url = (settings.get("cartography_database_url") or "").strip()
            if carto_url:
                _write_status(
                    backup_id,
                    {
                        **(_read_status(backup_id) or st),
                        "status": "running",
                        "progress": 45,
                        "label": "pg_dump GroSIG_Cartography",
                    },
                )
                db = _parse_db_url(carto_url)
                out = work / "GroSIG_Cartography.dump"
                try:
                    _run_pg_dump(db, out)
                    artifacts.append(str(out))
                except Exception as exc:
                    logger.warning("Dump cartography omitido: %s", exc)
                    (work / "cartography_error.txt").write_text(
                        str(exc)[:500], encoding="utf-8"
                    )
                    artifacts.append(str(work / "cartography_error.txt"))
            else:
                (work / "cartography_skipped.txt").write_text(
                    "CARTOGRAPHY_DATABASE_URL no configurada", encoding="utf-8"
                )
                artifacts.append(str(work / "cartography_skipped.txt"))

        if opts.get("include_config"):
            _write_status(
                backup_id,
                {
                    **(_read_status(backup_id) or st),
                    "status": "running",
                    "progress": 65,
                    "label": "Empaquetando config",
                },
            )
            cfg_dir = work / "config"
            cfg_dir.mkdir(exist_ok=True)
            for src in CONFIG_DIRS:
                if src.is_dir():
                    dest = cfg_dir / src.name
                    if dest.exists():
                        shutil.rmtree(dest, ignore_errors=True)
                    shutil.copytree(src, dest, dirs_exist_ok=True)
            if MARTIN_YAML.is_file():
                _sanitize_martin_yaml(MARTIN_YAML, cfg_dir / "martin.yaml")
            manifest = {
                "created_at": _iso_now(),
                "note": "Config sanitizada; .env no incluido (secretos).",
                "dirs": [p.name for p in CONFIG_DIRS if p.is_dir()],
            }
            (cfg_dir / "MANIFEST.json").write_text(
                json.dumps(manifest, indent=2), encoding="utf-8"
            )
            artifacts.append(str(cfg_dir))

        if opts.get("include_mbtiles"):
            _write_status(
                backup_id,
                {
                    **(_read_status(backup_id) or st),
                    "status": "running",
                    "progress": 80,
                    "label": "Copiando MBTiles (puede tardar)",
                },
            )
            mb = next((p for p in MBTILES_CANDIDATES if p.is_file()), None)
            if mb:
                dest = work / mb.name
                shutil.copy2(mb, dest)
                artifacts.append(str(dest))
            else:
                (work / "mbtiles_missing.txt").write_text(
                    "No se encontró mexico.mbtiles en rutas montadas", encoding="utf-8"
                )
                artifacts.append(str(work / "mbtiles_missing.txt"))

        _write_status(
            backup_id,
            {
                **(_read_status(backup_id) or st),
                "status": "running",
                "progress": 90,
                "label": "Creando ZIP",
            },
        )
        zip_name = f"grosig_backup_{backup_id}.zip"
        zip_path = _job_dir(backup_id) / zip_name
        with zipfile.ZipFile(
            zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6
        ) as zf:
            readme = (
                "GroSIG Backup Studio\n"
                f"id={backup_id}\n"
                f"created={_iso_now()}\n"
                f"options={json.dumps(opts)}\n"
                "No incluye .env ni data_postgres.\n"
            )
            zf.writestr("README.txt", readme)
            for art in artifacts:
                p = Path(art)
                if p.is_dir():
                    _zip_dir(zf, p, p.name)
                elif p.is_file():
                    zf.write(p, arcname=p.name)

        size = zip_path.stat().st_size
        # Limpia work para ahorrar disco
        shutil.rmtree(work, ignore_errors=True)
        prune_old_backups(KEEP_BACKUPS)

        return _write_status(
            backup_id,
            {
                **(_read_status(backup_id) or st),
                "status": "ready",
                "progress": 100,
                "label": "Listo para descargar",
                "zip_name": zip_name,
                "size_bytes": size,
                "error": None,
            },
        )
    except Exception as exc:
        logger.exception("Backup %s falló", backup_id)
        return _write_status(
            backup_id,
            {
                **(_read_status(backup_id) or st),
                "status": "failed",
                "progress": 100,
                "label": "Error",
                "error": str(exc)[:800],
            },
        )


def meta() -> Dict[str, Any]:
    settings = get_settings()
    carto = bool((settings.get("cartography_database_url") or "").strip())
    mb = next((p for p in MBTILES_CANDIDATES if p.is_file()), None)
    pg_dump_ok = shutil.which("pg_dump") is not None
    return {
        "ok": True,
        "backup_dir": str(BACKUP_ROOT),
        "keep": KEEP_BACKUPS,
        "pg_dump_available": pg_dump_ok,
        "defaults": {
            "include_atlas": True,
            "include_cartography": carto,
            "include_config": True,
            "include_mbtiles": False,
        },
        "cartography_configured": carto,
        "mbtiles_available": mb is not None,
        "mbtiles_path": str(mb) if mb else None,
    }
