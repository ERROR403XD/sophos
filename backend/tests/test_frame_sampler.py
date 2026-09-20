"""抽帧测试（M3）：真实 ffmpeg 生成 testsrc 视频 → 间隔抽帧 → 清理。"""
import subprocess
from pathlib import Path

import pytest

from app.services.frame_sampler import cleanup_frames, locate_ffmpeg, sample_frames


def _make_video(path, seconds=2.0, src="testsrc"):
    exe = locate_ffmpeg()
    if exe is None:
        pytest.skip("ffmpeg not available")
    subprocess.run(
        [str(exe), "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", f"{src}=size=320x240:rate=10:duration={seconds}",
         "-pix_fmt", "yuv420p", str(path)],
        check=True, capture_output=True)


def test_sample_frames_interval(tmp_path):
    v = tmp_path / "t.mp4"
    _make_video(v, seconds=2.0)
    out = tmp_path / "frames"
    frames = sample_frames(v, out, interval_sec=0.5)
    assert len(frames) >= 3
    assert all(f.exists() and f.stat().st_size > 0 for f in frames)
    assert frames == sorted(frames)
    cleanup_frames(out)
    assert not out.exists()


def test_sample_missing_ffmpeg_raises(tmp_path, monkeypatch):
    from app.services import frame_sampler

    monkeypatch.setattr(frame_sampler, "locate_ffmpeg", lambda *a, **k: None)
    with pytest.raises(RuntimeError, match="ffmpeg not found"):
        frame_sampler.sample_frames(tmp_path / "x.mp4", tmp_path / "o", 1.0)


def test_sample_corrupt_video_raises(tmp_path):
    if locate_ffmpeg() is None:
        pytest.skip("ffmpeg not available")
    bad = tmp_path / "bad.mp4"
    bad.write_bytes(b"not a video" * 100)
    with pytest.raises(RuntimeError):
        sample_frames(bad, tmp_path / "frames", 1.0)
