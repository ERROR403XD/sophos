"""人脸处理流水线触发（M3；R5 分批链式启动，ADR-022）。"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.services import runtime_settings
from app.services.jobs import has_active_job, job_detail, submit_job
from app.services.pipeline import _pending_video_ids  # noqa: PLC2701 —— 同包内复用

router = APIRouter(tags=["process"])


@router.post("/process/start", status_code=202)
def start_process(db: Session = Depends(get_db)) -> dict:
    """按 process_batch_size（runtime_settings 可覆盖）把 pending 视频切成
    批次，只提交**第一批**；该批完成且未被暂停/取消时由 handler 链式提交
    下一批。批间其他任务（scan/train）可自然插队（ADR-022）。"""
    if has_active_job(db, "process"):
        raise HTTPException(status_code=409, detail={
            "code": "JOB_RUNNING",
            "message": "a process job is already queued/running",
        })
    ids = _pending_video_ids(db)
    if not ids:
        raise HTTPException(status_code=409, detail={
            "code": "NO_PENDING_VIDEOS",
            "message": "no pending videos to process",
        })
    batch_size = runtime_settings.get_value(db, "process_batch_size")
    job = submit_job(db, "process", {
        "video_ids": ids[:batch_size], "chain": True, "batch_index": 1,
        "total_batches": max(1, -(-len(ids) // batch_size)),
    })
    return {"job": job_detail(job)}
