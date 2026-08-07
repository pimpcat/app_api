"""API Backup Studio (admin)."""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from auth.deps import require_admin_user
from data_refresh import backup_studio as bs

router = APIRouter(prefix="/api/admin/backups", tags=["admin-backups"])


def _user_id(user: Dict[str, Any]) -> int:
    return int(user["id"])


class BackupCreateBody(BaseModel):
    include_atlas: bool = True
    include_cartography: bool = True
    include_config: bool = True
    include_mbtiles: bool = False


@router.get("/meta")
def backups_meta(_user: Dict[str, Any] = Depends(require_admin_user)):
    return bs.meta()


@router.get("/")
def backups_list(limit: int = 20, _user: Dict[str, Any] = Depends(require_admin_user)):
    return {"ok": True, "backups": bs.list_backups(limit)}


@router.post("/")
def backups_create(
    body: BackupCreateBody,
    background_tasks: BackgroundTasks,
    user: Dict[str, Any] = Depends(require_admin_user),
):
    try:
        job = bs.enqueue_backup(
            include_atlas=body.include_atlas,
            include_cartography=body.include_cartography,
            include_config=body.include_config,
            include_mbtiles=body.include_mbtiles,
            user_id=_user_id(user),
        )
        background_tasks.add_task(bs.run_backup_job, str(job["id"]))
        return {"ok": True, "backup": job, "async": True}
    except ValueError as exc:
        raise HTTPException(
            400, detail={"ok": False, "message": str(exc)}
        ) from exc
    except Exception as exc:
        raise HTTPException(
            500, detail={"ok": False, "message": str(exc)[:500]}
        ) from exc


@router.get("/{backup_id}")
def backups_get(backup_id: str, _user: Dict[str, Any] = Depends(require_admin_user)):
    job = bs.get_backup(backup_id)
    if not job:
        raise HTTPException(404, detail={"ok": False, "message": "BACKUP_NOT_FOUND"})
    return {"ok": True, "backup": job}


@router.get("/{backup_id}/download")
def backups_download(
    backup_id: str, _user: Dict[str, Any] = Depends(require_admin_user)
):
    path = bs.download_path(backup_id)
    if not path:
        job = bs.get_backup(backup_id)
        if not job:
            raise HTTPException(404, detail={"ok": False, "message": "BACKUP_NOT_FOUND"})
        raise HTTPException(
            400,
            detail={
                "ok": False,
                "message": f"BACKUP_NOT_READY:{job.get('status')}",
            },
        )
    return FileResponse(
        path,
        media_type="application/zip",
        filename=path.name,
    )
