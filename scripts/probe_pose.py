"""姿态启发式定标探针（R1/P2）：真实片源人工核对 yaw/pitch/遮挡估计。

用法：
  python scripts/probe_pose.py <video_or_image> [--every N] [--out DIR]

对视频按 --every 秒抽帧（复用 ffmpeg），每张检出人脸打印：
  yaw/pitch/pose_class/occlusion/quality/female_prob + bbox，
并将对齐 112 图带标注存入 --out（默认 data/tmp/pose_probe），
人工比对"目测角度 vs 估计角度"后调整 face_engine.YAW_AMP / 阈值
（SOPHOS_MAX_YAW_DEG / SOPHOS_MAX_PITCH_DEG，见 docs/PLAN_v1.0.1.md §2.2）。
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import cv2  # noqa: E402
import numpy as np  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
MODELS = ROOT / "data" / "models"


def locate_ffprobe() -> str | None:
    for exe in ("ffprobe", "ffprobe.exe"):
        found = shutil.which(exe)
        if found:
            return found
    for cand in sorted(ROOT.glob("tools/ffmpeg*/bin/ffprobe.exe")):
        return str(cand)
    return None


def locate_ffmpeg() -> str | None:
    for exe in ("ffmpeg", "ffmpeg.exe"):
        found = shutil.which(exe)
        if found:
            return found
    for cand in sorted(ROOT.glob("tools/ffmpeg*/bin/ffmpeg.exe")):
        return str(cand)
    return None


def iter_frames(video: Path, every_sec: float, tmp: Path) -> list[Path]:
    exe = locate_ffmpeg()
    if exe is None:
        raise SystemExit("ffmpeg not found (PATH or tools/ffmpeg*/bin)")
    tmp.mkdir(parents=True, exist_ok=True)
    cmd = [exe, "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
           "-i", str(video), "-vf", f"fps=1/{every_sec:g}", "-q:v", "2",
           str(tmp / "f%06d.jpg")]
    try:
        subprocess.run(cmd, check=True, capture_output=True)
    except subprocess.CalledProcessError as exc:  # stderr 带出（编码/路径问题易隐蔽）
        raise SystemExit(f"ffmpeg failed: {exc.stderr.decode('utf-8', 'replace')[-500:]}")
    return sorted(tmp.glob("f*.jpg"))


def load_bgr(path: Path) -> np.ndarray | None:
    data = np.fromfile(str(path), dtype=np.uint8)  # Unicode 路径安全
    return cv2.imdecode(data, cv2.IMREAD_COLOR)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("src", nargs="?", default=None,
                    help="视频或图片路径；缺省自动定位 D:/test_videos 下第一个 mkv"
                         "（避免 shell 传参把全角字符弄坏）")
    ap.add_argument("--every", type=float, default=5.0, help="抽帧间隔秒（视频）")
    ap.add_argument("--out", default=str(ROOT / "data" / "tmp" / "pose_probe"),
                    help="标注图输出目录")
    args = ap.parse_args()

    if args.src is None:
        candidates = sorted(Path("D:/test_videos").glob("**/*S01E01*.mkv"))
        if not candidates:
            raise SystemExit("no default video found under D:/test_videos; pass src")
        src = candidates[0]
    else:
        src = Path(args.src)
    print("source:", src)

    if not (MODELS / "det_500m.onnx").is_file():
        raise SystemExit("models missing (run scripts/download_models.py)")

    from app.services.face_engine import FaceEngine  # noqa: E402

    engine = FaceEngine(models_dir=MODELS)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    frames: list[Path]
    if src.suffix.lower() in {".jpg", ".jpeg", ".png", ".bmp", ".webp"}:
        frames = [src]
    else:
        with tempfile.TemporaryDirectory() as td:
            frames_all = iter_frames(src, args.every, Path(td))
            frames = []
            for f in frames_all:  # 拷出临时目录再处理（Windows 下 TemporaryDirectory 占用句柄）
                dst = out_dir / f.name
                shutil.copy2(f, dst)
                frames.append(dst)

    print(f"{'frame':>10s} {'yaw':>7s} {'pitch':>7s} {'class':>7s} "
          f"{'occl':>6s} {'qual':>6s} {'female':>6s}  bbox")
    n_faces = 0
    for f in frames:
        img = load_bgr(f)
        if img is None:
            continue
        for det in engine.detect(img):
            from app.services.face_engine import estimate_pose, occlusion_score, quality_score
            yaw, pitch, pose_class = estimate_pose(det.kps)
            aligned = engine.align(img, det.kps)
            occ = occlusion_score(aligned)
            qual = quality_score(img, det.bbox)
            fp = engine.female_prob(aligned)
            x1, y1, x2, y2 = [int(v) for v in det.bbox]
            crop = img[max(0, y1):y2, max(0, x1):x2]
            if crop.size:
                tag = f"{f.stem}_y{yaw:+.0f}_p{pitch:+.0f}_{pose_class}_o{occ:.2f}.jpg"
                ok, buf = cv2.imencode(".jpg", crop)
                if ok:
                    buf.tofile(str(out_dir / tag))
            n_faces += 1
            print(f"{f.stem:>10s} {yaw:7.1f} {pitch:7.1f} {pose_class:>7s} "
                  f"{occ:6.2f} {qual:6.2f} {fp:6.2f}  "
                  f"({x1},{y1})-({x2},{y2})")
    print(f"\n{n_faces} faces; annotated crops -> {out_dir}")
    print("人工核对：目测角 vs 估计角，偏差大则调 YAW_AMP（face_engine.py）"
          "或 SOPHOS_MAX_YAW/PITCH_DEG")


if __name__ == "__main__":
    main()
