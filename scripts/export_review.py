"""R2 审计工具：处理单集 → 导出全部 identity rep 裁剪 + 接触表（供视觉审核）。

运行：backend/.venv/Scripts/python.exe -X utf8 scripts/export_review.py [视频] [--every 5] [--out DIR]
输出：out/rep_{id:02d}_fp{均值}_pose_q质量_n样本.jpg + contact_sheet.jpg（网格拼图）
"""
import argparse
import math
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import cv2  # noqa: E402
import numpy as np  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.config import settings  # noqa: E402
from app.db import session as db_session_mod  # noqa: E402
from app.db.models import Face, FaceIdentity, Video  # noqa: E402
from app.db.session import init_engine  # noqa: E402
from app.services import pipeline, scanner  # noqa: E402


def crop_bgr(img_path: Path, bbox, out: Path, height: int = 144) -> None:
    data = np.fromfile(str(img_path), dtype=np.uint8)
    img = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if img is None:
        return
    h, w = img.shape[:2]
    x1, y1, x2, y2 = [int(round(v)) for v in bbox]
    x1, y1 = max(0, x1), max(0, y1)
    x2, y2 = min(w, x2), min(h, y2)
    face = img[y1:y2, x1:x2]
    if face.size == 0:
        return
    scale = height / face.shape[0]
    face = cv2.resize(face, (max(1, int(face.shape[1] * scale)), height))
    ok, buf = cv2.imencode(".jpg", face, [cv2.IMWRITE_JPEG_QUALITY, 88])
    if ok:
        buf.tofile(str(out))


def sheet(paths: list[Path], out: Path, cols: int = 6, cell_w: int = 150,
          cell_h: int = 168) -> None:
    """接触表：每格上方 24px 写 id 标签（文件名前缀）。"""
    from PIL import Image, ImageDraw  # opcdf 无字体绘制中文不便，用 PIL；无 PIL 则退化为纯图
    rows = math.ceil(len(paths) / cols)
    board = Image.new("RGB", (cols * cell_w, rows * cell_h), (24, 26, 30))
    draw = ImageDraw.Draw(board)
    for i, p in enumerate(paths):
        r, c = divmod(i, cols)
        try:
            im = Image.open(p)
        except OSError:
            continue
        im.thumbnail((cell_w - 4, cell_h - 28))
        x = c * cell_w + (cell_w - im.width) // 2
        y = r * cell_h + 24
        board.paste(im, (x, y))
        tag = p.stem.split("_")[1] if "_" in p.stem else p.stem[:10]
        draw.text((c * cell_w + 6, r * cell_h + 5), tag, fill=(240, 240, 240))
    board.save(out)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("src", nargs="?", default=None)
    ap.add_argument("--every", type=float, default=5.0)
    ap.add_argument("--out", default=str(ROOT / "data" / "tmp" / "review_e01"))
    ap.add_argument("--skip-process", action="store_true", help="复用已有数据目录")
    args = ap.parse_args()

    src = (Path(args.src) if args.src
           else sorted(Path("D:/test_videos").glob("**/*S01E01*.mkv"))[0])

    tmp = Path(args.out)
    tmp.mkdir(parents=True, exist_ok=True)
    settings.data_dir = tmp
    settings.db_path = tmp / "sophos.db"
    settings.models_dir = ROOT / "data" / "models"
    settings.sample_interval_sec = args.every
    init_engine(settings)

    with db_session_mod.SessionLocal() as db:
        if not args.skip_process:
            scanner.scan_all([str(src.parent)], db)
            video = db.execute(
                select(Video).where(Video.filename.contains("S01E01"))).scalars().one()
            info = pipeline._process_video(db, pipeline.get_face_engine(), video)
            print("process:", info)

        # 需要帧图取 bbox 裁剪：重抽帧一次（快）
        video = db.execute(
            select(Video).where(Video.filename.contains("S01E01"))).scalars().one()
        frames_dir = tmp / "review_frames"
        from app.services import frame_sampler
        frames = frame_sampler.sample_frames(
            video.path, frames_dir, interval_sec=args.every,
            ffmpeg_exe=settings.ffmpeg_exe)

        rows = db.execute(
            select(FaceIdentity, Face)
            .outerjoin(Face, Face.id == FaceIdentity.rep_face_id)
            .where(FaceIdentity.video_id == video.id)
            .order_by(FaceIdentity.id)).all()
        # 帧序号 × 间隔 = 近似时间戳（与 frame_sampler 约定一致）
        frame_by_ts = {round(i * args.every, 3): f
                       for i, f in enumerate(sorted(frames))}

        reps = []
        for ident, rep in rows:
            ts = rep.timestamp_sec or 0.0
            frame_path = frame_by_ts.get(round(ts, 3)) or frames[min(
                int(ts / args.every), len(frames) - 1)]
            fp = f"{ident.female_prob_mean:.2f}" if ident.female_prob_mean is not None else "-"
            name = (f"rep_{ident.id:02d}_fp{fp}_{rep.pose_class or '?'}"
                    f"_q{rep.quality_score:.2f}_n{ident.n_samples}"
                    f"_occ{rep.occlusion_score:.2f}_{ts:.0f}s.jpg")
            out = tmp / name
            crop_bgr(frame_path, (rep.bbox_x1, rep.bbox_y1, rep.bbox_x2, rep.bbox_y2),
                     out, height=144)
            reps.append(out)
            print(f"id={ident.id:02d} fp={fp} pose={rep.pose_class} "
                  f"q={rep.quality_score:.2f} n={ident.n_samples} "
                  f"occ={rep.occlusion_score:.2f} t={ts:.0f}s -> {name}")

        try:
            sheet(reps, tmp / "contact_sheet.jpg")
            print("sheet:", tmp / "contact_sheet.jpg")
        except ImportError:
            print("(PIL 不可用，跳过接触表；逐张看 rep_*.jpg)")
        finally:
            frame_sampler.cleanup_frames(frames_dir)


if __name__ == "__main__":
    main()
