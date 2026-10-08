"""R1 轻量迁移测试：旧 schema 库补列 + 幂等（docs/PLAN_v1.0.1.md §6）。"""
import sqlite3

import pytest
from sqlalchemy import create_engine, inspect, text

from app.db.migrations import NEW_COLUMNS, run_migrations
from app.db.models import Base


@pytest.fixture()
def old_schema_engine(tmp_path):
    """模拟 v0.x 库：video/face/face_identity 只有旧列。"""
    eng = create_engine(f"sqlite:///{(tmp_path / 'old.db').as_posix()}")
    with eng.begin() as conn:
        conn.execute(text("""
            CREATE TABLE video (
                id INTEGER PRIMARY KEY, path TEXT NOT NULL UNIQUE,
                filename TEXT NOT NULL, dir_path TEXT NOT NULL,
                status TEXT NOT NULL, identity_count INTEGER)
        """))
        conn.execute(text("""
            CREATE TABLE job (
                id INTEGER PRIMARY KEY, type TEXT NOT NULL, status TEXT NOT NULL,
                done INTEGER, total INTEGER, params TEXT, result TEXT, error TEXT,
                created_at TEXT, started_at TEXT, finished_at TEXT)
        """))
        conn.execute(text("""
            CREATE TABLE face (
                id INTEGER PRIMARY KEY, video_id INTEGER NOT NULL,
                quality_score REAL, female_prob REAL)
        """))
        conn.execute(text("""
            CREATE TABLE face_identity (
                id INTEGER PRIMARY KEY, video_id INTEGER NOT NULL,
                n_samples INTEGER NOT NULL)
        """))
    return eng


def _columns(eng, table):
    insp = inspect(eng)
    return {c["name"] for c in insp.get_columns(table)}


def test_migrations_add_missing_columns(old_schema_engine):
    added = run_migrations(old_schema_engine)
    assert added == sum(len(cols) for cols in NEW_COLUMNS.values()) + 1  # face(video_id, ts)
    for table, cols in NEW_COLUMNS.items():
        assert set(c for c, _ in cols) <= _columns(old_schema_engine, table)


def test_migrations_idempotent(old_schema_engine):
    run_migrations(old_schema_engine)
    assert run_migrations(old_schema_engine) == 0


def test_migrations_skip_fresh_schema(tmp_path):
    """新库（create_all 全新 schema）→ 无需补列。"""
    eng = create_engine(f"sqlite:///{(tmp_path / 'new.db').as_posix()}")
    Base.metadata.create_all(eng)
    assert run_migrations(eng) == 0


def test_migrations_preserve_rows(old_schema_engine):
    with old_schema_engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO video (path, filename, dir_path, status, identity_count) "
            "VALUES ('x', 'x.mp4', 'd', 'done', 3)"))
    run_migrations(old_schema_engine)


def test_migrations_add_query_composite_indexes(tmp_path):
    """存量库补齐评分/对比/轻量抽样查询所需的复合索引。"""
    eng = create_engine(f"sqlite:///{(tmp_path / 'indexes.db').as_posix()}")
    with eng.begin() as conn:
        conn.execute(text("""
            CREATE TABLE user_rating (
                id INTEGER PRIMARY KEY, identity_id INTEGER NOT NULL,
                rating_type TEXT, rating_value TEXT, created_at TEXT)
        """))
        conn.execute(text("""
            CREATE TABLE pair_comparison (
                id INTEGER PRIMARY KEY, winner_identity_id INTEGER NOT NULL,
                loser_identity_id INTEGER NOT NULL, created_at TEXT)
        """))
        conn.execute(text("""
            CREATE TABLE face_identity (
                id INTEGER PRIMARY KEY, video_id INTEGER NOT NULL,
                mean_embedding BLOB,
                female_prob_mean REAL, clip_female_mean REAL,
                occluded INTEGER DEFAULT 0, occluded_source TEXT)
        """))

    assert run_migrations(eng) == 4  # 兼容旧单列索引 + 3 个复合索引
    with eng.begin() as conn:
        names = {row[0] for row in conn.execute(text(
            "SELECT name FROM sqlite_master WHERE type='index'"))}
    assert {"ix_user_rating_identity_id_id",
            "ix_pair_comparison_winner_loser",
            "ix_face_identity_id_video_id"} <= names


# ---------------- R12：WAL 三态（auto/true/false） ----------------

def test_wal_tristate_resolution(monkeypatch):
    """显式 true/false 直接生效；auto 按介质判定（本地盘 → WAL）。"""
    from app.config import settings
    from app.db.session import _wal_enabled

    monkeypatch.setattr(settings, "db_wal", "true")
    assert _wal_enabled(settings) is True
    monkeypatch.setattr(settings, "db_wal", "false")
    assert _wal_enabled(settings) is False
    monkeypatch.setattr(settings, "db_wal", "auto")
    # 测试库在 tmp_path（本地固定盘）→ auto 判定启用
    assert _wal_enabled(settings) is True


def test_wal_journal_mode_active(db):
    """R12：auto 模式下本地盘 DB 实际处于 WAL（读不再被写提交阻塞）。"""
    mode = db.execute(text("PRAGMA journal_mode")).scalar()
    assert str(mode).lower() == "wal"
