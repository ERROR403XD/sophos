"""R1(P1/P2/P3) 面容准入与整理测试（ADR-014）：合成样本，不依赖模型/ffmpeg。

覆盖 docs/PLAN_v1.0.1.md §10：
- identity 级性别裁决（_gender_filter）：男性组整组拒绝 / 女性组保留 / 单样本严阈值
- 入库整理（_persist_groups）：全遮挡 mean_embedding 置空+标记 / 干净样本隔离 /
  rep 选正面清晰样本不选模糊侧面 / top-N 修剪
- personalizer：全遮挡 identity 不进训练集（test_full_occluded_excluded_from_training）
"""
import numpy as np
import pytest
from sqlalchemy import select

from app.db.models import Face, FaceIdentity, FaceScore, UserRating, Video
from app.services.pipeline import (
    SINGLE_SAMPLE_FEMALE_THRESHOLD,
    _gender_filter,
    _persist_groups,
)


def _sample(idx, female_prob=0.9, quality=0.5, pose_class="frontal",
            occlusion_score=None, base_score=None):
    from app.services.face_engine import FaceSample

    rng = np.random.default_rng(idx)
    emb = rng.normal(0, 1, 16).astype(np.float32)
    emb /= np.linalg.norm(emb)
    aligned = np.zeros((112, 112, 3), dtype=np.uint8)  # 缩略图编码用
    return FaceSample(
        bbox=(10.0 * idx, 10, 100, 100), det_score=0.9, quality_score=quality,
        female_prob=female_prob, timestamp_sec=float(idx),
        aligned=aligned, embedding=emb,
        pose_yaw=0.0 if pose_class == "frontal" else 30.0,
        pose_pitch=0.0, pose_class=pose_class,
        occlusion_score=occlusion_score, base_score=base_score)


@pytest.fixture()
def video(db):
    v = Video(path="/media/v/r1.mp4", filename="r1.mp4", dir_path="/media/v",
              size_bytes=1, mtime=1.0, status="pending")
    db.add(v)
    db.commit()
    db.refresh(v)
    return v


# ---------------- P1：identity 级性别裁决 ----------------

def test_male_group_rejected(db, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "female_identity_threshold", 0.60)
    samples = [_sample(i, female_prob=fp) for i, fp in enumerate([0.2, 0.3, 0.45])]
    groups = [[0, 1, 2]]
    kept = _gender_filter(groups, samples)
    assert kept == []  # 男性组（偶发单帧 fp>0.5 也被组均值稀释拒绝）


def test_female_group_kept_and_male_fp_spikes_diluted(db, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "female_identity_threshold", 0.60)
    # 女性组带 1 帧男性误判尖峰：均值仍过阈
    samples = [_sample(i, female_prob=fp) for i, fp in enumerate([0.9, 0.85, 0.2, 0.95])]
    kept = _gender_filter([[0, 1, 2, 3]], samples)
    assert kept == [[0, 1, 2, 3]]


# ---------------- R2：CLIP 一致同意门控 ----------------

def _with_clip(sample, clip_female):
    sample.clip_female_prob = clip_female
    sample.clip_profile_prob = 0.1
    return sample


def test_gender_agreement_gate_rejects_high_fp_male(db, monkeypatch):
    """R2 核心场景：genderage 高置信误判（0.83~1.0）但 CLIP 判男 → 整组拒绝。"""
    from app.config import settings

    monkeypatch.setattr(settings, "female_identity_threshold", 0.60)
    monkeypatch.setattr(settings, "clip_gender_min", 0.50)
    samples = [_with_clip(_sample(i, female_prob=fp), cf)
               for i, (fp, cf) in enumerate([(0.98, 0.04), (1.0, 0.07)])]
    assert _gender_filter([[0, 1]], samples) == []  # 实测 Ted 类男性 fp≥0.83/clip≤0.07


