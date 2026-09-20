"""运行时可调设置（R5，ADR-22；R6.3 扩展自动化与自动训练）。

需求："分批大小 / 每人面容上限可在设置中调整"。存 kv_setting.key=
'runtime_settings'（JSON dict，复用 M2 的 kv 机制），键白名单 + 类型/范围
校验；默认值取自 config.settings（.env 仍是最初来源），WebUI 设置卡片
通过 /api/settings 覆盖，改动立即对**后续任务**生效（无需重启进程）。

R6.3：新增自动化与训练键——
- auto_train_every：自动训练触发的新增评分/对比条数（0 = 关闭），
  faces.py 的 _maybe_autotrain 改读此运行时值（原为 .env 只读）；
- auto_scan_enabled / auto_scan_time：**每天固定时刻**（HH:mm 本地时间）
  自动扫描（R6.3b 按用户反馈由间隔制改为每日定时，services/automation）；
- auto_process_enabled：自动处理面容（扫描出 pending 视频后自动起处理链）。
"""
from __future__ import annotations

import re

from sqlalchemy.orm import Session

from app.config import settings
from app.services.kv import get_kv, set_kv

KEY = "runtime_settings"

_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

# 白名单：名称 -> (类型, 下限, 上限, settings 中默认值属性名)
# int 键范围钳制；bool 键类型校验；str 键格式校验（HH:mm）；
# max_faces_per_person 允许 0 = 不限制
SPECS: dict[str, tuple[type, int | None, int | None, str]] = {
    "process_batch_size": (int, 1, 50, "process_batch_size"),
    "max_faces_per_person": (int, 0, 500, "max_faces_per_person"),
    # ---- R6.3：自动化 ----
    "auto_train_every": (int, 0, 100000, "auto_train_every"),
    "auto_scan_enabled": (bool, None, None, "auto_scan_enabled"),
    "auto_scan_time": (str, None, None, "auto_scan_time"),
    "auto_process_enabled": (bool, None, None, "auto_process_enabled"),
}


def get_all(session: Session) -> dict:
    """合并默认值与已存覆盖值（脏数据回退默认，范围越界夹紧）。"""
    stored = get_kv(session, KEY, {})
    if not isinstance(stored, dict):
        stored = {}
    out: dict = {}
    for name, (typ, lo, hi, attr) in SPECS.items():
        default = getattr(settings, attr)
        raw = stored.get(name, default)
        try:
            if typ is bool:
                val = raw if isinstance(raw, bool) else default
            elif typ is str:
                val = raw if (isinstance(raw, str) and _HHMM_RE.match(raw)) else default
            else:
                val = min(max(int(raw), lo), hi)
        except (TypeError, ValueError):
            val = default
        out[name] = val
    return out


def get_value(session: Session, name: str):
    if name not in SPECS:
        raise KeyError(f"unknown setting: {name}")
    return get_all(session)[name]


def set_values(session: Session, patch: dict) -> dict:
    """校验并保存覆盖值；返回合并后的全部设置。非法键/类型/范围抛 ValueError。"""
    if not isinstance(patch, dict):
        raise ValueError("settings payload must be an object")
    stored = get_kv(session, KEY, {})
    if not isinstance(stored, dict):
        stored = {}
    for name, value in patch.items():
        if name not in SPECS:
            raise ValueError(f"unknown setting: {name}")
        typ, lo, hi, _ = SPECS[name]
        if typ is bool:
            if not isinstance(value, bool):
                raise ValueError(f"{name} must be a boolean")
        elif typ is str:
            if not isinstance(value, str) or not _HHMM_RE.match(value):
                raise ValueError(f"{name} must be HH:mm (00:00-23:59)")
        else:
            # bool 是 int 子类，显式拒绝（true 会被 JSON 侧当 1）
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"{name} must be an integer")
            if not lo <= value <= hi:
                raise ValueError(f"{name} must be in [{lo}, {hi}]")
        stored[name] = value
    set_kv(session, KEY, stored)
    return get_all(session)
