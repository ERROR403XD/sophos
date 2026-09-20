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
    assert added == sum(len(cols) for cols in NEW_COLUMNS.values())
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
    with old_schema_engine.begin() as conn:
        # face_identity 空表：能查询新列即证明补列成功
        assert conn.execute(text("SELECT occluded, occluded_source FROM face_identity")).all() == []
        v = conn.execute(text("SELECT path, vcodec FROM video")).first()
        assert v == ("x", None)  # 存量行保留，新列为空（按旧行为兜底）
