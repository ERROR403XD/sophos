"""扫描触发（M2）。"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.services.jobs import has_active_job, job_detail, submit_job

router = APIRouter(tags=["scan"])


@router.post("/scan/start", status_code=202)
def start_scan(db: Session = Depends(get_db)) -> dict:
    if has_active_job(db, "scan"):
        raise HTTPException(status_code=409, detail={
            "code": "JOB_RUNNING",
            "message": "a scan job is already queued/running",
        })
    job = submit_job(db, "scan")
    return {"job": job_detail(job)}
