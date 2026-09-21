"""R1 真实片源验证（P1/P2/P3）：HIMYM E01 处理 → 裁决/质量门/聚类/遮挡落库统计。

运行：backend/.venv/Scripts/python.exe -X utf8 scripts/validate_real_data.py [视频路径]
数据目录：临时目录（脚本结束时打印路径）
"""
import json
import sys
import tempfile
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import numpy as np  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.config import settings  # noqa: E402
from app.db import session as db_session_mod  # noqa: E402
from app.db.models import Face, FaceIdentity, Video  # noqa: E402
from app.db.session import init_engine  # noqa: E402
from app.services import pipeline, scanner  # noqa: E402

VIDEO = sys.argv[1] if len(sys.argv) > 1 else str(
    next(Path("Z:/<sample-video-dir>").glob("**/*S01E01*.mkv")))
INTERVAL = 5.0


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="sophos_r1_validate_"))
    settings.data_dir = tmp
    settings.db_path = tmp / "sophos.db"
    settings.models_dir = ROOT / "data" / "models"
    settings.sample_interval_sec = INTERVAL
    init_engine(settings)

    with db_session_mod.SessionLocal() as db:
        report = scanner.scan_all([str(Path(VIDEO).parent)], db)
        print(f"scan: {report.as_dict()}")

        video = db.execute(
            select(Video).where(Video.filename.contains("S01E01"))).scalars().one()
        print(f"video#{video.id}: vcodec={video.vcodec} acodec={video.acodec} "
              f"size={video.size_bytes / 1e9:.2f}GB")

        info = pipeline._process_video(db, pipeline.get_face_engine(), video)
        print(f"process: {json.dumps(info)}")

        idents = db.execute(select(FaceIdentity)
                            .where(FaceIdentity.video_id == video.id)).scalars().all()
        faces = db.execute(select(Face).where(Face.video_id == video.id)).scalars().all()
        print(f"\n== 结果 ==\nidentities={len(idents)} faces={len(faces)}")
        for i in sorted(idents, key=lambda x: -x.n_samples):
            rep = db.get(Face, i.rep_face_id)
            fp = f"{i.female_prob_mean:.2f}" if i.female_prob_mean is not None else "-"
            print(f"  id={i.id} n={i.n_samples} fp_mean={fp} "
                  f"occluded={i.occluded}({i.occluded_source}) "
                  f"rep[pose={rep.pose_class} q={rep.quality_score:.2f} "
                  f"occ={rep.occlusion_score}] emb={'Y' if i.mean_embedding else 'N'}")

        # 字段分布（全样本）
        print("\npose_class 分布:", Counter(f.pose_class for f in faces))
        occ = [f.occlusion_score for f in faces if f.occlusion_score is not None]
        print(f"occlusion_score: n={len(occ)} min={min(occ):.2f} "
              f"max={max(occ):.2f} mean={np.mean(occ):.2f} ≥0.6: {sum(o >= 0.6 for o in occ)}")
        fp = [f.female_prob for f in faces]
        print(f"female_prob(样本级): min={min(fp):.2f} max={max(fp):.2f} "
              f"mean={np.mean(fp):.2f} <0.5: {sum(v < 0.5 for v in fp)}/{len(fp)}")
        qs = [f.quality_score for f in faces]
        print(f"quality: min={min(qs):.2f} mean={np.mean(qs):.2f} (门 0.15 已生效)")
        thumb_ok = sum((Path(settings.data_dir) / "thumbs" / f"{i.id}.jpg").is_file()
                       for i in idents)
        print(f"thumbs: {thumb_ok}/{len(idents)}")
        print(f"data dir: {tmp}")


if __name__ == "__main__":
    main()
