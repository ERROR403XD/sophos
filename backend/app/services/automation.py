"""自动化后台线程（R6.3；R6.3b 按用户反馈改为每日固定时刻）。

设计：
- 单守护线程，每 AUTOMATION_TICK_SEC 秒做一次 tick（读 runtime_settings，
  与 /api/settings 的改动即时同步，无需重启）；
- **auto_scan_enabled 开启时，每天在 auto_scan_time（HH:mm，本地时间）触发
  一次扫描**（params {"auto": true}，任务页详情可见来源）：tick 时刻 ≥ 配置
  时刻且今天尚未触发过 → 触发（服务在配置时刻之后才启动/恢复也会当天补跑
  一次；错过不跨天追补）；
- auto_process_enabled 开启时，无 process 任务且存在 pending 视频 → 提交
  处理链首批（与 /api/process/start 同口径，后续批次由分批链续）；
  与扫描的**联动**由此自然成立：扫描产出 pending → 下一 tick（≤20s）自动起处理；
  另在 scan_handler 完成自动扫描且发现新增/变更时**立即**提交处理批（免等 tick）。
- 上次触发日期存 kv_setting（一天最多一次，重启不重跑）；worker 单线程串行
  执行，提交前 has_active_job 防重复。
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime

from sqlalchemy import func, select

from app.db import session as db_session_mod
from app.db.models import Video
from app.services import runtime_settings
from app.services.kv import get_kv, set_kv
from app.services.jobs import has_active_job, submit_job

log = logging.getLogger("sophos.automation")

AUTOMATION_TICK_SEC = 20.0
LAST_SCAN_KEY = "last_auto_scan_date"

_thread: threading.Thread | None = None
_stop = threading.Event()


def _is_scan_due(now: datetime, hhmm: str, last_date: str) -> bool:
    """纯函数便于单测：当前时刻已到今天的配置触发点，且今天尚未触发过。"""
    return (now.strftime("%H:%M") >= hhmm
            and now.date().isoformat() != last_date)


def _submit_process_batch(db) -> bool:
    """与 /api/process/start 同口径：无活动 process 任务且有 pending 时提交首批。"""
    from app.services.pipeline import _pending_video_ids

    if has_active_job(db, "process"):
        return False
    ids = _pending_video_ids(db)
    if not ids:
        return False
    batch_size = runtime_settings.get_value(db, "process_batch_size")
    submit_job(db, "process", {
        "video_ids": ids[:batch_size], "chain": True, "batch_index": 1,
        "total_batches": max(1, -(-len(ids) // batch_size)), "auto": True,
    })
    log.info("automation: submitted process batch (%d pending)", len(ids))
    return True


def tick() -> dict:
    """一次自动化决策（独立函数便于单测）：返回动作摘要。"""
    actions: dict[str, bool] = {}
    with db_session_mod.SessionLocal() as db:
        cfg = runtime_settings.get_all(db)
        if cfg["auto_scan_enabled"]:
            now = datetime.now()
            last = str(get_kv(db, LAST_SCAN_KEY, "") or "")
            if (_is_scan_due(now, cfg["auto_scan_time"], last)
                    and not has_active_job(db, "scan")):
                set_kv(db, LAST_SCAN_KEY, now.date().isoformat())
                submit_job(db, "scan", {"auto": True})
                actions["scan"] = True
                log.info("automation: submitted auto scan (daily at %s)",
                         cfg["auto_scan_time"])
        if cfg["auto_process_enabled"]:
            pending = db.execute(
                select(func.count()).select_from(Video).where(Video.status == "pending")
            ).scalar_one()
            if pending > 0 and _submit_process_batch(db):
                actions["process"] = True
    return actions


def _run_loop() -> None:
    while not _stop.wait(AUTOMATION_TICK_SEC):
        try:
            tick()
        except Exception:  # noqa: BLE001 —— 自动化线程永不退出
            log.exception("automation tick failed")


def start_automation() -> None:
    global _thread
    if _thread is not None and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_run_loop, name="sophos-automation", daemon=True)
    _thread.start()


def stop_automation() -> None:
    _stop.set()
