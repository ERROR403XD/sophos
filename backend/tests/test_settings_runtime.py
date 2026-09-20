"""运行时设置 API + process 分批启动测试（R5，ADR-022/023）；R6.3 自动化设置。"""
import pathlib

import pytest
from sqlalchemy import select, text

from app.db.models import Video


def test_settings_defaults_and_roundtrip(client):
    body = client.get("/api/settings").json()["settings"]
    assert body["process_batch_size"] == 4
    assert body["max_faces_per_person"] == 5  # R5：同一视频同一人最多 5 张

    r = client.put("/api/settings", json={
        "process_batch_size": 2, "max_faces_per_person": 0})
    assert r.status_code == 200
    body = r.json()["settings"]
    assert body["process_batch_size"] == 2
    assert body["max_faces_per_person"] == 0  # 0 = 不限制
    assert client.get("/api/settings").json()["settings"]["max_faces_per_person"] == 0


def test_settings_validation(client):
    for payload in (
        {"process_batch_size": 0},            # 低于下限
        {"process_batch_size": "8"},          # 字符串
        {"max_faces_per_person": True},       # bool 是 int 子类，拒绝
        {"max_faces_per_person": 501},        # 超上限
        {"unknown_key": 1},                   # 白名单外
    ):
        r = client.put("/api/settings", json=payload)
        assert r.status_code == 400, payload
        assert r.json()["code"] == "INVALID_SETTING"


def _add_video(db, tmp_path, name, status="pending"):
    v = Video(path=str(tmp_path / name), filename=name,
              dir_path=str(tmp_path), status=status)
    db.add(v)
    db.commit()
    return v.id


def test_process_start_batches_by_runtime_setting(client, db, tmp_path):
    """process/start 按运行时批大小提交第一批并下发 total_batches。"""
    for i in range(5):
        _add_video(db, tmp_path, f"v{i}.mp4")
    client.put("/api/settings", json={"process_batch_size": 2})

    r = client.post("/api/process/start")
    assert r.status_code == 202
    job = r.json()["job"]
    assert job["params"]["video_ids"] == [1, 2]
    assert job["params"]["chain"] is True
    assert job["params"]["total_batches"] == 3


def test_process_start_no_pending(client, db):
    r = client.post("/api/process/start")
    assert r.status_code == 409
    assert r.json()["code"] == "NO_PENDING_VIDEOS"


def test_max_faces_per_person_effective_in_pipeline(client, db):
    """cap 面容上限经 runtime_settings 生效（pipeline 读取口径）。"""
    from app.services import pipeline, runtime_settings

    runtime_settings.set_values(db, {"max_faces_per_person": 3})
    assert pipeline.runtime_settings.get_value(db, "max_faces_per_person") == 3


# ---------------- R6.3：自动训练阈值 + 自动扫描/处理设置 ----------------

def test_settings_new_automation_keys(client, db):
    from app.services import runtime_settings

    body = client.get("/api/settings").json()["settings"]
    assert body["auto_train_every"] == 30
    assert body["auto_scan_enabled"] is False
    assert body["auto_scan_time"] == "03:00"
    assert body["auto_process_enabled"] is False

    r = client.put("/api/settings", json={
        "auto_train_every": 10,
        "auto_scan_enabled": True,
        "auto_scan_time": "07:30",
        "auto_process_enabled": True,
    })
    assert r.status_code == 200
    body = r.json()["settings"]
    assert body["auto_train_every"] == 10
    assert body["auto_scan_enabled"] is True
    assert body["auto_scan_time"] == "07:30"
    assert body["auto_process_enabled"] is True
    # 运行时读取口径（faces._maybe_autotrain 的来源）
    assert runtime_settings.get_value(db, "auto_train_every") == 10

    # 类型/校验：bool 键拒绝整数、int 键拒绝 bool、时刻必须 HH:mm
    for payload in (
        {"auto_scan_enabled": 1},
        {"auto_process_enabled": "yes"},
        {"auto_train_every": True},
        {"auto_scan_time": "25:00"},
        {"auto_scan_time": "7:30"},
        {"auto_scan_time": "07:5"},
        {"auto_scan_time": 730},
    ):
        r = client.put("/api/settings", json=payload)
        assert r.status_code == 400, payload