def test_gender_agreement_gate_keeps_consistent_female(db, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "female_identity_threshold", 0.60)
    monkeypatch.setattr(settings, "clip_gender_min", 0.50)
    samples = [_with_clip(_sample(i, female_prob=fp), cf)
               for i, (fp, cf) in enumerate([(0.92, 0.91), (0.88, 0.63)])]
    assert _gender_filter([[0, 1]], samples) == [[0, 1]]


def test_gender_gate_degrades_without_clip(db, monkeypatch):
    """clip 值缺失（旧模型/缺文件）→ 退回仅 genderage 行为。"""
    from app.config import settings

    monkeypatch.setattr(settings, "female_identity_threshold", 0.60)
    samples = [_sample(i, female_prob=fp) for i, fp in enumerate([0.9, 0.8])]
    samples[0].clip_female_prob = 0.9  # 只有一个样本有值 → 视为不可用
    assert _gender_filter([[0, 1]], samples) == [[0, 1]]


# ---------------- R2：侧脸 rep 不入库 ----------------

def test_side_rep_identity_rejected(db, video, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "allow_side_rep", False)
    samples = [_sample(0, quality=0.9, pose_class="side", base_score=80.0),
               _sample(1, quality=0.5, pose_class="side", base_score=60.0)]
    assert _persist_groups(db, video, samples, [[0, 1]]) == 0  # 整组跳过
    assert db.execute(select(FaceIdentity)).scalars().all() == []


def test_side_rep_identity_kept_when_allowed(db, video, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "allow_side_rep", True)
    samples = [_sample(0, quality=0.9, pose_class="side", base_score=80.0)]
    assert _persist_groups(db, video, samples, [[0]]) == 1


def test_single_sample_stricter_threshold(db, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "female_identity_threshold", 0.60)
    samples = [_sample(0, female_prob=0.62), _sample(1, female_prob=0.66)]
    kept = _gender_filter([[0], [1]], samples)
    # 0.62 ≥ 0.60 但 < 单样本严阈值 0.65 → 拒；0.66 → 留
    assert SINGLE_SAMPLE_FEMALE_THRESHOLD == 0.65
    assert kept == [[1]]


# ---------------- P2/P3：入库整理（rep 优选 / 修剪 / 遮挡） ----------------

def test_persist_full_occluded_identity(db, video):
    """整组无干净样本：入库但 mean_embedding 为空 + fully-occluded 标记。"""
    samples = [_sample(0, occlusion_score=0.9, quality=0.8, base_score=70.0),
               _sample(1, occlusion_score=0.95, quality=0.6, base_score=65.0)]
    n = _persist_groups(db, video, samples, [[0, 1]])
    db.commit()
    assert n == 1
    ident = db.execute(select(FaceIdentity)).scalars().one()
    assert ident.mean_embedding is None
    assert ident.occluded == 1 and ident.occluded_source == "auto"
    assert ident.n_samples == 2
    # 样本行照常入库（库内保留语义），遮挡分随行记录
    faces = db.execute(select(Face)).scalars().all()
    assert len(faces) == 2
    assert {f.occlusion_score for f in faces} == {0.9, 0.95}
    # 评分照常入库（页面展示/统计用）
    assert db.execute(select(FaceScore)).scalars().one().base_score == 70.0


def test_persist_mean_embedding_uses_clean_samples_only(db, video):
    """P2 特征隔离：有干净样本时 mean_embedding 只用干净样本计算。"""
    occ = _sample(0, occlusion_score=0.9, quality=0.99)   # 高质量但遮挡
    clean1 = _sample(1, occlusion_score=0.1, quality=0.5)
    clean2 = _sample(2, occlusion_score=0.2, quality=0.4)
    _persist_groups(db, video, [occ, clean1, clean2], [[0, 1, 2]])
    db.commit()
    ident = db.execute(select(FaceIdentity)).scalars().one()
    expected = np.mean([clean1.embedding, clean2.embedding], axis=0)
    actual = np.frombuffer(ident.mean_embedding, dtype=np.float32)
    assert np.allclose(actual, expected, atol=1e-6)
    # 1/3 遮挡非多数 → 不标记
    assert ident.occluded == 0


