"""接续训练测试（M6）：合成数据训练冒烟、指标、启用/回退、样本不足拒绝。"""
import numpy as np
import pytest
from sqlalchemy import select

from app.db.models import (Face, FaceIdentity, FaceScore, PairComparison,
                           UserRating, Video, VideoScore)
from app.services import aggregator, personalizer


@pytest.fixture()
def model_dir(tmp_path):
    return tmp_path / "models"


def _mk_identity(db, direction, base, seed):
    """构造一个 identity：embedding 以 direction 为主方向 + 小噪声。"""
    rng = np.random.default_rng(seed)
    emb = np.asarray(direction, dtype=np.float32) + rng.normal(0, 0.01, 8).astype(np.float32)
    emb = emb / np.linalg.norm(emb)
    ident = FaceIdentity(video_id=0, n_samples=3)
    ident.mean_embedding = emb.astype(np.float32).tobytes()
    db.add(ident)
    db.flush()
    face = Face(video_id=0, quality_score=0.8, identity_id=ident.id)
    db.add(face)
    db.flush()
    ident.rep_face_id = face.id
    db.add(FaceScore(identity_id=ident.id, base_score=base, base_model_version="t"))
    db.commit()
    return ident


@pytest.fixture()
def training_data(db):
    """8 组"好看"组（用户喜欢，评分高）+ 4 组"一般"组（评分低）+ pair。"""
    good, bad = [], []
    for i in range(8):
        good.append(_mk_identity(db, [1, i * 0.05, 0, 0, 0, 0, 0, 0], base=60 + i, seed=100 + i))
    for i in range(4):
        bad.append(_mk_identity(db, [0, 0, 1, i * 0.05, 0, 0, 0, 0], base=50 + i, seed=200 + i))

    # 绝对评分：好组 9-10 分（y≈89-100），差组 1-2 分（y≈0-11）
    for k, ident in enumerate(good):
        db.add(UserRating(identity_id=ident.id, rating_type="score", rating_value=str(9 + k % 2)))
    for k, ident in enumerate(bad):
        db.add(UserRating(identity_id=ident.id, rating_type="score", rating_value=str(1 + k % 2)))
    # 两两对比：好组 > 差组（8 条，凑足 MIN_TOTAL_SAMPLES=20）
    for k in range(8):
        db.add(PairComparison(winner_identity_id=good[k].id,
                              loser_identity_id=bad[k % 4].id))
    # 构造一个视频以便聚合
    v = Video(path="X:/v/t.mp4", filename="t.mp4", dir_path=".", status="done")
    db.add(v)
    db.commit()
    for ident in good + bad:
        ident.video_id = v.id
    db.commit()
    aggregator.recompute_video(db, v.id)
    return {"good": good, "bad": bad, "video": v}


def test_train_rejects_when_not_enough(db, model_dir):
    with pytest.raises(personalizer.NotEnoughSamples):
        personalizer.train(db, model_dir)


def test_train_and_activate_flow(db, model_dir, training_data):
    # ---- 训练 ----
    meta = personalizer.train(db, model_dir)
    assert meta["n_abs"] == 12 and meta["n_pair"] == 8
    assert meta["pair_acc"] is not None and meta["pair_acc"] >= 0.75  # 好差分明，应易分
    assert meta["mae_train"] is not None and meta["mae_train"] < 25.0

    versions = personalizer.list_versions(model_dir)
    assert versions[0]["version"] == "v1"

    # 训练但未启用：personalized_score 仍为空，视频分回退基础分
    row = db.get(FaceScore, training_data["good"][0].id)
    assert row.personalized_score is None

    # ---- 启用 v1 ----
    info = personalizer.activate(db, model_dir, "v1")
    assert info["version"] == "v1" and info["videos_recomputed"] >= 1
    assert personalizer.active_version(db) == "v1"

    db.expire_all()
    good_score = db.get(FaceScore, training_data["good"][0].id).personalized_score
    bad_score = db.get(FaceScore, training_data["bad"][0].id).personalized_score
    assert good_score is not None and bad_score is not None
    assert good_score > bad_score + 20  # 偏好方向应显著拉开
    assert 0 <= good_score <= 100 and 0 <= bad_score <= 100

    # 视频综合分切换为个性化分
    vs = db.get(VideoScore, training_data["video"].id)
    assert vs.personalized_final is not None
    assert vs.final_score == vs.personalized_final

    # ---- 回退 ----
    personalizer.deactivate(db)
    assert personalizer.active_version(db) is None
    db.expire_all()
    vs = db.get(VideoScore, training_data["video"].id)
    assert vs.final_score == vs.base_final
    assert db.get(FaceScore, training_data["good"][0].id).personalized_score is None


def test_activate_missing_version(db, model_dir, training_data):
    with pytest.raises(FileNotFoundError):
        personalizer.activate(db, model_dir, "v999")


def test_train_api_flow(client, db, model_dir, training_data, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "models_dir", model_dir)
    import time

    job = client.post("/api/train/start").json()["job"]
    deadline = time.time() + 20
    while time.time() < deadline:
        body = client.get(f"/api/jobs/{job['id']}").json()
        if body["status"] in ("done", "failed"):
            break
        time.sleep(0.1)
    assert body["status"] == "done", body.get("error")
    assert body["result"]["n_abs"] == 12

    # 启用 → 视频表个性化分生效
    assert client.post("/api/train/activate/v1").status_code == 200
    status = client.get("/api/train/status").json()
    assert status["active_version"] == "v1"
    versions = client.get("/api/train/versions").json()["versions"]
    assert versions[0]["active"] is True

    items = client.get("/api/videos").json()["items"]
    assert items[0]["personalized_final"] is not None

    # 未知版本 404
    assert client.post("/api/train/activate/v42").status_code == 404
