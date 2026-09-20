"""任务查询（M2）；R5 增加暂停/取消/恢复控制端点（ADR-022）。"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Job
from app.db.session import get_db
from app.services import jobs as jobsvc

router = APIRouter(prefix="/jobs", tags=["jobs"])


@router.get("")
def list_jobs(active: bool = False, limit: int = 50,
              db: Session = Depends(get_db)) -> dict:
    stmt = select(Job).order_by(Job.id.desc()).limit(min(max(1, limit), 200))
    if active:
        stmt = stmt.where(Job.status.in_(("queued", "running", "paused")))
    rows = db.execute(stmt).scalars().all()
    return {"items": [jobsvc.job_detail(j) for j in rows]}


@router.get("/{job_id}")
def get_job(job_id: int, db: Session = Depends(get_db)) -> dict:
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail={
            "code": "NOT_FOUND", "message": f"job {job_id} not found",
        })
    return jobsvc.job_detail(job)


def _control(fn, job_id: int, db: Session) -> dict:
    try:
        action = fn(db, job_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail={  # noqa: B904
            "code": "NOT_FOUND", "message": str(exc),
        })
    except ValueError as exc:
        raise HTTPException(status_code=409, detail={  # noqa: B904
            "code": "JOB_STATE", "message": str(exc),
        })
    return {"id": job_id, "action": action}


@router.post("/{job_id}/pause", status_code=200)
def pause_job(job_id: int, db: Session = Depends(get_db)) -> dict:
    """暂停任务：queued 直接置 paused；running 等待下一个检查点生效。"""
    return _control(jobsvc.request_pause, job_id, db)


@router.post("/{job_id}/cancel", status_code=200)
def cancel_job(job_id: int, db: Session = Depends(get_db)) -> dict:
    """取消任务：queued/paused 直接置 cancelled；running 等待下一个检查点。"""
    return _control(jobsvc.request_cancel, job_id, db)


@router.post("/{job_id}/resume", status_code=200)
def resume_job(job_id: int, db: Session = Depends(get_db)) -> dict:
    """恢复 paused 任务：状态回 queued 并重新入队（handler 对部分进度幂等）。"""
    return _control(jobsvc.request_resume, job_id, db)
