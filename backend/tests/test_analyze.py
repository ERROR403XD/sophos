"""R6.3 外部视频分析测试：上传/投放目录 → analyze 任务 → 结果/缩略图 → 隔离语义。

- 隔离语义是核心断言：分析后 video / face / face_identity 表计数不变；
- 无脸视频（testsrc）→ n_faces=0；lena 人脸视频 → ≥1 张带分面容。
"""
import pathlib
import subprocess
import time

import pytest
from sqlalchemy import func, select

from app.db.models import Face, FaceIdentity, Video
from app.services.frame_sampler import locate_ffmpeg

ROOT = pathlib.Path(__file__).resolve().parents[2]
LENA = pathlib.Path(__file__).parent / "assets" / "lena.jpg"
MODELS_OK = (ROOT / "data" / "models" / "det_500m.onnx").is_file()
FFMPEG = locate_ffmpeg() is not None


def _wait_job(client, job_id, timeout=600):
    deadline = time.time() + timeout
    while time.time() < deadline:
        body = client.get(f"/api/jobs/{job_id}").json()
        if body["status"] in ("done", "failed"):
            return body
        time.sleep(0.1)
    raise AssertionError("job timeout")


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


def test_upload_inbox_listing_and_validation(client):
    """上传落盘 + 投放目录列表 + 校验（404/无文件名）。"""
    r = client.post("/api/analyze/upload",
                    files={"file": ("my test clip.mp4", b"0" * 4096, "video/mp4")})
    assert r.status_code == 200
    name = r.json()["filename"]
    assert name.endswith("my test clip.mp4")
    assert r.json()["size_bytes"] == 4096

    items = client.get("/api/analyze/inbox").json()["items"]
    assert any(i["filename"] == name and i["size_bytes"] == 4096 for i in items)

    assert client.post("/api/analyze/start", json={}).status_code == 400
    assert client.post("/api/analyze/start",
                       json={"filename": "nope.mp4"}).status_code == 404
    assert client.get("/api/analyze/deadbeef").status_code == 400
    assert client.get("/api/analyze/123456789012").status_code == 404
    assert client.get("/api/analyze/123456789012/face_0.jpg").status_code == 404


def test_analyze_upload_rejects_unsupported_types_and_oversize(client, monkeypatch):
    from app.config import settings

    assert client.post("/api/analyze/upload", files={
        "file": ("clip.exe", b"x", "application/octet-stream")}).status_code == 415
    assert client.post("/api/analyze/upload", files={
        "file": ("clip.mp4", b"x", "text/plain")}).status_code == 415

    monkeypatch.setattr(settings, "analyze_max_upload_bytes", 8)
    response = client.post("/api/analyze/upload", files={
        "file": ("too-large.mp4", b"123456789", "video/mp4")})
    assert response.status_code == 413
    assert response.json()["code"] == "UPLOAD_TOO_LARGE"
    assert client.get("/api/analyze/inbox").json()["items"] == []


def test_analyze_inbox_path_traversal_and_symlinks_are_rejected(client):
    from app.config import settings

    for filename in ("../outside.mp4", "/outside.mp4", "nested/inside.mp4"):
        response = client.post("/api/analyze/start", json={"filename": filename})
        assert response.status_code == 400
        assert response.json()["code"] == "INVALID_FILENAME"

    inbox = settings.data_dir / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    outside = settings.data_dir / "outside.mp4"
    outside.write_bytes(b"secret")
    (inbox / "linked.mp4").symlink_to(outside)
    symlink_response = client.post("/api/analyze/start",
                                   json={"filename": "linked.mp4"})
    assert symlink_response.status_code == 400
    assert symlink_response.json()["code"] == "INVALID_FILENAME"
    assert client.get("/api/analyze/inbox").json()["items"] == []
    outside.unlink()


def test_analyze_start_params_do_not_expose_absolute_paths(client):
    response = client.post("/api/analyze/upload", files={
        "file": ("clip.mp4", b"x", "video/mp4")})
    assert response.status_code == 200
    start = client.post("/api/analyze/start",
                        json={"filename": response.json()["filename"]})
    assert start.status_code == 202
    assert set(start.json()["job"]["params"]) == {"token", "filename"}


