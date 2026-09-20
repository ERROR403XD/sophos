"""kv_setting 读写助手（M2 实现）。

工作目录列表存 kv_setting.key='work_dirs'（JSON 数组）；
SOPHOS_WORK_DIRS 仅在 key 尚不存在时播种一次，之后以 API 配置为准。
"""
from __future__ import annotations

import json

from sqlalchemy.orm import Session

from app.config import Settings
from app.db.models import KVSetting

KEY_WORK_DIRS = "work_dirs"


def get_kv(session: Session, key: str, default=None):
    row = session.get(KVSetting, key)
    if row is None:
        return default
    try:
        return json.loads(row.value)
    except json.JSONDecodeError:
        return default


def set_kv(session: Session, key: str, value) -> None:
    row = session.get(KVSetting, key)
    payload = json.dumps(value, ensure_ascii=False)
    if row is None:
        session.add(KVSetting(key=key, value=payload))
    else:
        row.value = payload
    session.commit()


def get_work_dirs(session: Session) -> list[str]:
    v = get_kv(session, KEY_WORK_DIRS, [])
    return [str(p) for p in v] if isinstance(v, list) else []


def set_work_dirs(session: Session, dirs: list[str]) -> list[str]:
    clean = sorted({str(d) for d in dirs})
    set_kv(session, KEY_WORK_DIRS, clean)
    return clean


def seed_work_dirs_from_env(session: Session, settings: Settings) -> bool:
    """key 不存在且 SOPHOS_WORK_DIRS 有值时播种；返回是否播种。"""
    if session.get(KVSetting, KEY_WORK_DIRS) is None:
        dirs = settings.parse_work_dirs()
        if dirs:
            set_work_dirs(session, dirs)
            return True
    return False
