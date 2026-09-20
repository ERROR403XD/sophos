"""后台任务 worker（M2 实现；R5 增加暂停/取消/恢复）。

设计（docs/ARCHITECTURE.md §3.1、ADR-011、ADR-022）：
- 进程内单 worker 线程 + FIFO 队列，串行执行（MVP 不引入 celery/redis）
- job 表：type/status/done/total/params/result/error
- 状态机：queued → running → done | failed | cancelled | paused
  （R5：queued/running 均可 → cancelled；running → paused → resume 回 queued）
- 取消/暂停为**协作式**：API 置位请求标志，handler 在检查点
  （process 每处理完一个视频、scan 每处理完一个目录）调用 check_point()
  抛出 JobInterrupted，由 worker 落终态；queued 状态直接改库，worker
  取出时发现非 queued 即跳过。
- paused 的 job 不占用队列（其余任务正常排队）；恢复 = 状态回 queued 并
  重新入队，handler 需对部分进度幂等（process/scan 天然满足）。
- 启动时 reset_stale_jobs：上次进程遗留的 queued/running 置 failed，
  processing 的视频回退 pending（断点续跑语义）；paused 保留（仍可恢复）。
- 具体处理器在 services/pipeline.py 注册，本模块保持通用
"""
from __future__ import annotations

import json
import logging
import queue
import threading
import traceback
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import session as db_session_mod
from app.db.models import Job, Video, utcnow

log = logging.getLogger("sophos.jobs")

_queue: queue.Queue = queue.Queue()
_lock = threading.Lock()
_worker_started = False

# R5：协作式暂停/取消请求标志（job_id 集合），API 线程写、worker 线程读
_pause_requested: set[int] = set()
_cancel_requested: set[int] = set()

HandlerFn = Callable[[Session, Job, dict], None]
HANDLERS: dict[str, HandlerFn] = {}


class JobInterrupted(RuntimeError):
    """handler 在检查点被要求暂停/取消（R5）。

    kind = "paused" | "cancelled"；worker 据此落 job 终态。
    """

    def __init__(self, kind: str, message: str = ""):
        super().__init__(message or f"job {kind} by user")
        self.kind = kind


def register_handler(job_type: str, fn: HandlerFn) -> None:
    HANDLERS[job_type] = fn


def check_point(session: Session, job: Job) -> None:
    """handler 协作检查点：有暂停/取消请求时先提交已有进度再抛出。

    必须在"一个完整工作单元"边界调用（调用方保证此刻 session 处于可提交
    状态）；抛出前 commit 一次，保证 done/total 等进度落库。
    """
    with _lock:
        paused = job.id in _pause_requested
        cancelled = job.id in _cancel_requested
    if paused or cancelled:
        try:
            session.commit()
        except Exception:  # noqa: BLE001 —— 进度提交失败不掩盖中断本身
            session.rollback()
        kind = "paused" if paused else "cancelled"
        with _lock:
            _pause_requested.discard(job.id)
            _cancel_requested.discard(job.id)
        raise JobInterrupted(kind)


def _take_request(job_id: int) -> str | None:
    with _lock:
        if job_id in _cancel_requested:
            _cancel_requested.discard(job_id)
            return "cancelled"
        if job_id in _pause_requested:
            _pause_requested.discard(job_id)
            return "paused"
    return None


def submit_job(session: Session, job_type: str, params: dict | None = None) -> Job:
    job = Job(type=job_type, status="queued",
              params=json.dumps(params or {}, ensure_ascii=False))
    session.add(job)
    session.commit()
    session.refresh(job)
    _queue.put(job.id)
    return job


def has_active_job(session: Session, job_type: str) -> bool:
    row = session.execute(
        select(Job.id).where(Job.type == job_type,
                             Job.status.in_(("queued", "running"))).limit(1)
    ).scalar_one_or_none()
    return row is not None


def request_pause(session: Session, job_id: int) -> str:
    """暂停任务；返回动作说明（"paused" 直接置态 / "requested" 等检查点）。"""
    job = session.get(Job, job_id)
    if job is None:
        raise LookupError(f"job {job_id} not found")
    if job.status == "queued":
        job.status = "paused"
        session.commit()
        return "paused"
    if job.status == "running":
        with _lock:
            _pause_requested.add(job_id)
        return "requested"
    raise ValueError(f"job {job_id} is {job.status}; cannot pause")


