"""聚合器测试（M4）：top-K 均值、路人过滤、无分场景、分数行滚动更新。"""
import json

from app.db.models import Face, FaceIdentity, FaceScore, Video, VideoScore
from app.services import aggregator


def _mk_video(db, path):
    v = Video(path=path, filename=Path(path).name, dir_path=".", status="done")
    db.add(v)
    db.commit()
    db.refresh(v)
    return v


def _mk_identity(db, video, base=None, n_samples=2, rep_quality=0.8):
    f = Face(video_id=video.id, quality_score=rep_quality, timestamp_sec=0.0)
    db.add(f)
    db.flush()
    ident = FaceIdentity(video_id=video.id, rep_face_id=f.id, n_samples=n_samples)
    db.add(ident)
    db.flush()
    f.identity_id = ident.id
    if base is not None:
        db.add(FaceScore(identity_id=ident.id, base_score=base, base_model_version="t"))
    db.commit()
    return ident


from pathlib import Path  # noqa: E402


def test_topk_mean_and_bystander_filter(db):
    v = _mk_video(db, "/media/v/a.mp4")
    _mk_identity(db, v, base=90, n_samples=1, rep_quality=0.1)  # 路人：样本少且质量差
    _mk_identity(db, v, base=80, n_samples=3, rep_quality=0.8)
    _mk_identity(db, v, base=70, n_samples=1, rep_quality=0.5)  # 样本少但质量达标
    _mk_identity(db, v, base=60, n_samples=2)
    _mk_identity(db, v, base=50, n_samples=2)

    info = aggregator.recompute_video(db, v.id)
    assert info["eligible"] == 4
    assert info["base"] == round((80 + 70 + 60) / 3, 2)

    row = db.get(VideoScore, v.id)
    assert row.final_score == info["base"]
    assert row.identity_count == 4
    assert len(json.loads(row.topk_detail)) == 3
    assert "base:t" in (row.score_model_version or "")


def test_no_scores_video(db):
    v = _mk_video(db, "/media/v/b.mp4")
    _mk_identity(db, v, base=None)
    info = aggregator.recompute_video(db, v.id)
    assert info["base"] is None and info["final"] is None
    row = db.get(VideoScore, v.id)
    assert row is not None and row.identity_count == 0 and row.final_score is None


def test_recompute_overwrites(db):
    v = _mk_video(db, "/media/v/c.mp4")
    ident = _mk_identity(db, v, base=40, n_samples=2)
    aggregator.recompute_video(db, v.id)
    row = db.get(VideoScore, v.id)
    assert row.final_score == 40.0
    # 分数变化后重算 → 覆盖（滚动更新语义）
    fs = db.query(FaceScore).filter_by(identity_id=ident.id).first()
    fs.base_score = 90.0
    db.commit()
    info = aggregator.recompute_video(db, v.id)
    assert info["base"] == 90.0