def test_autotrain_uses_runtime_setting(client, db, monkeypatch):
    """自动训练阈值经运行时设置生效：0 = 关闭。"""
    from app.api.faces import _maybe_autotrain
    from app.db.models import FaceIdentity, PairComparison, UserRating
    from app.services import runtime_settings
    from app.services.jobs import has_active_job

    vid = _add_video(db, pathlib.Path("."), "seed.mp4", status="done")
    ident = FaceIdentity(video_id=vid, n_samples=1)
    db.add(ident)
    db.commit()
    # 造 20 条评分 + 10 条对比 = 30（恰为默认阈值 30 的整数倍）
    for i in range(20):
        db.add(UserRating(identity_id=ident.id, rating_type="score", rating_value=str(i % 10 + 1)))
    for i in range(10):
        db.add(PairComparison(winner_identity_id=ident.id, loser_identity_id=ident.id))
    db.commit()

    runtime_settings.set_values(db, {"auto_train_every": 0})
    _maybe_autotrain(db)
    db.commit()
    assert not has_active_job(db, "train")  # 0 = 关闭

    runtime_settings.set_values(db, {"auto_train_every": 15})  # 30 % 15 == 0 → 触发
    _maybe_autotrain(db)
    db.commit()
    assert has_active_job(db, "train")


def test_automation_tick_daily_scan_and_process(client, db, tmp_path, monkeypatch):
    """automation.tick：每日定时扫描（当天未触发过才发）+ pending 自动处理首批。"""
    import time as _time

    from app.services import automation, runtime_settings
    from app.services.jobs import has_active_job

    runtime_settings.set_values(db, {
        "auto_scan_enabled": True,
        "auto_scan_time": "00:00",  # 任何时刻都已"过点" → 当天首次 tick 必触发
        "auto_process_enabled": True,
    })
    _add_video(db, tmp_path, "p1.mp4")  # pending → 自动处理目标

    actions = automation.tick()
    db.commit()
    assert actions.get("scan") is True      # 当天尚未触发 → 扫描
    assert actions.get("process") is True   # pending → 提交处理首批
    assert has_active_job(db, "scan") and has_active_job(db, "process")

    # 今天已触发 → 不再重复扫描；process 已有任务 → 不重复提交
    actions2 = automation.tick()
    assert not actions2.get("scan") and not actions2.get("process")

    # 等 worker 排干（tick 与单 worker 排干任务有竞态，scan 仍在队列会被跳过），
    # 再把"上次触发日期"清成过去（模拟跨天）→ 再次扫描
    deadline = _time.time() + 15.0
    while _time.time() < deadline and (has_active_job(db, "scan") or has_active_job(db, "process")):
        _time.sleep(0.1)
    db.execute(text("update kv_setting set value = '2000-01-01' where key = 'last_auto_scan_date'"))
    db.commit()
    actions3 = automation.tick()
    assert actions3.get("scan") is True


def test_is_scan_due_pure():
    from datetime import datetime

    from app.services.automation import _is_scan_due

    now = datetime(2026, 9, 19, 7, 30)
    assert _is_scan_due(now, "07:30", "")                # 到点未触发 → 触发
    assert not _is_scan_due(now, "07:30", "2026-09-19")  # 今天已触发
    assert not _is_scan_due(now, "23:00", "")            # 未到配置时刻
    assert _is_scan_due(now, "03:00", "2026-09-18")      # 错过后当天补跑一次
    # 跨天：昨天触发过，今天 0 点刚过且配置 00:00 → 触发
    assert _is_scan_due(datetime(2026, 9, 19, 0, 0), "00:00", "2026-09-18")
