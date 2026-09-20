"""ffmpeg 抽帧（M3 实现）。

职责（docs/ARCHITECTURE.md §2.1）：
- 对 video.path 按固定间隔抽帧（SOPHOS_SAMPLE_INTERVAL_SEC，默认 2s）
- 输出 data/frames/{video_id}/f%06d.jpg；该视频处理完成后整目录清理
- subprocess 调 ffmpeg：定位顺序 = 配置显式路径 > PATH > 项目 tools/ffmpeg*/bin
- 可选扩展：场景切换检测抽帧（ARCHITECTURE §6，本期不做）
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def locate_ffmpeg(explicit: str = "") -> Path | None:
    """定位 ffmpeg.exe：显式配置 > PATH > tools/ffmpeg*/bin（绿色版约定）。"""
    if explicit:
        p = Path(explicit)
        if p.is_file():
            return p
        return None
    found = shutil.which("ffmpeg")
    if found:
        return Path(found)
    for candidate in sorted(PROJECT_ROOT.glob("tools/ffmpeg*/bin/ffmpeg.exe")):
        return candidate
    return None


def locate_ffprobe() -> Path | None:
    """定位 ffprobe.exe（R1/P4 播放分档用）：与 locate_ffmpeg 同模式。"""
    found = shutil.which("ffprobe")
    if found:
        return Path(found)
    for candidate in sorted(PROJECT_ROOT.glob("tools/ffmpeg*/bin/ffprobe.exe")):
        return candidate
    return None


def sample_frames(video_path: str | Path, out_dir: str | Path,
                  interval_sec: float = 2.0, ffmpeg_exe: str = "") -> list[Path]:
    """按固定秒数间隔抽帧，返回按帧号排序的 jpg 路径列表；失败抛 RuntimeError。"""
    exe = locate_ffmpeg(ffmpeg_exe)
    if exe is None:
        raise RuntimeError(
            "ffmpeg not found (set SOPHOS_FFMPEG_EXE, install to PATH, "
            "or put ffmpeg under tools/ffmpeg-*/bin)")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    pattern = out_dir / "f%06d.jpg"
    cmd = [str(exe), "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
           "-i", str(video_path),
           "-vf", f"fps=1/{max(0.1, interval_sec):g}",
           "-q:v", "2", str(pattern)]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed (code {proc.returncode}): {proc.stderr[-500:]}")
    frames = sorted(out_dir.glob("f*.jpg"))
    if not frames:
        raise RuntimeError("ffmpeg produced no frames (corrupt video or unsupported codec?)")
    return frames


def cleanup_frames(out_dir: str | Path) -> None:
    """处理完成后清理抽帧临时目录。"""
    out_dir = Path(out_dir)
    if out_dir.is_dir():
        shutil.rmtree(out_dir, ignore_errors=True)