def test_persist_rep_prefers_clean_frontal_over_blurry_side(db, video):
    """P3/3c：rep 选正面清晰样本，不选高质量模糊侧面。"""
    blurry_side = _sample(0, quality=0.99, pose_class="side", base_score=99.0)
    clean_frontal = _sample(1, quality=0.30, pose_class="frontal", base_score=55.0)
    _persist_groups(db, video, [blurry_side, clean_frontal], [[0, 1]])
    db.commit()
    ident = db.execute(select(FaceIdentity)).scalars().one()
    rep = db.get(Face, ident.rep_face_id)
    assert rep.pose_class == "frontal"
    assert db.get(FaceScore, ident.id).base_score == 55.0  # base_score 跟随 rep


def test_persist_prunes_to_top_n_for_mean_embedding(db, video, monkeypatch):
    """P3/3b：mean_embedding 只用 top-N（质量排序），n_samples 记全量。"""
    from app.config import settings

    monkeypatch.setattr(settings, "max_samples_per_identity", 4)
    samples = [_sample(i, quality=q) for i, q in enumerate([0.1, 0.9, 0.5, 0.3, 0.7, 0.2])]
    _persist_groups(db, video, samples, [[0, 1, 2, 3, 4, 5]])
    db.commit()
    ident = db.execute(select(FaceIdentity)).scalars().one()
    assert ident.n_samples == 6
    # top-4 质量 = 0.9, 0.7, 0.5, 0.3 → 样本 idx 1,4,2,3
    expected = np.mean([samples[1].embedding, samples[4].embedding,
                        samples[2].embedding, samples[3].embedding], axis=0)
    actual = np.frombuffer(ident.mean_embedding, dtype=np.float32)
    assert np.allclose(actual, expected, atol=1e-6)


# ---------------- P2：personalizer 剔除全遮挡 ----------------

def test_full_occluded_excluded_from_training(db, video):
    """fully-occluded identity：评分在库但不进训练集统计。"""
    ident = FaceIdentity(video_id=video.id, n_samples=2, occluded=1,
                         occluded_source="auto", mean_embedding=None)
    db.add(ident)
    db.flush()
    db.add(Face(video_id=video.id, identity_id=ident.id, quality_score=0.8))
    db.add(FaceScore(identity_id=ident.id, base_score=70.0, base_model_version="t"))
    db.add(UserRating(identity_id=ident.id, rating_type="score", rating_value="9"))
    db.commit()

    from app.services import personalizer

    assert personalizer.identity_features(db) == {}      # 无特征产出
    data = personalizer.build_training_set(db)
    assert data["abs"] == []                             # 不进训练集


# ---------------- P2：遮挡人工标记 API ----------------

def test_occlusion_endpoint_manual_override(client, db, video):
    ident = FaceIdentity(video_id=video.id, n_samples=1, occluded=1,
                         occluded_source="auto")
    db.add(ident)
    db.commit()

    # manual 覆盖 auto
    r = client.post(f"/api/faces/{ident.id}/occlusion", json={"occluded": False})
    assert r.status_code == 200 and r.json()["occluded"] is False
    db.expire_all()
    row = db.get(FaceIdentity, ident.id)
    assert row.occluded == 0 and row.occluded_source == "manual"

    r = client.post(f"/api/faces/{ident.id}/occlusion", json={"occluded": True})
    assert r.json()["occluded"] is True

    # 校验与 404
    assert client.post(f"/api/faces/{ident.id}/occlusion", json={"occluded": "x"}).status_code == 400
    assert client.post("/api/faces/9999/occlusion", json={"occluded": True}).status_code == 404


