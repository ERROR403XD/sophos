"""接续训练 API（M6）；R11 增加个性化模型导出/导入。"""
from fastapi import APIRouter, Depends, HTTPException, UploadFile
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.config import settings
from app.db.session import get_db
from app.services import personalizer
from app.services.jobs import has_active_job, job_detail, submit_job
from app.services.personalizer import active_version, list_versions

router = APIRouter(prefix="/train", tags=["train"])


@router.post("/start", status_code=202)
def start_train(db: Session = Depends(get_db)) -> dict:
    if has_active_job(db, "train"):
        raise HTTPException(status_code=409, detail={
            "code": "JOB_RUNNING", "message": "a train job is already queued/running"})
    job = submit_job(db, "train")
    return {"job": job_detail(job)}


@router.get("/status")
def train_status(db: Session = Depends(get_db)) -> dict:
    """最新一次训练任务 + 当前启用版本。"""
    from app.db.models import Job
    from sqlalchemy import select
    job_row = db.execute(
        select(Job).where(Job.type == "train").order_by(Job.id.desc()).limit(1)
    ).scalar_one_or_none()
    return {
        "active_version": active_version(db),
        "last_job": job_detail(job_row) if job_row else None,
    }


@router.get("/versions")
def versions(db: Session = Depends(get_db)) -> dict:
    active = active_version(db)
    items = []
    for meta in list_versions(settings.final_models_dir()):
        meta["active"] = meta.get("version") == active
        items.append(meta)
    return {"versions": items, "active_version": active,
            "min_samples": None}  # 门槛见 personalizer.MIN_TOTAL_SAMPLES


@router.post("/activate/{version}", status_code=202)
def activate_version(version: str, db: Session = Depends(get_db)) -> dict:
    """启用个性化模型（R14 任务化）：apply 全库个性化分 + 聚合重算在 interactive
    池后台执行（大库分钟级，不再占住 HTTP 请求）；202 返回 job，前端轮询进度。"""
    if not personalizer.version_exists(settings.final_models_dir(), version):
        raise HTTPException(status_code=404, detail={
            "code": "NOT_FOUND", "message": f"version not found: {version}"})
    if has_active_job(db, "activate"):
        raise HTTPException(status_code=409, detail={
            "code": "ACTIVATE_RUNNING",
            "message": "an activate/deactivate job is already queued/running"})
    job = submit_job(db, "activate", {"version": version})
    return {"job": job_detail(job)}


@router.post("/deactivate", status_code=202)
def deactivate_version(db: Session = Depends(get_db)) -> dict:
    """停用个性化模型（R14 任务化）：清全库个性化分 + 聚合回退，后台执行。"""
    if has_active_job(db, "deactivate") or has_active_job(db, "activate"):
        raise HTTPException(status_code=409, detail={
            "code": "ACTIVATE_RUNNING",
            "message": "an activate/deactivate job is already queued/running"})
    job = submit_job(db, "deactivate")
    return {"job": job_detail(job)}


@router.get("/versions/{version}/export")
def export_version(version: str) -> Response:
    """导出一个个性化模型版本为单个 zip（模型参数 + 训练指标，几 KB）。"""
    try:
        data, filename = personalizer.export_version(settings.final_models_dir(), version)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail={  # noqa: B904
            "code": "NOT_FOUND", "message": f"version not found: {version}"})
    except ValueError as exc:
        raise HTTPException(status_code=500, detail={  # noqa: B904
            "code": "EXPORT_CORRUPT", "message": str(exc)})
    return Response(content=data, media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@router.post("/import")
async def import_version(file: UploadFile) -> dict:
    """导入导出包为新版本（版本号冲突自动重编号）；**不自动启用**——启用是显式动作。"""
    payload = await file.read()
    try:
        meta = personalizer.import_version(settings.final_models_dir(), payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={  # noqa: B904
            "code": "INVALID_EXPORT", "message": str(exc)})
    return {"ok": True, "meta": meta}
