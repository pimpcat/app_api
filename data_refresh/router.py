"""API REST Data Refresh Studio."""

from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field
from auth.deps import require_admin_user
from data_refresh.jobs_store import get_job, list_history_jobs, list_recent_jobs
from data_refresh.names import assert_job_id
from data_refresh.indicator_refresh import (
    apply_indicator_job,
    build_synthetic_indicator_xlsx,
    build_template_mold_xlsx,
    cancel_indicator_job,
    create_indicator_job_from_csv,
    list_indicator_jobs,
    list_indicator_templates,
)
from data_refresh.service import (
    apply_job,
    cancel_job,
    enqueue_job_from_upload,
    list_targets,
    meta,
    normalize_job_geometry,
    run_job_pipeline,
)
from data_refresh.versions import get_version, list_versions, restore_version

router = APIRouter(prefix="/api/data-refresh", tags=["data-refresh"])


def _user_id(user: Dict[str, Any]) -> int:
    return int(user["id"])


def _http_exc(exc: Exception, default_code: int = 400) -> HTTPException:
    msg = str(exc)
    code = default_code
    if (
        msg.startswith("JOB_NOT_FOUND")
        or msg.startswith("TARGET_NOT_FOUND")
        or msg.startswith("VERSION_NOT_FOUND")
        or msg.startswith("BACKUP_NOT_FOUND")
    ):
        code = 404
    return HTTPException(status_code=code, detail={"ok": False, "message": msg})


@router.get("/meta")
def data_refresh_meta(_user: Dict[str, Any] = Depends(require_admin_user)):
    try:
        return meta()
    except Exception as exc:
        raise HTTPException(500, detail={"ok": False, "message": str(exc)}) from exc


@router.get("/targets")
def data_refresh_targets(_user: Dict[str, Any] = Depends(require_admin_user)):
    try:
        return {"ok": True, "targets": list_targets()}
    except Exception as exc:
        raise HTTPException(500, detail={"ok": False, "message": str(exc)}) from exc


@router.get("/jobs")
def data_refresh_jobs_list(
    limit: int = 20, _user: Dict[str, Any] = Depends(require_admin_user)
):
    return {"ok": True, "jobs": list_recent_jobs(limit)}


@router.get("/history")
def data_refresh_history(
    limit: int = 50,
    kind: Optional[str] = None,
    status: Optional[str] = None,
    target_table: Optional[str] = None,
    _user: Dict[str, Any] = Depends(require_admin_user),
):
    return {
        "ok": True,
        "jobs": list_history_jobs(
            limit=limit, kind=kind, status=status, target_table=target_table
        ),
    }


@router.get("/versions")
def data_refresh_versions_list(
    table_name: Optional[str] = None,
    limit: int = 50,
    _user: Dict[str, Any] = Depends(require_admin_user),
):
    return {"ok": True, "versions": list_versions(table_name=table_name, limit=limit)}


@router.get("/versions/{version_id}")
def data_refresh_version_get(
    version_id: int, _user: Dict[str, Any] = Depends(require_admin_user)
):
    ver = get_version(version_id)
    if not ver:
        raise HTTPException(404, detail={"ok": False, "message": "VERSION_NOT_FOUND"})
    return {"ok": True, "version": ver}


@router.post("/versions/{version_id}/restore")
def data_refresh_version_restore(
    version_id: int, user: Dict[str, Any] = Depends(require_admin_user)
):
    try:
        result = restore_version(version_id, user_id=_user_id(user))
        return {"ok": True, **result}
    except ValueError as exc:
        raise _http_exc(exc) from exc
    except Exception as exc:
        raise HTTPException(
            500, detail={"ok": False, "message": str(exc)[:500]}
        ) from exc


@router.get("/jobs/{job_id}")
def data_refresh_job_get(
    job_id: str, _user: Dict[str, Any] = Depends(require_admin_user)
):
    try:
        jid = assert_job_id(job_id)
    except ValueError as exc:
        raise _http_exc(exc) from exc
    job = get_job(jid)
    if not job:
        raise HTTPException(404, detail={"ok": False, "message": "JOB_NOT_FOUND"})
    return {"ok": True, "job": job}


@router.post("/jobs")
async def data_refresh_job_create(
    background_tasks: BackgroundTasks,
    target_table: str = Form(...),
    file: UploadFile = File(...),
    user: Dict[str, Any] = Depends(require_admin_user),
):
    try:
        content = await file.read()
        job = enqueue_job_from_upload(
            content=content,
            filename=file.filename or "upload.zip",
            target_table=target_table,
            user_id=_user_id(user),
        )
        background_tasks.add_task(run_job_pipeline, str(job["id"]))
        return {"ok": True, "job": job, "async": True}
    except ValueError as exc:
        raise _http_exc(exc) from exc
    except Exception as exc:
        raise HTTPException(
            500, detail={"ok": False, "message": str(exc)[:500]}
        ) from exc


@router.post("/jobs/{job_id}/normalize-geometry")
def data_refresh_job_normalize_geometry(
    job_id: str,
    accept: bool = True,
    _user: Dict[str, Any] = Depends(require_admin_user),
):
    """Convierte MULTIPOINT→POINT en staging (si es seguro) o registra declinación."""
    try:
        job = normalize_job_geometry(job_id, accept=accept)
        return {"ok": True, "job": job}
    except ValueError as exc:
        raise _http_exc(exc) from exc
    except Exception as exc:
        raise HTTPException(
            500, detail={"ok": False, "message": str(exc)[:500]}
        ) from exc


