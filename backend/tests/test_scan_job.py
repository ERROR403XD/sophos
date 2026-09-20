"""扫描任务端到端（API → worker 线程 → DB → 视频表）（M2）。"""
import time


def _wait_job(client, job_id, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        body = client.get(f"/api/jobs/{job_id}").json()
        if body["status"] in ("done", "failed"):
            return body
        time.sleep(0.05)
    raise AssertionError("job did not finish in time")


def test_scan_job_end_to_end(client, tmp_path):
    d = tmp_path / "vd"
    d.mkdir()
    (d / "a.mp4").write_bytes(b"x" * 50)
    (d / "b.txt").write_bytes(b"ignore")

    assert client.post("/api/workdirs", json={"path": str(d)}).status_code == 201

    resp = client.post("/api/scan/start")
    assert resp.status_code == 202
    job = resp.json()["job"]
    assert job["type"] == "scan"

    body = _wait_job(client, job["id"])
    assert body["status"] == "done"
    assert body["result"]["added"] == 1

    items = client.get("/api/videos").json()["items"]
    assert len(items) == 1
    assert items[0]["filename"] == "a.mp4"
    assert items[0]["status"] == "pending"
    assert items[0]["final_score"] is None  # M4 前分数字段为空

    # 二次扫描幂等
    body2 = _wait_job(client, client.post("/api/scan/start").json()["job"]["id"])
    assert body2["status"] == "done"
    assert body2["result"]["unchanged"] == 1


def test_scan_start_rejects_when_active(client, tmp_path):
    """已有 scan 任务 queued/running 时返回 409。"""
    d = tmp_path / "vd2"
    d.mkdir()
    client.post("/api/workdirs", json={"path": str(d)})

    # 人为插入一个 queued 任务（不进 worker 队列即可保持 queued）
    from app.db import session as db_session_mod
    from app.db.models import Job
    with db_session_mod.SessionLocal() as s:
        s.add(Job(type="scan", status="queued"))
        s.commit()

    assert client.post("/api/scan/start").status_code == 409
