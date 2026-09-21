"""压测工装 S3：真实流水线压测驱动（对 8040 端口的压测实例执行）。

前置：uvicorn 以 SOPHOS_DATA_DIR=<stress-data>/data_pipe 启动于 8040
（见 STRESS_LOG S3 节），切片已由 gen_clips.py 备好。

流程：
  1. 添加工作目录（真实切片 120 + 合成切片 500）→ scan
  2. process（分批链 batch=4）：运行中**暂停** → 校验链停/进度冻结 → **恢复** → 等全部完成
  3. 取消测试：6 个视频置回 pending、batch=1 重启 process → **取消** → 校验链停、剩余仍 pending
  4. 输出吞吐统计（总耗时/均速/各 job ok·failed）

用法：python scripts/stress/run_pipe_stress.py [--base http://127.0.0.1:8040]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import time
from pathlib import Path

import requests

CLIPS_REAL = r"<stress-data>\clips"
CLIPS_SYN = r"<stress-data>\clips_synthetic"
PIPE_DB = Path(r"<stress-data>\data_pipe\sophos.db")


class Driver:
    def __init__(self, base: str):
        self.base = base

    def __call__(self, method: str, path: str, **kw) -> dict:
        r = requests.request(method, self.base + path, timeout=60, **kw)
        if r.status_code >= 400:
            raise RuntimeError(f"{method} {path} -> {r.status_code}: {r.text[:300]}")
        return r.json()

    def wait_job(self, job_id: int, statuses=("done", "failed"), timeout: float = 3600) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            body = self("GET", f"/api/jobs/{job_id}")
            if body["status"] in statuses:
                return body
            time.sleep(1.0)
        raise TimeoutError(f"job {job_id} not in {statuses}")

    def active_process(self) -> dict | None:
        """当前"在干活"的 process job（queued/running；paused 不算——它已让出队列）。"""
        jobs = self("GET", "/api/jobs", params={"active": True, "limit": 50})["items"]
        for j in jobs:
            if j["type"] == "process" and j["status"] in ("queued", "running"):
                return j
        return None

    def cleanup_stale(self) -> None:
        """清掉上次运行遗留的 paused process job（取消之，保证流程干净）。"""
        jobs = self("GET", "/api/jobs", params={"active": True, "limit": 50})["items"]
        for j in jobs:
            if j["type"] == "process" and j["status"] == "paused":
                self("POST", f"/api/jobs/{j['id']}/cancel")

    def video_status(self) -> dict[str, int]:
        rows = self("GET", "/api/videos", params={"page_size": 1})["total"]
        counts: dict[str, int] = {}
        page = 1
        while True:
            body = self("GET", "/api/videos", params={"page": page, "page_size": 200})
            for it in body["items"]:
                counts[it["status"]] = counts.get(it["status"], 0) + 1
            if page * 200 >= body["total"]:
                break
            page += 1
        _ = rows
        return counts

    def wait_no_active_process(self, timeout: float = 3600) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.active_process() is None:
                return
            time.sleep(1.0)
        raise TimeoutError("active process job still running")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8040")
    args = ap.parse_args()
    d = Driver(args.base)
    log: dict = {"t0": time.strftime("%FT%T")}

    print("health:", d("GET", "/api/health"))
    d.cleanup_stale()

    # 1) 工作目录 + 扫描
    for p in (CLIPS_REAL, CLIPS_SYN):
        d("POST", "/api/workdirs", json={"path": p})
    t0 = time.time()
    scan = d("POST", "/api/scan/start")["job"]["id"]
    body = d.wait_job(scan)
    log["scan"] = {"sec": round(time.time() - t0, 1), "result": body["result"]}
    print("scan:", log["scan"])

    # 2) process 分批链 + 暂停/恢复
    d("PUT", "/api/settings", json={"process_batch_size": 4})
    t0 = time.time()
    job = d("POST", "/api/process/start")["job"]
    log["process_start_job"] = job["id"]
    paused_once = False
    while True:
        time.sleep(2.0)
        counts = d.video_status()
        done_now = counts.get("done", 0)
        if not paused_once and done_now >= 20:
            act = d.active_process()
            if act is not None:
                d("POST", f"/api/jobs/{act['id']}/pause")
                paused = d.wait_job(act["id"], statuses=("paused",), timeout=300)
                counts_at_pause = d.video_status()
                time.sleep(3)
                counts_frozen = d.video_status()
                assert counts_at_pause == counts_frozen, "暂停后进度仍在推进！"
                assert d.active_process() is None, "暂停后仍有活动 process job"
                log["pause"] = {"at_done": done_now,
                                "status_counts": counts_at_pause}
                print("paused at:", log["pause"])
                d("POST", f"/api/jobs/{act['id']}/resume")
                paused_once = True
        if d.active_process() is None and paused_once:
            break
        if not paused_once and time.time() - t0 > 3600:
            raise TimeoutError("process chain too slow")
    counts_final = d.video_status()
    log["process"] = {"sec": round(time.time() - t0, 1), "final_counts": counts_final}
    print("process done:", log["process"])
    assert counts_final.get("pending", 0) == 0 and counts_final.get("failed", 0) == 0

    # 3) 取消测试：6 个视频置回 pending，batch=1，取消后链应停止
    con = sqlite3.connect(PIPE_DB)
    ids = [r[0] for r in con.execute(
        "SELECT id FROM video ORDER BY id LIMIT 6").fetchall()]
    con.execute(f"UPDATE video SET status='pending' WHERE id IN ({','.join('?'*6)})", ids)
    con.commit()
    con.close()
    d("PUT", "/api/settings", json={"process_batch_size": 1})
    job = d("POST", "/api/process/start")["job"]
    deadline = time.time() + 120
    while time.time() < deadline:  # 等它真正跑起来
        act = d.active_process()
        if act is not None:
            break
        time.sleep(0.5)
    d("POST", f"/api/jobs/{job['id']}/cancel")
    d.wait_no_active_process(timeout=300)
    counts = d.video_status()
    log["cancel"] = {"status_counts": counts}
    print("cancel test:", log["cancel"])
    assert counts.get("pending", 0) >= 1, "取消后不应全部完成（链应停止）"

    # 4) job 链统计
    jobs = [j for j in d("GET", "/api/jobs", params={"limit": 200})["items"]
            if j["type"] == "process"]
    log["process_jobs"] = [{"id": j["id"], "status": j["status"],
                            "done": j["done"], "total": j["total"]} for j in jobs]
    Path(r"<stress-data>\pipe_stress.json").write_text(
        json.dumps(log, ensure_ascii=False, indent=1), encoding="utf-8")
    print("report -> <stress-data>\\pipe_stress.json")


if __name__ == "__main__":
    main()
