"""SQLAlchemy 2.0 模型 —— 与 docs/DATA_MODEL.md 一一对应（M2 建齐九张表）。

注意：
- SQLite 默认不启用外键强制（PRAGMA foreign_keys=OFF），级联删除由应用层负责
  （见 docs/ARCHITECTURE.md §3.1 幂等设计）；表定义中的 ForeignKey 用于文档化与未来迁移。
- face.identity_id ↔ face_identity.rep_face_id 构成循环引用，为避免建表顺序问题，
  这两列用普通 Integer 表示逻辑外键（已加注释标明）。
- 项目可能位于 SMB 网络盘（实测 X: → \\<SMB-share>），WAL 依赖共享内存不可靠，
  故保持默认 DELETE 日志模式，仅设置 busy timeout（见 session.py）。
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import BLOB, Float, ForeignKey, Index, Integer, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Base(DeclarativeBase):
    pass


class Video(Base):
    __tablename__ = "video"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    path: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    filename: Mapped[str] = mapped_column(Text, nullable=False)
    dir_path: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    size_bytes: Mapped[int | None] = mapped_column(Integer)
    mtime: Mapped[float | None] = mapped_column(Float)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending", index=True)
    status_msg: Mapped[str | None] = mapped_column(Text)
    duration_sec: Mapped[float | None] = mapped_column(Float)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    identity_count: Mapped[int | None] = mapped_column(Integer)
    # R1(P4)：扫描期 ffprobe 探测的编码（h264/hevc/...），决定播放分档；探测失败留空播放期再试
    vcodec: Mapped[str | None] = mapped_column(Text)
    acodec: Mapped[str | None] = mapped_column(Text)
    # R9（ADR-030）：文件头嗅探的实测容器（mp4/matroska/mpegts/...）。扩展名会骗人
    #（实测 ".mp4" 实为 MPEG-TS），播放分档以实测容器为准；留空=未知（回退扩展名）
    container: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(Text, default=utcnow)
    updated_at: Mapped[str] = mapped_column(Text, default=utcnow, onupdate=utcnow)


class Face(Base):
    __tablename__ = "face"
    # R5（ADR-024）：同帧共现 pair 计算（video_id + timestamp 滑窗）的复合索引；
    # 旧库由 migrations.run_migrations 兜底补建
    __table_args__ = (Index("ix_face_video_ts", "video_id", "timestamp_sec"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    video_id: Mapped[int] = mapped_column(ForeignKey("video.id"), nullable=False, index=True)
    identity_id: Mapped[int | None] = mapped_column(Integer, index=True)  # 逻辑外键 -> face_identity.id
    timestamp_sec: Mapped[float | None] = mapped_column(Float)
    thumb_path: Mapped[str | None] = mapped_column(Text)
    bbox_x1: Mapped[float | None] = mapped_column(Float)
    bbox_y1: Mapped[float | None] = mapped_column(Float)
    bbox_x2: Mapped[float | None] = mapped_column(Float)
    bbox_y2: Mapped[float | None] = mapped_column(Float)
    det_score: Mapped[float | None] = mapped_column(Float)
    quality_score: Mapped[float | None] = mapped_column(Float)
    female_prob: Mapped[float | None] = mapped_column(Float)  # R1(P1)：降级为样本级记录值，identity 级裁决见 face_identity.female_prob_mean
    pose_yaw: Mapped[float | None] = mapped_column(Float)    # R1(P2)：5 点启发式姿态（度）；存量行为空 = 不参与偏好
    pose_pitch: Mapped[float | None] = mapped_column(Float)
    pose_class: Mapped[str | None] = mapped_column(Text)      # frontal|near|side
    occlusion_score: Mapped[float | None] = mapped_column(Float)  # R1(P2)：0-1 启发式遮挡提示（下半区低纹理）
    clip_female_prob: Mapped[float | None] = mapped_column(Float)  # R2：CLIP 女性 0-1（缺模型/旧行为空）
    clip_profile_prob: Mapped[float | None] = mapped_column(Float)  # R2：CLIP 侧脸 0-1
    embedding: Mapped[bytes | None] = mapped_column(BLOB)
    created_at: Mapped[str] = mapped_column(Text, default=utcnow)


class FaceIdentity(Base):
    __tablename__ = "face_identity"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    video_id: Mapped[int] = mapped_column(ForeignKey("video.id"), nullable=False, index=True)
    rep_face_id: Mapped[int | None] = mapped_column(Integer)  # 逻辑外键 -> face.id
    mean_embedding: Mapped[bytes | None] = mapped_column(BLOB)
    n_samples: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    female_prob_mean: Mapped[float | None] = mapped_column(Float)  # R1(P1)：组内样本 female_prob 均值（identity 级裁决依据）
    clip_female_mean: Mapped[float | None] = mapped_column(Float)  # R2：组内 CLIP 女性均值（一致同意门控第二票）
    occluded: Mapped[int] = mapped_column(Integer, default=0)      # R1(P2)：0/1 遮挡标记（库内保留，仅影响训练）
    occluded_source: Mapped[str | None] = mapped_column(Text)      # auto|manual（manual 覆盖 auto）
    created_at: Mapped[str] = mapped_column(Text, default=utcnow)
    updated_at: Mapped[str] = mapped_column(Text, default=utcnow, onupdate=utcnow)


class FaceScore(Base):
    __tablename__ = "face_score"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    identity_id: Mapped[int] = mapped_column(
        ForeignKey("face_identity.id"), nullable=False, unique=True, index=True
    )
    base_score: Mapped[float | None] = mapped_column(Float)
    base_model_version: Mapped[str | None] = mapped_column(Text)
    personalized_score: Mapped[float | None] = mapped_column(Float)
    pers_model_version: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[str] = mapped_column(Text, default=utcnow, onupdate=utcnow)


class UserRating(Base):
    __tablename__ = "user_rating"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    identity_id: Mapped[int] = mapped_column(ForeignKey("face_identity.id"), nullable=False, index=True)
    rating_type: Mapped[str] = mapped_column(Text, nullable=False)   # score | thumbs
    rating_value: Mapped[str] = mapped_column(Text, nullable=False)  # "1".."10" | "up"/"down"
    created_at: Mapped[str] = mapped_column(Text, default=utcnow)


class PairComparison(Base):
    """两两对比结果（ADR-013）：用户在 A/B 中选择更好的一张。"""

    __tablename__ = "pair_comparison"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    winner_identity_id: Mapped[int] = mapped_column(
        ForeignKey("face_identity.id"), nullable=False, index=True
    )
    loser_identity_id: Mapped[int] = mapped_column(
        ForeignKey("face_identity.id"), nullable=False, index=True
    )
    created_at: Mapped[str] = mapped_column(Text, default=utcnow)


class VideoScore(Base):
    __tablename__ = "video_score"

    video_id: Mapped[int] = mapped_column(ForeignKey("video.id"), primary_key=True)
    final_score: Mapped[float | None] = mapped_column(Float, index=True)
    base_final: Mapped[float | None] = mapped_column(Float)
    personalized_final: Mapped[float | None] = mapped_column(Float)
    identity_count: Mapped[int | None] = mapped_column(Integer)
    topk_detail: Mapped[str | None] = mapped_column(Text)  # JSON
    score_model_version: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[str] = mapped_column(Text, default=utcnow, onupdate=utcnow)


class Job(Base):
    __tablename__ = "job"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    type: Mapped[str] = mapped_column(Text, nullable=False)  # scan | process | train
    status: Mapped[str] = mapped_column(Text, nullable=False, default="queued", index=True)
    done: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    params: Mapped[str | None] = mapped_column(Text)   # JSON
    result: Mapped[str | None] = mapped_column(Text)   # JSON（scan 报告 / train 指标等）
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(Text, default=utcnow)
    started_at: Mapped[str | None] = mapped_column(Text)
    finished_at: Mapped[str | None] = mapped_column(Text)


class KVSetting(Base):
    __tablename__ = "kv_setting"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False, default="{}")  # JSON
    updated_at: Mapped[str] = mapped_column(Text, default=utcnow, onupdate=utcnow)
