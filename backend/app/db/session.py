"""engine/session 管理（M2 实现；R5 压测调优）。

- init_engine(settings)：（重）建全局 engine + SessionLocal 并 create_all；
  应用 lifespan 与测试 fixture 均调用（幂等）。
- SQLite：check_same_thread=False（API 线程与 worker 线程共用）+ timeout=30（busy 等待）。
  WAL 三态（R12，ADR-033）：auto=本地固定磁盘启用 / true / false——网络盘（SMB/NFS）
  保持回滚日志（WAL 共享内存不可靠，M2 实测）；
  不启用 PRAGMA foreign_keys（级联删除由应用层负责，见 ARCHITECTURE.md §3.1）。
- R5 压测（STRESS_LOG S6）：SMB 网络盘上 SQLite 默认页缓存（2MB）远不够——
  24 万 identity（含 2KB embedding blob）规模下随机 PK 点查全部落到网络读
  （608 点查 ≈ 3.5s）。调 cache_size=128MB + temp_store=MEMORY，
  大库下各类随机访问显著受益。
"""
from __future__ import annotations

from typing import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.db.migrations import run_migrations
from app.db.models import Base

_engine: Engine | None = None
SessionLocal: sessionmaker[Session] | None = None


@event.listens_for(Engine, "connect")
def _sqlite_pragma(dbapi_connection, _record):  # pragma: no cover —— 配置性代码
    import sqlite3

    if isinstance(dbapi_connection, sqlite3.Connection):
        cur = dbapi_connection.cursor()
        cur.execute("PRAGMA cache_size=-131072")   # 128MB（负数 = KB）
        cur.execute("PRAGMA temp_store=MEMORY")    # 排序/临时表走内存
        from app.config import settings
        if _wal_enabled(settings):
            # R12（ADR-033）：WAL 三态 auto/true/false。auto = DB 在本地固定磁盘
            # 才开（SMB/NFS 上 WAL 不可靠，M2 实测；Windows 按盘符类型判定）。
            # WAL 下读完全不被写提交阻塞——处理/训练/应用模型期间浏览不再卡顿。
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
        cur.close()


def _wal_enabled(settings: Settings) -> bool:
    """解析 db_wal 三态（R12）：true/false 显式生效；auto 按存储介质判定。"""
    mode = str(settings.db_wal).strip().lower()
    if mode in ("1", "true", "yes", "on"):
        return True
    if mode in ("0", "false", "no", "off"):
        return False
    return _db_on_fixed_disk(settings.final_db_path())


def _db_on_fixed_disk(db_path) -> bool:
    """DB 所在盘是否本地固定磁盘。Windows 用 GetDriveTypeW（DRIVE_FIXED=3，
    DRIVE_REMOTE=4 网络盘 / DRIVE_REMOVABLE=2）；POSIX 无可靠 NFS 判定，
    默认按本地盘处理（NFS 部署形态请显式 SOPHOS_DB_WAL=false）。"""
    import os

    if os.name != "nt":
        return True
    try:
        import ctypes

        drive = os.path.splitdrive(str(db_path))[0]
        if not drive:
            return True
        return ctypes.windll.kernel32.GetDriveTypeW(
            ctypes.c_wchar_p(drive + "\\")) == 3  # DRIVE_FIXED
    except Exception:  # noqa: BLE001 —— 判定失败保守关闭（维持旧行为）
        return False


def init_engine(settings: Settings) -> Engine:
    global _engine, SessionLocal
    db_path = settings.final_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if _engine is not None:
        _engine.dispose()
    _engine = create_engine(
        f"sqlite:///{db_path.as_posix()}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    Base.metadata.create_all(_engine)
    run_migrations(_engine)  # R1：对已存在的表补新列（幂等）
    SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False, autoflush=False)
    return _engine


def get_engine() -> Engine | None:
    return _engine


def get_db() -> Iterator[Session]:
    if SessionLocal is None:
        raise RuntimeError("database not initialised (call init_engine first)")
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
