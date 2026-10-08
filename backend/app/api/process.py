"""人脸处理流水线触发（M3；R5 分批链式启动，ADR-022）。"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.db.models import Video
from app.db.session import get_db
from app.services import runtime_settings
from app.services.jobs import has_active_job, job_detail, submit_job
from app.services.pipeline import _pending_video_ids  # noqa: PLC2701 —— 同包内复用

router = APIRouter(tags=["process"])


class ReprocessIn(BaseModel):
    video_ids: list[int]


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


@router.post("/process/reprocess", status_code=202)
def reprocess_videos(body: ReprocessIn, db: Session = Depends(get_db)) -> dict:
    """将指定 done/failed 视频置回 pending 并提交独立小批任务。

    pipeline 会拒绝重处理已有评分/对比的面容；无人工信号的历史结果可以安全重建。
    """
    if not body.video_ids:
        raise HTTPException(status_code=422, detail={
            "code": "NO_VIDEOS", "message": "video_ids must not be empty"})
    if has_active_job(db, "process"):
        raise HTTPException(status_code=409, detail={
            "code": "JOB_RUNNING",
            "message": "a process job is already queued/running"})
    videos = db.execute(
        select(Video).where(Video.id.in_(body.video_ids),
                            Video.status.in_(("done", "failed")))
    ).scalars().all()
    if not videos:
        raise HTTPException(status_code=409, detail={
            "code": "NO_ELIGIBLE_VIDEOS",
            "message": "videos are missing or not done/failed"})
    for video in videos:
        video.status = "pending"
        video.status_msg = None
    db.commit()
    job = submit_job(db, "process", {
        "video_ids": [video.id for video in videos], "chain": False,
        "reprocess": True,
    })
    return {"job": job_detail(job)}


@router.post("/process/rebuild-faces", status_code=202)
def rebuild_faces(db: Session = Depends(get_db)) -> dict:
    """彻底重建面容库：分批重跑 done/failed/processing/pending 视频。

    已训练模型文件和 active_pers_model 版本不动；评分/对比人工数据保留。
    每批使用 process_batch_size，任务可暂停/取消；后续批次从 pending 继续。
    """
    if has_active_job(db, "process"):
        raise HTTPException(status_code=409, detail={
            "code": "JOB_RUNNING",
            "message": "a process job is already queued/running",
        })
    ids = db.execute(
        select(Video.id).where(Video.status.in_(("pending", "processing", "done", "failed")))
        .order_by(Video.id)
    ).scalars().all()
    if not ids:
        raise HTTPException(status_code=409, detail={
            "code": "NO_VIDEOS",
            "message": "no videos are available for face rebuilding",
        })
    batch_size = runtime_settings.get_value(db, "process_batch_size")
    db.execute(update(Video).values(
        status="pending", status_msg=None).where(Video.id.in_(ids)))
    db.commit()
    job = submit_job(db, "process", {
        "video_ids": ids[:batch_size], "chain": True, "rebuild": True,
        "force_refresh": True, "batch_index": 1,
        "total_batches": max(1, -(-len(ids) // batch_size)),
    })
    return {"job": job_detail(job), "total_videos": len(ids)}