def test_analyze_delete_rejects_symlink_and_malicious_result_paths(client):
    from app.config import settings

    outside_dir = settings.data_dir / "outside-analysis"
    outside_dir.mkdir(parents=True)
    outside_file = settings.data_dir / "outside.mp4"
    outside_file.write_bytes(b"protected")
    analysis = settings.data_dir / "analyze"
    analysis.mkdir(parents=True)
    (analysis / "111111111111").symlink_to(outside_dir)
    (outside_dir / "result.json").write_text(
        '{"token":"111111111111","filename":"linked.mp4"}', encoding="utf-8")
    assert client.get("/api/analyze/111111111111").status_code == 404
    assert client.get("/api/analyze/111111111111/face_0.jpg").status_code == 404
    assert client.delete("/api/analyze/111111111111").status_code == 404
    assert outside_dir.exists()

    malicious = analysis / "222222222222"
    malicious.mkdir()
    (malicious / "result.json").write_text(
        '{"token":"222222222222","filename":"../outside.mp4"}', encoding="utf-8")
    assert client.delete("/api/analyze/222222222222").json() == {"ok": True}
    assert outside_file.exists()


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg not available")
def test_analyze_no_faces_video(client, tmp_path):
    """无人脸视频：任务 done、n_faces=0、final_score=None。"""
    clip = tmp_path / "nf.mp4"
    _make_video(clip)
    r = client.post("/api/analyze/upload", files={"file": ("nf.mp4", clip.read_bytes(),
                                                           "video/mp4")})
    name = r.json()["filename"]
    start = client.post("/api/analyze/start", json={"filename": name}).json()
    body = _wait_job(client, start["job"]["id"])
    assert body["status"] == "done", body.get("error")
    token = body["result"]["token"]
    result = client.get(f"/api/analyze/{token}").json()
    assert result["n_faces"] == 0 and result["faces"] == []
    assert result["final_score"] is None
    assert client.get("/api/analyze/list").json()["items"][0]["token"] == token


@pytest.mark.skipif(not (FFMPEG and MODELS_OK and LENA.is_file()),
                    reason="ffmpeg/models/lena missing")
def test_analyze_lena_isolated_from_library(client, db, tmp_path):
    """lena 视频：出综合分与面容缩略图；主库 video/face/identity 计数零变化。"""
    from app.config import settings

    settings.sample_interval_sec = 1.0
    clip = tmp_path / "lena_ext.mp4"
    _make_video(clip, src_image=LENA)

    counts_before = {
        "video": db.execute(select(func.count()).select_from(Video)).scalar_one(),
        "face": db.execute(select(func.count()).select_from(Face)).scalar_one(),
        "identity": db.execute(select(func.count()).select_from(FaceIdentity)).scalar_one(),
    }

    r = client.post("/api/analyze/upload", files={"file": ("lena_ext.mp4",
                                                           clip.read_bytes(),
                                                           "video/mp4")})
    name = r.json()["filename"]
    start = client.post("/api/analyze/start", json={"filename": name}).json()
    token = start["token"]
    body = _wait_job(client, start["job"]["id"])
    assert body["status"] == "done", body.get("error")

    result = client.get(f"/api/analyze/{token}").json()
    assert result["token"] == token
    assert result["n_faces"] >= 1
    assert result["duration_sec"] is not None
    if (ROOT / "data" / "models" / "beauty_scut.onnx").is_file():
        assert 0.0 <= result["final_score"] <= 100.0
        assert result["max_score"] is not None
    face0 = result["faces"][0]
    assert face0["thumb"] == f"/api/analyze/{token}/face_0.jpg"
    assert face0["n_samples"] >= 1
    thumb = client.get(face0["thumb"])
    assert thumb.status_code == 200
    assert thumb.headers["content-type"].startswith("image/jpeg")

    # 隔离语义：主库三表计数不变，video 表无该文件行
    counts_after = {
        "video": db.execute(select(func.count()).select_from(Video)).scalar_one(),
        "face": db.execute(select(func.count()).select_from(Face)).scalar_one(),
        "identity": db.execute(select(func.count()).select_from(FaceIdentity)).scalar_one(),
    }
    assert counts_after == counts_before
    assert db.execute(select(Video).where(Video.filename == name)).scalars().all() == []

    # 删除：结果与 inbox 源文件一起清掉
    assert client.delete(f"/api/analyze/{token}").json() == {"ok": True}
    assert client.get(f"/api/analyze/{token}").status_code == 404
    assert client.get("/api/analyze/inbox").json()["items"] == []


