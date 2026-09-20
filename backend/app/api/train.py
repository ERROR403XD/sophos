"""接续训练 API（M6）。"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.config import settings
from app.db.session import get_db
from app.services.jobs import has_active_job, job_detail, submit_job
from app.services.personalizer import (active_version, activate, deactivate,
                                       list_versions)

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


@router.post("/activate/{version}")
def activate_version(version: str, db: Session = Depends(get_db)) -> dict:
    try:
        info = activate(db, settings.final_models_dir(), version)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail={  # noqa: B904
            "code": "NOT_FOUND", "message": f"version not found: {version}"})
    return {"ok": True, **info}


@router.post("/deactivate")
def deactivate_version(db: Session = Depends(get_db)) -> dict:
    n = deactivate(db)
    return {"ok": True, "videos_recomputed": n}
