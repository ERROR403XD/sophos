"""处理流水线端到端测试（M3）：扫描 → process job → 面容库/缩略图/API。

- testsrc 合成视频（无人脸）→ done 且 identity_count=0，抽帧目录清理
- lena.jpg 合成视频（真人脸）→ 模型可用时产出 ≥1 个面容 + 缩略图
"""
import subprocess
import time
from pathlib import Path

import pytest
from sqlalchemy import select

from app.db.models import Face, FaceIdentity, PairComparison, Video
from app.services import pipeline
from app.services.frame_sampler import locate_ffmpeg

ROOT = Path(__file__).resolve().parents[2]
LENA = Path(__file__).parent / "assets" / "lena.jpg"
MODELS_OK = (ROOT / "data" / "models" / "det_500m.onnx").is_file()
FFMPEG = locate_ffmpeg() is not None

pytestmark = pytest.mark.skipif(not FFMPEG, reason="ffmpeg not available")


def _make_video(path, src_image=None, seconds=2.0):
    exe = locate_ffmpeg()
    if src_image is not None:
        cmd = [str(exe), "-hide_banner", "-loglevel", "error", "-y",
               "-loop", "1", "-i", str(src_image), "-t", str(seconds),
               "-r", "10", "-pix_fmt", "yuv420p", str(path)]
    else:
        cmd = [str(exe), "-hide_banner", "-loglevel", "error", "-y",
               "-f", "lavfi", "-i", f"testsrc=size=320x240:rate=10:duration={seconds}",
               "-pix_fmt", "yuv420p", str(path)]
    subprocess.run(cmd, check=True, capture_output=True)


def test_reprocess_refuses_when_identity_was_pair_loser(db):
    video = Video(path="X:/v/reprocess.mp4", filename="reprocess.mp4",
                  dir_path=".", status="done")
    db.add(video)
    db.flush()
    target = FaceIdentity(video_id=video.id, n_samples=1)
    winner = FaceIdentity(video_id=video.id, n_samples=1)
    db.add_all([target, winner])
    db.flush()
    db.add(PairComparison(winner_identity_id=winner.id,
                          loser_identity_id=target.id))
    db.commit()

    with pytest.raises(RuntimeError, match="user ratings"):
        pipeline._reset_video_faces(db, video.id)
    assert db.execute(select(FaceIdentity).where(
        FaceIdentity.video_id == video.id)).scalars().all() == [target, winner]


def _wait_job(client, job_id, timeout=600):
    deadline = time.time() + timeout
    while time.time() < deadline:
        body = client.get(f"/api/jobs/{job_id}").json()
        if body["status"] in ("done", "failed"):
            return body
        time.sleep(0.1)
    raise AssertionError("job timeout")


@pytest.fixture()
def fresh_engine():
    pipeline._engine = None
    yield
    pipeline._engine = None


def test_process_video_without_faces(client, tmp_path, fresh_engine):
    d = tmp_path / "vd"
    d.mkdir()
    _make_video(d / "clip.mp4")
    assert client.post("/api/workdirs", json={"path": str(d)}).status_code == 201
    _wait_job(client, client.post("/api/scan/start").json()["job"]["id"])

    r = client.post("/api/process/start")
    assert r.status_code == 202
    body = _wait_job(client, r.json()["job"]["id"])
    assert body["status"] == "done", body.get("error")
    assert body["result"]["ok"] == 1 and body["result"]["failed"] == 0

    items = client.get("/api/videos").json()["items"]
    assert items[0]["status"] == "done"
    assert items[0]["identity_count"] == 0
    assert body["detail"]  # R14：细粒度进度文本（帧级/阶段）已记录

    # 抽帧临时目录应已清理
    frames_root = tmp_path / "data" / "frames"
    assert not frames_root.exists() or not any(frames_root.rglob("*.jpg"))

    # 面容库为空
    assert client.get("/api/faces").json()["total"] == 0


@pytest.mark.skipif(not (MODELS_OK and LENA.is_file()),
                    reason="models or lena.jpg missing")
def test_process_lena_video_end_to_end(client, db, tmp_path, fresh_engine):
    from app.config import settings

    settings.sample_interval_sec = 0.5  # 多抽几帧，验证同人合并为一个面容

    d = tmp_path / "vd2"
    d.mkdir()
    _make_video(d / "lena.mp4", src_image=LENA, seconds=2.0)
    assert client.post("/api/workdirs", json={"path": str(d)}).status_code == 201
    _wait_job(client, client.post("/api/scan/start").json()["job"]["id"])

    r = client.post("/api/process/start")
    assert r.status_code == 202
    body = _wait_job(client, r.json()["job"]["id"])
    assert body["status"] == "done", body.get("error")
    assert body["result"]["failed"] == 0

    identities = db.execute(select(FaceIdentity)).scalars().all()
    assert len(identities) >= 1          # lena 应产出至少一个面容
    faces = db.execute(select(Face)).scalars().all()
    assert len(faces) >= len(identities)
    assert identities[0].rep_face_id is not None
    assert identities[0].mean_embedding is not None

    # R5：缩略图分片存储（thumbs/{id//1000:03d}/{id}.jpg），rglob 覆盖分片
    thumbs = list((tmp_path / "data" / "thumbs").rglob("*.jpg"))
    assert len(thumbs) >= 1

    items = client.get("/api/videos").json()["items"]
    assert items[0]["status"] == "done"
    assert items[0]["identity_count"] == len(identities)

    # 面容 API
    faces_items = client.get("/api/faces").json()["items"]
    assert len(faces_items) >= 1
    # R5：rep_thumb 带 ?v= 内容版本（配合缩略图端点 immutable 缓存）
    assert faces_items[0]["rep_thumb"].startswith(f"/api/thumbs/{identities[0].id}.jpg?v=")
    # M4 起有颜值模型时应有分；模型缺失时为 None
    if (ROOT / "data" / "models" / "beauty_scut.onnx").is_file():
        assert 0.0 <= faces_items[0]["base_score"] <= 100.0
    else:
        assert faces_items[0]["base_score"] is None
    detail = client.get(f"/api/faces/{identities[0].id}").json()
    assert detail["timestamp_sec"] is not None


