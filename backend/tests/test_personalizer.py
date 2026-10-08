"""接续训练测试（M6）：合成数据训练冒烟、指标、启用/回退、样本不足拒绝。
R11：偏好模型导出/导入（roundtrip/重编号/非法包拒绝/API 层/NaN 兼容）。"""
import io
import json
import shutil
import time
import zipfile

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


def test_activation_failure_rolls_back_scores_and_active_version(
        db, model_dir, training_data, monkeypatch):
    personalizer.train(db, model_dir)
    original_recompute = aggregator.recompute_all
    calls = 0

    def fail_first_recompute(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("simulated aggregate failure")
        return original_recompute(*args, **kwargs)

    monkeypatch.setattr(aggregator, "recompute_all", fail_first_recompute)
    with pytest.raises(RuntimeError, match="simulated aggregate failure"):
        personalizer.activate(db, model_dir, "v1")

    assert personalizer.active_version(db) is None
    assert db.execute(select(FaceScore.pers_model_version)).scalars().all() == [None] * 12
    assert all(score.personalized_score is None
               for score in db.execute(select(FaceScore)).scalars())
    video_score = db.get(VideoScore, training_data["video"].id)
    assert video_score.final_score == video_score.base_final


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

    # R14：启用为后台任务（202 + job），完成后 active_version 生效
    r = client.post("/api/train/activate/v1")
    assert r.status_code == 202
    activate_job_id = r.json()["job"]["id"]
    deadline = time.time() + 20
    while time.time() < deadline:
        body = client.get(f"/api/jobs/{activate_job_id}").json()
        if body["status"] in ("done", "failed"):
            break
        time.sleep(0.1)
    assert body["status"] == "done", body.get("error")
    status = client.get("/api/train/status").json()
    assert status["active_version"] == "v1"
    versions = client.get("/api/train/versions").json()["versions"]
    assert versions[0]["active"] is True

    items = client.get("/api/videos").json()["items"]
    assert items[0]["personalized_final"] is not None

    # 未知版本 404
    assert client.post("/api/train/activate/v42").status_code == 404


# ---------------- R11：导出/导入 ----------------

def _fake_export_npz(dim: int) -> bytes:
    buf = io.BytesIO()
    np.savez(buf, w=np.zeros(dim, dtype=np.float32), b=np.float32(50.0),
             x_mean=np.zeros(dim, dtype=np.float32),
             x_std=np.ones(dim, dtype=np.float32))
    return buf.getvalue()


def test_export_import_roundtrip(db, model_dir, training_data, monkeypatch):
    """导出 → 清空（模拟换机）→ 导入 → 手动启用：偏好完整迁移。"""
    monkeypatch.setattr(personalizer, "FEATURE_DIM", 10)  # 测试特征为 8 维合成
    meta = personalizer.train(db, model_dir)
    assert meta["feature_dim"] == 10
    w_before = np.load(model_dir / "personalizer" / "v1.npz")["w"]

    payload, filename = personalizer.export_version(model_dir, "v1")
    assert filename == "sophos-personalizer-v1.zip"

    shutil.rmtree(model_dir / "personalizer")
    with pytest.raises(FileNotFoundError):
        personalizer.activate(db, model_dir, "v1")

    imported = personalizer.import_version(model_dir, payload)
    assert imported["version"] == "v1"
    assert imported["n_abs"] == 12 and imported["n_pair"] == 8  # 训练指标随行
    z = np.load(model_dir / "personalizer" / "v1.npz")
    assert np.allclose(z["w"], w_before)

    # 导入不自动启用
    assert personalizer.active_version(db) is None
    assert db.get(FaceScore, training_data["good"][0].id).personalized_score is None

    # 手动启用后正常生效
    assert personalizer.activate(db, model_dir, "v1")["version"] == "v1"
    db.expire_all()
    assert db.get(FaceScore, training_data["good"][0].id).personalized_score is not None


def test_import_renumbers_on_conflict(db, model_dir, training_data, monkeypatch):
    monkeypatch.setattr(personalizer, "FEATURE_DIM", 10)
    personalizer.train(db, model_dir)  # v1
    payload, _ = personalizer.export_version(model_dir, "v1")
    imported = personalizer.import_version(model_dir, payload)
    assert imported["version"] == "v2"  # v1 已存在 → 重编号，不覆盖
    assert [v["version"] for v in personalizer.list_versions(model_dir)] == ["v2", "v1"]


def test_import_rejects_invalid(model_dir):
    with pytest.raises(ValueError):
        personalizer.import_version(model_dir, b"not a zip")
    # zip 缺 meta.json
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("v1.npz", _fake_export_npz(8))
    with pytest.raises(ValueError):
        personalizer.import_version(model_dir, buf.getvalue())
    # 特征维度与当前定义不符（默认 FEATURE_DIM=514，包里是 999 维）
    buf2 = io.BytesIO()
    with zipfile.ZipFile(buf2, "w") as z:
        z.writestr("v9.npz", _fake_export_npz(999))
        z.writestr("v9.meta.json", json.dumps({"version": "v9"}))
    with pytest.raises(ValueError, match="feature dimension"):
        personalizer.import_version(model_dir, buf2.getvalue())
    # meta 缺合法 version
    buf3 = io.BytesIO()
    with zipfile.ZipFile(buf3, "w") as z:
        z.writestr("v9.npz", _fake_export_npz(8))
        z.writestr("v9.meta.json", json.dumps({"version": "oops"}))
    with pytest.raises(ValueError):
        personalizer.import_version(model_dir, buf3.getvalue())


def test_train_export_import_api(client, db, model_dir, training_data, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "models_dir", model_dir)
    monkeypatch.setattr(personalizer, "FEATURE_DIM", 10)
    personalizer.train(db, model_dir)

    r = client.get("/api/train/versions/v1/export")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"
    assert r.content[:2] == b"PK"
    assert client.get("/api/train/versions/v9/export").status_code == 404

    imp = client.post("/api/train/import",
                      files={"file": ("export.zip", r.content, "application/zip")})
    assert imp.status_code == 200
    assert imp.json()["meta"]["version"] == "v2"
    assert client.get("/api/train/versions").json()["active_version"] is None  # 不自动启用

    bad = client.post("/api/train/import",
                      files={"file": ("x.zip", b"garbage", "application/zip")})
    assert bad.status_code == 400
    assert bad.json()["code"] == "INVALID_EXPORT"

    assert [v["version"] for v in
            client.get("/api/train/versions").json()["versions"]] == ["v2", "v1"]


def test_train_versions_api_nan_meta_safe(client, model_dir, monkeypatch):
    """极端训练产出 NaN 指标（真实库 v1 即如此）时版本列表仍可用：NaN → null。"""
    from app.config import settings

    monkeypatch.setattr(settings, "models_dir", model_dir)
    out = model_dir / "personalizer"
    out.mkdir(parents=True)
    with open(out / "v1.npz", "wb") as f:
        f.write(_fake_export_npz(8))
    (out / "v1.meta.json").write_text(json.dumps(
        {"version": "v1", "n_abs": 1, "n_pair": 0, "mae_train": float("nan"),
         "r2_train": None, "pair_acc": None, "feature_dim": 8, "created_at": "t"}),
        encoding="utf-8")
    r = client.get("/api/train/versions")
    assert r.status_code == 200
    assert r.json()["versions"][0]["mae_train"] is None


# ---------------- R14：activate/deactivate 任务化 + 进度 ----------------

def test_activate_deactivate_job_api(client, db, model_dir, training_data, monkeypatch):
    """启用/停用走 interactive 池任务：202 即返回，进度字段随任务下发。"""
    from app.config import settings

    monkeypatch.setattr(settings, "models_dir", model_dir)
    personalizer.train(db, model_dir)

    r = client.post("/api/train/activate/v1")
    assert r.status_code == 202
    job = r.json()["job"]
    assert job["pool"] == "interactive"
    deadline = time.time() + 20
    while time.time() < deadline:
        body = client.get(f"/api/jobs/{job['id']}").json()
        if body["status"] in ("done", "failed"):
            break
        time.sleep(0.1)
    assert body["status"] == "done", body.get("error")
    assert client.get("/api/train/status").json()["active_version"] == "v1"
    assert body["detail"]  # 分相进度文本已落库

    # 409：activate 进行中再提交被拒（慢 handler 占住 interactive worker）
    import threading

    from app.services import jobs as jobsvc

    release = threading.Event()

    def slow_activate(session, job, params):
        release.wait(5)

    monkeypatch.setitem(jobsvc.HANDLERS, "activate", slow_activate)
    try:
        job2 = client.post("/api/train/activate/v1")
        assert job2.status_code == 202
        assert client.post("/api/train/activate/v1").status_code == 409
        assert client.post("/api/train/deactivate").status_code == 409
    finally:
        release.set()
    deadline = time.time() + 10
    while time.time() < deadline:
        body2 = client.get(f"/api/jobs/{job2.json()['job']['id']}").json()
        if body2["status"] in ("done", "failed"):
            break
        time.sleep(0.1)
    assert body2["status"] == "done"

    # 停用也走任务
    r = client.post("/api/train/deactivate")
    assert r.status_code == 202
    deadline = time.time() + 20
    while time.time() < deadline:
        body = client.get(f"/api/jobs/{r.json()['job']['id']}").json()
        if body["status"] in ("done", "failed"):
            break
        time.sleep(0.1)
    assert body["status"] == "done", body.get("error")
    assert client.get("/api/train/status").json()["active_version"] is None

    # 预检：不存在的版本同步 404（不产生注定失败的 job）
    assert client.post("/api/train/activate/v99").status_code == 404


def test_apply_version_progress_callbacks(db, model_dir, training_data):
    """apply 两相进度回调（扫描+写入）：终值 = (2n, 2n)。"""
    personalizer.train(db, model_dir)
    apply_seen = []
    agg_seen = []
    personalizer.activate(db, model_dir, "v1",
                          apply_progress=lambda d, t: apply_seen.append((d, t)),
                          aggregate_progress=lambda d, t: agg_seen.append((d, t)))
    assert apply_seen[-1] == (24, 24)  # 12 identity × 2 相
    assert apply_seen[0] == (0, 24)
    assert agg_seen and agg_seen[-1][1] == 1  # 仅 1 个视频


def test_make_detail_updater_throttle(db):
    """detail 节流更新：force 立即落库；未到间隔的更新只在内存。"""
    from app.db.models import Job
    from app.services.jobs import make_detail_updater

    job = Job(type="detailtest", status="running")
    db.add(job)
    db.commit()
    upd = make_detail_updater(db, job, min_interval=60.0)
    upd("帧 1", force=True)
    db.expire_all()
    assert db.get(Job, job.id).detail == "帧 1"
    upd("帧 2")  # 未到节流间隔：内存已更新但不提交
    db.expire_all()
    assert db.get(Job, job.id).detail == "帧 1"


def test_scores_bounded_no_mass_ties(db, model_dir, training_data):
    """R14.2/ADR-035：百分位映射——个性化分严格落在 (0,100) 开区间且两两不同，
    全库铺满区间（不再有并列的 100/99.99）。"""
    personalizer.train(db, model_dir)
    personalizer.activate(db, model_dir, "v1")
    db.expire_all()
    scores = [r.personalized_score for r in db.execute(select(FaceScore)).scalars()
              if r.personalized_score is not None]
    assert scores
    assert all(0.01 <= s <= 99.99 for s in scores)
    assert len(set(scores)) == len(scores)  # 12 张两两不同（4 位小数）
    assert min(scores) <= 1.0 and max(scores) >= 99.0  # 铺满区间两端
    good = db.get(FaceScore, training_data["good"][0].id).personalized_score
    bad = db.get(FaceScore, training_data["bad"][0].id).personalized_score
    assert good > bad + 20  # 偏好方向仍显著拉开


def test_apply_percentile_order_preserved(db, model_dir, training_data, monkeypatch):
    """病态模型（raw 悬殊）apply 百分位：仍落在 (0,100) 且严格保序——
    旧软饱和方案下 raw>250 会并列 99.99，百分位从数学上排除。"""
    monkeypatch.setattr(personalizer, "FEATURE_DIM", 10)
    import json as _json

    out = model_dir / "personalizer"
    out.mkdir(parents=True)
    # raw₁ = 90·1+50 = 140，raw₂ = 200·1+50 = 250（clip 旧语义下并列 100）
    w = np.zeros(10, dtype=np.float32)
    w[0], w[1] = 90.0, 200.0
    np.savez(out / "v1.npz", w=w, b=np.float32(50.0),
             x_mean=np.zeros(10, dtype=np.float32),
             x_std=np.ones(10, dtype=np.float32))
    (out / "v1.meta.json").write_text(_json.dumps(
        {"version": "v1", "feature_dim": 10}), encoding="utf-8")

    ident_a = _mk_identity(db, [1, 0, 0, 0, 0, 0, 0, 0], base=50, seed=1)
    ident_b = _mk_identity(db, [0, 1, 0, 0, 0, 0, 0, 0], base=50, seed=2)
    personalizer.apply_version(db, model_dir, "v1")
    db.expire_all()
    sa = db.get(FaceScore, ident_a.id).personalized_score
    sb = db.get(FaceScore, ident_b.id).personalized_score
    assert 0.01 <= sa <= 99.99 and 0.01 <= sb <= 99.99
    assert sa != sb
    assert sb > sa   # 保序


def test_reapply_recomputes_stale_scores(db, model_dir, training_data):
    """R14 修复：同版本重复启用必须重算——修复前 stamped 旧语义分数（如 100）
    会被幂等跳过永久保留（用户部署后仍见大片 100 的根因）。"""
    personalizer.train(db, model_dir)
    personalizer.activate(db, model_dir, "v1")
    db.expire_all()

    # 模拟修复前的残留：把几行改成旧语义的饱和值（版本标记不变）
    rows = db.execute(select(FaceScore)).scalars().all()
    for r in rows[:5]:
        r.personalized_score = 100.0
    db.commit()

    # 重新启用同一版本 → 必须 全量重算（不再跳过），残留 100 被清除
    personalizer.apply_version(db, model_dir, "v1")
    db.expire_all()
    scores = [r.personalized_score for r in db.execute(select(FaceScore)).scalars()]
    assert all(s is not None and 0.01 <= s <= 99.99 for s in scores)
    assert 100.0 not in scores


def test_list_versions_numeric_order(tmp_path, monkeypatch):
    """R14.3：v10+ 排序修复——按数字降序（新版本在前），不受字典序影响。"""
    import json as _json

    out = tmp_path / "personalizer"
    out.mkdir()
    for v in (1, 2, 9, 10, 11):
        (out / f"v{v}.meta.json").write_text(_json.dumps({"version": f"v{v}"}),
                                             encoding="utf-8")
    (out / "vweird.meta.json").write_text("{}", encoding="utf-8")  # 手工杂项：无 version 字段，不入列表
    versions = [m["version"] for m in personalizer.list_versions(tmp_path)]
    assert versions == ["v11", "v10", "v9", "v2", "v1"]
