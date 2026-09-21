"""压测工装 S1a：从本地测试视频批量生成**可重复**的低质量短切片。

用途（PLAN_v1.1.0 后续压测，STRESS_LOG）：为真实流水线压测（scan/process/
分批链/暂停取消）提供批量片源。生成完全确定性（按 index 推导切片起点），
且幂等（已存在的输出跳过）——可随时重跑/扩量。

用法（backend/ 目录运行）：
  python ../scripts/stress/gen_clips.py --source <视频路径> \
      --out D:/<stress-data>/clips --count 120 --clip-sec 6
  python ../scripts/stress/gen_clips.py --synthetic --out D:/<stress-data>/clips \
      --count 500          # testsrc 合成片（无人脸），用于纯量级扫描
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from app.services.frame_sampler import locate_ffmpeg, locate_ffprobe  # noqa: E402
from _common import STRESS_ROOT, ensure_cap  # noqa: E402


def probe_duration(ffprobe: str, src: str) -> float:
    r = subprocess.run([ffprobe, "-v", "error", "-show_entries", "format=duration",
                        "-of", "default=nw=1:nk=1", src],
                       capture_output=True, text=True, timeout=120)
    return float(r.stdout.strip())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default="auto",
                    help="视频路径；auto = 读 <stress-data>/_source.txt 第 1 行")
    ap.add_argument("--synthetic", action="store_true", help="testsrc 合成片（无人脸）")
    ap.add_argument("--out", required=True)
    ap.add_argument("--count", type=int, default=120)
    ap.add_argument("--clip-sec", type=float, default=6.0)
    ap.add_argument("--width", type=int, default=640)
    ap.add_argument("--fps", type=int, default=12)
    ap.add_argument("--crf", type=int, default=35, help="低质量：crf 越大越糊")
    ap.add_argument("--seed", type=int, default=20260918)
    args = ap.parse_args()

    ffmpeg, ffprobe = locate_ffmpeg(), locate_ffprobe()
    assert ffmpeg and ffprobe, "ffmpeg/ffprobe not found"
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    ensure_cap(STRESS_ROOT)  # 1TB 上限守卫（写入前 + 写入后各校验一次）

    duration = 3600.0
    if not args.synthetic:
        src = args.source
        if src == "auto":
            meta = STRESS_ROOT / "_source.txt"
            src = meta.read_text(encoding="utf-8").splitlines()[0].strip()
        assert src, "--source required (or --synthetic)"
        duration = probe_duration(str(ffprobe), src)
        print(f"source: {src}\nsource duration: {duration:.1f}s")
        args.source = src
    # 切片起点确定性推导：均匀铺开并加 seed 相位偏移（可重复）
    usable = max(duration - args.clip_sec - 1.0, args.clip_sec)
    stride = max(args.clip_sec * 2.0, usable / max(1, args.count))

    t0 = time.time()
    made = skipped = 0
    for i in range(args.count):
        out_path = out / f"stress_clip_{i:04d}.mkv"
        if out_path.is_file() and out_path.stat().st_size > 0:
            skipped += 1
            continue
        if args.synthetic:
            cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                   "-f", "lavfi", "-i",
                   f"testsrc=size=320x240:rate={args.fps}:duration={args.clip_sec}",
                   "-c:v", "libx264", "-preset", "ultrafast", "-crf", str(args.crf),
                   str(out_path)]
        else:
            start = (args.clip_sec + i * stride + (args.seed % 7)) % usable
            cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                   "-ss", f"{start:.2f}", "-i", args.source, "-t", str(args.clip_sec),
                   "-vf", f"scale={args.width}:-2", "-r", str(args.fps),
                   "-c:v", "libx264", "-preset", "veryfast", "-crf", str(args.crf),
                   "-c:a", "aac", "-b:a", "48k", "-ac", "1",
                   str(out_path)]
        subprocess.run(cmd, check=True, capture_output=True, timeout=600)
        made += 1
        if made % 20 == 0:
            print(f"  {made + skipped}/{args.count} clips ({time.time() - t0:.0f}s)")
    print(f"done: made={made} skipped={skipped} elapsed={time.time() - t0:.0f}s "
          f"dir_size={ensure_cap(STRESS_ROOT) / (1 << 30):.2f} GB")


if __name__ == "__main__":
    main()