@router.post("/jobs/{job_id}/apply")
def data_refresh_job_apply(
    job_id: str, user: Dict[str, Any] = Depends(require_admin_user)
):
    try:
        job = apply_job(job_id, user_id=_user_id(user))
        return {"ok": True, "job": job}
    except ValueError as exc:
        job = None
        try:
            job = get_job(assert_job_id(job_id))
        except Exception:
            pass
        raise HTTPException(
            status_code=400,
            detail={"ok": False, "message": str(exc), "job": job},
        ) from exc
    except Exception as exc:
        job = None
        try:
            job = get_job(assert_job_id(job_id))
        except Exception:
            pass
        raise HTTPException(
            status_code=500,
            detail={"ok": False, "message": str(exc)[:500], "job": job},
        ) from exc


@router.post("/jobs/{job_id}/cancel")
def data_refresh_job_cancel(
    job_id: str, _user: Dict[str, Any] = Depends(require_admin_user)
):
    try:
        # Detectar job de indicadores
        job = get_job(assert_job_id(job_id))
        if job and (job.get("report") or {}).get("kind") == "indicator":
            return {"ok": True, "job": cancel_indicator_job(job_id)}
        job = cancel_job(job_id)
        return {"ok": True, "job": job}
    except ValueError as exc:
        raise _http_exc(exc) from exc
    except Exception as exc:
        raise HTTPException(500, detail={"ok": False, "message": str(exc)}) from exc


@router.get("/indicators/templates")
def indicator_templates(_user: Dict[str, Any] = Depends(require_admin_user)):
    try:
        return {"ok": True, "templates": list_indicator_templates()}
    except Exception as exc:
        raise HTTPException(500, detail={"ok": False, "message": str(exc)}) from exc


@router.get("/indicators/templates/{template_id}/mold")
def indicator_template_mold(
    template_id: str, _user: Dict[str, Any] = Depends(require_admin_user)
):
    """Descarga molde Excel (municipios + columnas vacías; cve_mun texto 3 dígitos)."""
    try:
        filename, body = build_template_mold_xlsx(template_id)
        return Response(
            content=body,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
            },
        )
    except ValueError as exc:
        raise _http_exc(exc) from exc
    except Exception as exc:
        raise HTTPException(500, detail={"ok": False, "message": str(exc)}) from exc


class SyntheticIndicatorBody(BaseModel):
    template_id: str = Field(..., min_length=1)
    changes: int = Field(40, ge=1, le=5000)
    seed: int = Field(1234, ge=0, le=2_147_483_647)
    jitter_pct: float = Field(0.08, ge=0.01, le=0.5)


@router.post("/indicators/synthetic")
def indicator_synthetic_dataset(
    body: SyntheticIndicatorBody,
    _user: Dict[str, Any] = Depends(require_admin_user),
):
    """Genera Excel de prueba reproducible (seed) a partir de valores actuales de BD.

    No escribe en producción. Pensado para validar el pipeline de Indicator Refresh.
    """
    try:
        filename, xlsx, _meta = build_synthetic_indicator_xlsx(
            template_id=body.template_id,
            changes=body.changes,
            seed=body.seed,
            jitter_pct=body.jitter_pct,
        )
        return Response(
            content=xlsx,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "X-GroSIG-Seed": str(body.seed),
                "X-GroSIG-Changes": str(_meta.get("changes_applied", "")),
            },
        )
    except ValueError as exc:
        raise _http_exc(exc) from exc
    except Exception as exc:
        raise HTTPException(500, detail={"ok": False, "message": str(exc)[:500]}) from exc


@router.get("/indicators/jobs")
def indicator_jobs_list(
    limit: int = 15, _user: Dict[str, Any] = Depends(require_admin_user)
):
    return {"ok": True, "jobs": list_indicator_jobs(limit)}


@router.post("/indicators/jobs")
async def indicator_job_create(
    template_id: str = Form(...),
    file: UploadFile = File(...),
    user: Dict[str, Any] = Depends(require_admin_user),
):
    try:
        content = await file.read()
        job = create_indicator_job_from_csv(
            content=content,
            filename=file.filename or "upload.csv",
            template_id=template_id,
            user_id=_user_id(user),
        )
        return {"ok": True, "job": job}
    except ValueError as exc:
        raise _http_exc(exc) from exc
    except Exception as exc:
        raise HTTPException(
            500, detail={"ok": False, "message": str(exc)[:500]}
        ) from exc


@router.post("/indicators/jobs/{job_id}/apply")
def indicator_job_apply(
    job_id: str, user: Dict[str, Any] = Depends(require_admin_user)
):
    try:
        job = apply_indicator_job(job_id, user_id=_user_id(user))
        return {"ok": True, "job": job}
    except ValueError as exc:
        job = None
        try:
            job = get_job(assert_job_id(job_id))
        except Exception:
            pass
        raise HTTPException(
            status_code=400,
            detail={"ok": False, "message": str(exc), "job": job},
        ) from exc
    except Exception as exc:
        job = None
        try:
            job = get_job(assert_job_id(job_id))
        except Exception:
            pass
        raise HTTPException(
            status_code=500,
            detail={"ok": False, "message": str(exc)[:500], "job": job},
        ) from exc
