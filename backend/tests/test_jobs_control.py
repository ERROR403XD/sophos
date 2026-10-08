"""任务暂停/取消/恢复测试（R5，ADR-022）。

- 用注册的慢 handler 驱动协作检查点：running 中 pause → paused、resume →
  重新入队跑完、cancel → cancelled；
- queued 状态直接置 cancelled（worker 取出时跳过）；已完成态操作 → 409；
- process handler 在视频边界/抽帧循环响应中断，把 processing 视频回退
  pending；链式分批在有暂停/取消请求时停止链接，无请求时提交下一批。
"""
import json
import time

import pytest
from sqlalchemy import select

from app.db.models import Job as JobRow
from app.db.models import Video
from app.services import jobs as jobsvc
from app.services import pipeline


class Slow:
    """慢 handler：50 个检查点 × 40ms；记数供断言。"""

    def __init__(self):
        self.steps = 0

    def __call__(self, session, job, params):
        for _ in range(50):
            jobsvc.check_point(session, job)
            time.sleep(0.04)
            self.steps += 1


@pytest.fixture()
def slow_handler():
    handler = Slow()
    jobsvc.register_handler("test_slow", handler)
    return handler


def _wait_status(client, job_id, statuses, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        body = client.get(f"/api/jobs/{job_id}").json()
        if body["status"] in statuses:
            return body
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} not in {statuses}: {body}")


def test_job_list_puts_active_work_before_finished(client, db):
    rows = [
        JobRow(type="scan", status="done", done=1, total=1),
        JobRow(type="process", status="running", done=0, total=2),
        JobRow(type="train", status="paused", done=1, total=2),
        JobRow(type="analyze", status="queued", done=0, total=1),
        JobRow(type="scan", status="cancelled", done=0, total=1),
    ]
    db.add_all(rows)
    db.commit()

    statuses = [item["status"] for item in client.get("/api/jobs").json()["items"]]
    assert statuses == ["running", "queued", "paused", "cancelled", "done"]


def test_pause_resume_cancel_lifecycle(client, db, slow_handler):
    job_id = jobsvc.submit_job(db, "test_slow").id
    _wait_status(client, job_id, ("running",))

    r = client.post(f"/api/jobs/{job_id}/pause")
    assert r.status_code == 200 and r.json()["action"] in ("paused", "requested")
    _wait_status(client, job_id, ("paused",))

    r = client.post(f"/api/jobs/{job_id}/resume")
    assert r.status_code == 200
    _wait_status(client, job_id, ("running",))

    r = client.post(f"/api/jobs/{job_id}/cancel")
    assert r.status_code == 200
    body = _wait_status(client, job_id, ("cancelled",))
    assert body["error"] == "cancelled by user"
    assert 0 < slow_handler.steps < 50  # 协作中断：未跑完也未失败


def test_cancel_queued_job_skips_execution(client, db, slow_handler):
    first = jobsvc.submit_job(db, "test_slow").id
    _wait_status(client, first, ("running",))          # 占住唯一 worker
    second = jobsvc.submit_job(db, "test_slow").id     # 排队中

    # queued 直接置 cancelled；worker 取出时见非 queued 即跳过
    r = client.post(f"/api/jobs/{second}/cancel")
    assert r.status_code == 200 and r.json()["action"] == "cancelled"
    # 已取消态再操作 → 409
    assert client.post(f"/api/jobs/{second}/pause").status_code == 409
    assert client.post(f"/api/jobs/{second}/resume").status_code == 409

    client.post(f"/api/jobs/{first}/pause")
    _wait_status(client, first, ("paused",))
    client.post(f"/api/jobs/{first}/cancel")
    _wait_status(client, first, ("cancelled",))

    _wait_status(client, jobsvc.submit_job(db, "test_slow").id, ("done",))


def _add_pending_video(db, tmp_path, name):
    v = Video(path=str(tmp_path / name), filename=name,
              dir_path=str(tmp_path), status="pending")
    db.add(v)
    db.commit()
    return v.id


def _running_job(db, params: dict) -> JobRow:
    """直接构造 running 态 job 行（不经 submit_job/队列——避免常驻 worker 抢跑）。"""
    job = JobRow(type="process", status="running",
                 params=json.dumps(params, ensure_ascii=False))
    db.add(job)
    db.commit()
    return job


