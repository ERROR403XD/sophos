"""engine/session 管理（M2 实现；R5 压测调优）。

- init_engine(settings)：（重）建全局 engine + SessionLocal 并 create_all；
  应用 lifespan 与测试 fixture 均调用（幂等）。
- SQLite：check_same_thread=False（API 线程与 worker 线程共用）+ timeout=30（busy 等待）。
  不启用 WAL（项目目录位于 SMB 网络盘，见 db/models.py 注释）；
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
        if settings.db_wal:
            # R5.2：仅在 data_dir 于本地盘时开启（SMB 上 WAL 不可靠，见模块 docstring）
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
        cur.close()


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