# ---------------- R11：图片分析 ----------------

def test_upload_image_accepted_and_bad_ext_rejected(client):
    r = client.post("/api/analyze/upload",
                    files={"file": ("photo one.jpg", b"fake", "image/jpeg")})
    assert r.status_code == 200
    items = client.get("/api/analyze/inbox").json()["items"]
    assert any(i["filename"] == r.json()["filename"] and i["kind"] == "image"
               for i in items)
    assert client.post("/api/analyze/upload",
                       files={"file": ("doc.pdf", b"x", "application/pdf")}).status_code == 415
    assert client.post("/api/analyze/upload",
                       files={"file": ("clip.mp4", b"x", "text/plain")}).status_code == 415


@pytest.mark.skipif(not (MODELS_OK and LENA.is_file()),
                    reason="models/lena missing")
def test_analyze_lena_image_isolated(client, db):
    """图片分析（R11）：不依赖 ffmpeg；media_type=image；主库隔离语义同视频。"""
    counts_before = {
        "video": db.execute(select(func.count()).select_from(Video)).scalar_one(),
        "face": db.execute(select(func.count()).select_from(Face)).scalar_one(),
        "identity": db.execute(select(func.count()).select_from(FaceIdentity)).scalar_one(),
    }
    r = client.post("/api/analyze/upload",
                    files={"file": ("lena.jpg", LENA.read_bytes(), "image/jpeg")})
    assert r.status_code == 200
    start = client.post("/api/analyze/start",
                        json={"filename": r.json()["filename"]}).json()
    token = start["token"]
    body = _wait_job(client, start["job"]["id"])
    assert body["status"] == "done", body.get("error")

    result = client.get(f"/api/analyze/{token}").json()
    assert result["media_type"] == "image"
    assert result["n_frames"] == 1
    assert result["duration_sec"] is None
    assert result["n_faces"] >= 1
    if (ROOT / "data" / "models" / "beauty_scut.onnx").is_file():
        assert 0.0 <= result["final_score"] <= 100.0
        assert result["max_score"] is not None
    face0 = result["faces"][0]
    assert face0["thumb"] == f"/api/analyze/{token}/face_0.jpg"
    assert client.get(face0["thumb"]).status_code == 200
    assert client.get("/api/analyze/list").json()["items"][0]["media_type"] == "image"

    counts_after = {
        "video": db.execute(select(func.count()).select_from(Video)).scalar_one(),
        "face": db.execute(select(func.count()).select_from(Face)).scalar_one(),
        "identity": db.execute(select(func.count()).select_from(FaceIdentity)).scalar_one(),
    }
    assert counts_after == counts_before

    # 删除：结果与 inbox 源文件一起清掉
    assert client.delete(f"/api/analyze/{token}").json() == {"ok": True}
    assert client.get(f"/api/analyze/{token}").status_code == 404
    assert client.get("/api/analyze/inbox").json()["items"] == []


@pytest.mark.skipif(not MODELS_OK, reason="models missing")
def test_analyze_corrupt_image_fails_job(client):
    """解码失败的图片：任务 failed 带明确错误，不留半截结果。"""
    r = client.post("/api/analyze/upload",
                    files={"file": ("bad.png", b"not-an-image", "image/png")})
    start = client.post("/api/analyze/start",
                        json={"filename": r.json()["filename"]}).json()
    body = _wait_job(client, start["job"]["id"])
    assert body["status"] == "failed"
    assert "cannot decode image" in (body.get("error") or "")