def test_process_engine_missing_models(client, db, tmp_path, fresh_engine, monkeypatch):
    """模型缺失时 job 明确失败（而非挂死）。"""
    from app.config import settings

    missing = tmp_path / "no_models"
    missing.mkdir()
    monkeypatch.setattr(settings, "models_dir", missing)

    d = tmp_path / "vd3"
    d.mkdir()
    _make_video(d / "clip.mp4")
    client.post("/api/workdirs", json={"path": str(d)})
    _wait_job(client, client.post("/api/scan/start").json()["job"]["id"])

    body = _wait_job(client, client.post("/api/process/start").json()["job"]["id"])
    assert body["status"] == "failed"
    assert "model not found" in (body["error"] or "")


@pytest.mark.skipif(not (MODELS_OK and LENA.is_file()
                         and (ROOT / "data" / "models" / "beauty_scut.onnx").is_file()),
                    reason="models/lena/beauty model missing")
def test_process_lena_with_beauty_scores(client, db, tmp_path, fresh_engine):
    """M4：颜值模型就位时，面容分与视频综合分应落库并可排序。"""
    from app.config import settings
    from app.db.models import FaceScore, VideoScore

    settings.sample_interval_sec = 1.0
    d = tmp_path / "vd4"
    d.mkdir()
    _make_video(d / "lena2.mp4", src_image=LENA, seconds=2.0)
    client.post("/api/workdirs", json={"path": str(d)})
    _wait_job(client, client.post("/api/scan/start").json()["job"]["id"])
    body = _wait_job(client, client.post("/api/process/start").json()["job"]["id"])
    assert body["status"] == "done", body.get("error")

    fs = db.execute(select(FaceScore)).scalars().first()
    assert fs is not None
    assert 0.0 <= fs.base_score <= 100.0
    assert fs.base_model_version

    items = client.get("/api/videos", params={"sort": "final_score"}).json()["items"]
    assert items[0]["final_score"] is not None
    vs = db.get(VideoScore, items[0]["id"])
    assert vs.base_final is not None
    assert vs.final_score == vs.base_final  # M6 前个性化分为空，回退基础分


# ---------------- R12：rep 优选细化（同档位内更正脸优先） ----------------

def _mk_sample(yaw, quality, pose_class="near"):
    import numpy as np

    from app.services.face_engine import FaceSample
    return FaceSample(
        bbox=(0, 0, 10, 10), det_score=0.9, quality_score=quality,
        female_prob=0.9, timestamp_sec=0.0,
        aligned=np.zeros((112, 112, 3), np.uint8),
        embedding=np.zeros(512, np.float32),
        pose_yaw=yaw, pose_class=pose_class)


def test_sample_rank_prefers_more_frontal_within_class():
    """同为 near：|yaw| 更小（更正脸）者优先，即使质量分更低（R12/ADR-033）。"""
    rank = pipeline._sample_rank
    assert rank(_mk_sample(8.0, 0.5)) < rank(_mk_sample(12.0, 0.9))
    # 姿态档位仍优先于一切：frontal 稳赢 near
    assert rank(_mk_sample(15.0, 0.1, "frontal")) < rank(_mk_sample(0.0, 0.99, "near"))


def test_sample_rank_prefers_clean_frontal_before_quality():
    """干净、正脸、小 yaw 优先于高质量但遮挡样本。"""
    rank = pipeline._sample_rank
    clean = _mk_sample(10.0, 0.40, "frontal")
    clean.occlusion_score = 0.1
    occluded = _mk_sample(2.0, 0.95, "frontal")
    occluded.occlusion_score = 0.9
    assert rank(clean) < rank(occluded)


def test_phase_resample_is_needed_only_for_weak_representative(monkeypatch):
    """单样本、非干净、非正脸或低质量代表才触发半间隔补采样。"""
    from app.config import settings

    monkeypatch.setattr(settings, "min_quality", 0.30)
    strong = [_mk_sample(2.0, 0.80, "frontal"), _mk_sample(3.0, 0.70, "frontal")]
    assert not pipeline._needs_phase_resample([[0, 1]], strong)

    assert pipeline._needs_phase_resample([[0]], strong[:1])
    assert not pipeline._needs_phase_resample(
        [[0, 1]], [_mk_sample(10.0, 0.80, "near"), strong[0]])
    weak_quality = [_mk_sample(1.0, 0.35, "frontal"), strong[0]]
    assert pipeline._needs_phase_resample([[0, 1]], weak_quality)


def test_persist_groups_uses_source_frame_for_thumb(db):
    """缩略图按 rep 样本的 source_frame 取景，而非时间戳近似（R12/ADR-033）。"""
    import numpy as np

    from app.services import thumbs

    good = _mk_sample(0.0, 0.9, "frontal")
    good.source_frame = None  # 无实帧 → 兜底走 frames 近似（旧行为）
    video = Video(path="X:/v/t.mp4", filename="t.mp4", dir_path=".", status="processing")
    db.add(video)
    db.commit()
    n = pipeline._persist_groups(db, video, [good], [[0]], frames=None)
    db.commit()
    assert n == 1
    ident = db.query(FaceIdentity).order_by(FaceIdentity.id.desc()).first()
    # 兜底路径不抛错且缩略图已写（aligned 兜底图）
    assert thumbs.thumb_path(ident.id).is_file()
