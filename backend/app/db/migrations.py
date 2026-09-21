"""轻量列/索引迁移（R1 实现，docs/DATA_MODEL.md §2 迁移机制）。

背景：项目无 alembic，init_engine 只 create_all —— 对**已存在**的表不会补列。
升级部署（如 v0.x → v1.0.1）时新列靠这里补齐。

约定：
- 只做"缺列则 ADD COLUMN"（全部可空/带默认，SQLite ADD COLUMN 即可），不改列删列；
- R5 起兼管**缺失索引**补建（CREATE INDEX IF NOT EXISTS，幂等）——大规模库
  （数十万 face 行）的检索路径依赖复合索引，见 ADR-024；
- 幂等：已具备的列/索引跳过，可重复调用；
- 存量数据兼容：新列为空时按旧行为处理（如 occluded 空=0、pose_class 空=不参与偏好）。
"""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Engine

import logging

log = logging.getLogger("sophos.migrations")

# 表 -> 新增列定义（与 models.py 保持一致；顺序即补列顺序）
NEW_COLUMNS: dict[str, list[tuple[str, str]]] = {
    "video": [
        ("vcodec", "TEXT"),
        ("acodec", "TEXT"),
        ("container", "TEXT"),
    ],
    "face": [
        ("pose_yaw", "REAL"),
        ("pose_pitch", "REAL"),
        ("pose_class", "TEXT"),
        ("occlusion_score", "REAL"),
        ("clip_female_prob", "REAL"),
        ("clip_profile_prob", "REAL"),
    ],
    "face_identity": [
        ("female_prob_mean", "REAL"),
        ("clip_female_mean", "REAL"),
        ("occluded", "INTEGER DEFAULT 0"),
        ("occluded_source", "TEXT"),
    ],
}

# R5（ADR-024）：旧库补建复合索引（新库由 models.py 的 __table_args__ / index=True
# 在 create_all 时直接创建）。守卫条件：表存在且所需列齐全（旧 schema 可能缺列）。
NEW_INDEXES: list[tuple[str, str, str, list[str]]] = [
    # (索引名, DDL, 表, 所需列) —— 同帧共现 pair 计算（pairs._cooccur_pairs）
    ("ix_face_video_ts",
     "CREATE INDEX IF NOT EXISTS ix_face_video_ts ON face (video_id, timestamp_sec)",
     "face", ["video_id", "timestamp_sec"]),
    # 已评分过滤（faces API unrated NOT EXISTS）
    ("ix_user_rating_identity_id",
     "CREATE INDEX IF NOT EXISTS ix_user_rating_identity_id "
     "ON user_rating (identity_id)",
     "user_rating", ["identity_id"]),
]


def run_migrations(engine: Engine) -> int:
    """补齐缺列/缺索引，返回实际变更数（0 = 已是最新）。"""
    changed = 0
    with engine.begin() as conn:
        for table, columns in NEW_COLUMNS.items():
            existing = {row[1] for row in conn.execute(text(f"PRAGMA table_info({table})"))}
            if not existing:  # 表本身不存在（create_all 会建全新 schema）
                continue
            for col, ddl in columns:
                if col in existing:
                    continue
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}"))
                changed += 1
                log.info("migration: %s.%s added", table, col)
        tables = {row[0] for row in conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='table'"))}
        for name, ddl, table, required_cols in NEW_INDEXES:
            if table not in tables:
                continue
            cols = {row[1] for row in conn.execute(text(f"PRAGMA table_info({table})"))}
            if not set(required_cols) <= cols:
                continue  # 旧 schema 缺所需列：留给列迁移完成后的下一轮
            have = conn.execute(
                text("SELECT 1 FROM sqlite_master WHERE type='index' AND name=:n"),
                {"n": name}).scalar_one_or_none()
            if have:
                continue
            conn.execute(text(ddl))
            changed += 1
            log.info("migration: index %s created", name)
    return changed