def test_faces_list_exposes_gender_and_occlusion(client, db, video):
    ident = FaceIdentity(video_id=video.id, n_samples=3,
                         female_prob_mean=0.83, occluded=1, occluded_source="auto")
    db.add(ident)
    db.commit()
    items = client.get("/api/faces").json()["items"]
    row = next(i for i in items if i["id"] == ident.id)
    assert row["female_prob_mean"] == pytest.approx(0.83)
    assert row["occluded"] is True


# ---------------- R6：性别门保底（ADR-028，宁缺毋滥的"至少一张"面） ----------------

def _rescue_inputs(monkeypatch):
    from app.config import settings
    from app.services.pipeline import _gender_filter_detailed, _rescue_group

    monkeypatch.setattr(settings, "female_identity_threshold", 0.60)
    monkeypatch.setattr(settings, "clip_gender_min", 0.50)
    monkeypatch.setattr(settings, "min_quality", 0.30)
    return _gender_filter_detailed, _rescue_group


def test_rescue_keeps_borderline_female_cluster(db, monkeypatch):
    """全 cluster 被性别门拒掉时，边际女性 cluster（两票各过半）保底 1 张。"""
    detailed, rescue = _rescue_inputs(monkeypatch)
    samples = [_sample(0, female_prob=0.25, quality=0.6),   # 男性簇
               _sample(1, female_prob=0.55, quality=0.6)]   # 边际女（0.55 < 0.65 严阈值）
    kept, rejected = detailed([[0], [1]], samples)
    assert kept == []
    assert rescue(rejected, samples) == [[1]]


def test_rescue_picks_best_quality_frontal_cluster(db, monkeypatch):
    detailed, rescue = _rescue_inputs(monkeypatch)
    samples = [_sample(0, female_prob=0.52, quality=0.20, pose_class="side"),
               _sample(1, female_prob=0.55, quality=0.55, pose_class="frontal"),
               _sample(2, female_prob=0.53, quality=0.40, pose_class="near")]
    _kept, rejected = detailed([[0], [1], [2]], samples)
    assert rescue(rejected, samples) == [[1]]  # 只保 1 个：质量最优且正面


def test_rescue_skips_poor_evidence(db, monkeypatch):
    """护栏：纯男性 / CLIP 否决 / 侧脸 rep / 过暗（<min_quality×0.5）都不保底。"""
    detailed, rescue = _rescue_inputs(monkeypatch)

    # 1) 纯男性证据（fp < 0.50）→ 颗粒无收是正确结果
    samples = [_sample(0, female_prob=0.30, quality=0.6)]
    _kept, rejected = detailed([[0]], samples)
    assert rescue(rejected, samples) == []

    # 2) CLIP 第二票 < 0.50（genderage 误判场景）→ 不保
    samples = [_with_clip(_sample(0, female_prob=0.70, quality=0.6), 0.05)]
    _kept, rejected = detailed([[0]], samples)
    assert rejected and rejected[0]["clip_mean"] == pytest.approx(0.05)
    assert rescue(rejected, samples) == []

    # 3) rep 为侧脸 → 不保（保底也须"完整面庞"）
    samples = [_sample(0, female_prob=0.55, quality=0.9, pose_class="side")]
    _kept, rejected = detailed([[0]], samples)
    assert rescue(rejected, samples) == []

    # 4) 过暗（quality 0.05 < 0.30×0.5）→ 不保（宁缺毋滥）
    samples = [_sample(0, female_prob=0.55, quality=0.05)]
    _kept, rejected = detailed([[0]], samples)
    assert rescue(rejected, samples) == []


def test_rescue_multi_sample_cluster_with_clip_ok(db, monkeypatch):
    """多样本簇（均值 <0.60 被拒）但双票 ≥0.50 → 可保底。"""
    detailed, rescue = _rescue_inputs(monkeypatch)
    samples = [_with_clip(_sample(0, female_prob=0.53, quality=0.7), 0.80),
               _with_clip(_sample(1, female_prob=0.56, quality=0.5), 0.62)]
    _kept, rejected = detailed([[0, 1]], samples)
    assert rescue(rejected, samples) == [[0, 1]]