def test_process_interrupt_resets_video_and_stops_chain(db, tmp_path, monkeypatch):
    """直接驱动 handler（不经 worker）：取消请求发生在 _process_video 内部 →

    - 视频从 processing 回退 pending；
    - 抛出 JobInterrupted，链式分批不再提交下一批。
    """
    from app.services.jobs import JobInterrupted

    ids = [_add_pending_video(db, tmp_path, f"{i}.mp4") for i in range(4)]
    params = {"video_ids": ids[:2], "chain": True}
    job = _running_job(db, params)

    def fake_process(session, engine, video, job=None, set_detail=None, **_kwargs):
        video.status = "processing"
        session.commit()
        jobsvc.request_cancel(session, job.id)  # 模拟处理中途收到取消请求
        jobsvc.check_point(session, job)        # → 抛出 JobInterrupted

    monkeypatch.setattr(pipeline, "_process_video", fake_process)

    with pytest.raises(JobInterrupted):
        pipeline.process_handler(db, job, params)

    remaining = db.execute(
        select(Video.status).where(Video.id.in_(ids))).scalars().all()
    assert all(s == "pending" for s in remaining)
    # 链未继续：库中只有这一个 process job
    n_process = len(db.execute(
        select(JobRow).where(JobRow.type == "process")).scalars().all())
    assert n_process == 1


def test_process_chain_submits_next_batch(db, tmp_path, monkeypatch):
    """批完成且无暂停/取消 → 链式提交下一批 pending（分批大小取运行时设置）。"""
    ids = [_add_pending_video(db, tmp_path, f"c{i}.mp4") for i in range(3)]

    class _Engine:
        clip_ready = False

    def fake_process(session, engine, video, job=None, set_detail=None):
        video.status = "done"
        session.commit()
        return {"identities": 0}

    monkeypatch.setattr(pipeline, "get_face_engine", lambda: _Engine())
    monkeypatch.setattr(pipeline, "_process_video", fake_process)

    from app.services.runtime_settings import set_values
    set_values(db, {"process_batch_size": 2})

    params = {"video_ids": ids[:2], "chain": True}
    job = _running_job(db, params)
    pipeline.process_handler(db, job, params)

    nxt = db.execute(
        select(JobRow).where(JobRow.type == "process", JobRow.id != job.id)
        .order_by(JobRow.id.desc())).scalars().first()
    assert nxt is not None, "下一批应被链式提交"
    p = json.loads(nxt.params)
    assert p["video_ids"] == [ids[2]]
    assert p["chain"] is True
    assert json.loads(job.result)["next_batch"] == nxt.id


# ---------------- R13：双资源池（batch/interactive 并行） ----------------

def test_pool_mapping_and_job_detail_pool(db):
    """R13：任务类型 → 池映射固定；job_detail 下发 pool 字段。"""
    assert jobsvc.POOLS == {"scan": "batch", "process": "batch",
                            "train": "interactive", "analyze": "interactive",
                            "activate": "interactive",
                            "deactivate": "interactive"}
    job = jobsvc.submit_job(db, "analyze")
    assert jobsvc.job_detail(job)["pool"] == "interactive"


def test_two_pools_run_concurrently(client, db):
    """R13（ADR-034）：batch 长任务占住 batch worker 时，interactive 任务
    仍被立即执行（train/analyze 不再排在整批处理后面）。"""
    import threading

    batch_running = threading.Event()
    release_batch = threading.Event()

    def batch_handler(session, job, params):
        batch_running.set()
        release_batch.wait(10)

    def interactive_handler(session, job, params):
        pass

    jobsvc.POOLS["pooltest_b"] = "batch"
    jobsvc.POOLS["pooltest_i"] = "interactive"
    jobsvc.register_handler("pooltest_b", batch_handler)
    jobsvc.register_handler("pooltest_i", interactive_handler)
    try:
        jb = jobsvc.submit_job(db, "pooltest_b")
        assert batch_running.wait(5), "batch job must start"

        ji = jobsvc.submit_job(db, "pooltest_i")
        body = _wait_status(client, ji.id, ("done",), timeout=5)
        assert body["pool"] == "interactive"

        release_batch.set()
        body = _wait_status(client, jb.id, ("done",), timeout=8)
        assert body["pool"] == "batch"
    finally:
        release_batch.set()
        jobsvc.HANDLERS.pop("pooltest_b", None)
        jobsvc.HANDLERS.pop("pooltest_i", None)
        jobsvc.POOLS.pop("pooltest_b", None)
        jobsvc.POOLS.pop("pooltest_i", None)