def request_cancel(session: Session, job_id: int) -> str:
    """取消任务；返回动作说明（"cancelled" 直接置态 / "requested" 等检查点）。"""
    job = session.get(Job, job_id)
    if job is None:
        raise LookupError(f"job {job_id} not found")
    if job.status in ("queued", "paused"):
        job.status = "cancelled"
        job.finished_at = utcnow()
        job.error = "cancelled by user"
        session.commit()
        return "cancelled"
    if job.status == "running":
        with _lock:
            _cancel_requested.add(job_id)
        return "requested"
    raise ValueError(f"job {job_id} is {job.status}; cannot cancel")


def request_resume(session: Session, job_id: int) -> None:
    """恢复 paused 任务：状态回 queued 并重新入队（handler 对部分进度幂等）。"""
    job = session.get(Job, job_id)
    if job is None:
        raise LookupError(f"job {job_id} not found")
    if job.status != "paused":
        raise ValueError(f"job {job_id} is {job.status}; cannot resume")
    job.status = "queued"
    job.finished_at = None
    job.error = None
    session.commit()
    _queue.put(job.id)


def reset_stale_jobs(session: Session) -> int:
    """进程启动时调用：遗留 queued/running 置 failed；processing 视频回 pending。

    paused 任务保留（重启后仍可手动恢复——handler 幂等，恢复后从头续跑）。
    """
    n = 0
    for job in session.execute(select(Job).where(Job.status.in_(("queued", "running")))).scalars():
        job.status = "failed"
        job.error = "interrupted by process restart"
        job.finished_at = utcnow()
        n += 1
    for video in session.execute(select(Video).where(Video.status == "processing")).scalars():
        video.status = "pending"
        video.status_msg = None
    session.commit()
    return n


def job_detail(job: Job) -> dict:
    return {
        "id": job.id, "type": job.type, "status": job.status,
        "done": job.done, "total": job.total,
        "params": json.loads(job.params) if job.params else {},
        "result": json.loads(job.result) if job.result else None,
        "error": job.error,
        "created_at": job.created_at, "started_at": job.started_at,
        "finished_at": job.finished_at,
    }


def _run_one(job_id: int) -> None:
    session = db_session_mod.SessionLocal()
    try:
        job = session.get(Job, job_id)
        if job is None or job.status != "queued":
            return  # queued 期间被取消/暂停（直接置态）→ 跳过
        job.status = "running"
        job.started_at = utcnow()
        session.commit()
        params = json.loads(job.params) if job.params else {}
        handler = HANDLERS.get(job.type)
        try:
            if handler is None:
                raise RuntimeError(f"no handler registered for job type '{job.type}'")
            handler(session, job, params)
            # handler 正常返回但仍可能在最后一个检查点后收到请求：兜底落态
            kind = _take_request(job_id)
            if kind == "cancelled":
                job.status = "cancelled"
                job.finished_at = utcnow()
                job.error = "cancelled by user"
            elif kind == "paused":
                job.status = "paused"
            else:
                job.status = "done"
                job.finished_at = utcnow()
            session.commit()
        except JobInterrupted as exc:
            session.rollback()
            job = session.get(Job, job_id)  # rollback 后重新取行再写终态
            job.status = exc.kind  # paused | cancelled
            if exc.kind == "cancelled":
                job.finished_at = utcnow()
                job.error = "cancelled by user"
            session.commit()
        except Exception as exc:  # noqa: BLE001 —— 失败落库，worker 线程不退出
            log.exception("job %s (%s) failed", job_id, job.type)
            session.rollback()
            job = session.get(Job, job_id)  # rollback 后重新取行再写失败状态
            job.status = "failed"
            job.error = f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=5)}"
            job.finished_at = utcnow()
            session.commit()
        finally:
            with _lock:
                _pause_requested.discard(job_id)
                _cancel_requested.discard(job_id)
    finally:
        session.close()


def _run_loop() -> None:
    while True:
        job_id = _queue.get()
        try:
            _run_one(job_id)
        except Exception:  # noqa: BLE001 —— 兜底，worker 永不退出
            log.exception("worker loop error on job %s", job_id)


def start_worker() -> None:
    global _worker_started
    with _lock:
        if _worker_started:
            return
        t = threading.Thread(target=_run_loop, name="sophos-worker", daemon=True)
        t.start()
        _worker_started = True
