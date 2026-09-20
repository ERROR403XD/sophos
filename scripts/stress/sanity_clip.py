"""小样检测验证：确认低质切片仍能检出女性面容（S3 前置门）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import bootstrap  # noqa: E402

bootstrap(r"D:\SophosStress\data_tmp", r"data\models")
from app.config import settings  # noqa: E402
from app.db.session import init_engine  # noqa: E402

init_engine(settings)
import cv2  # noqa: E402
import numpy as np  # noqa: E402

from app.services.face_engine import FaceEngine  # noqa: E402
from app.services.frame_sampler import (cleanup_frames,  # noqa: E402
                                        sample_frames)

eng = FaceEngine(models_dir=settings.final_models_dir(),
                 det_name=settings.model_det, emb_name=settings.model_emb,
                 gender_name=settings.model_gender)
probe_dir = Path(r"D:\SophosStress\data_tmp\frames_probe")
for clip_no in range(3):
    clip = rf"D:\SophosStress\clips\stress_clip_{clip_no:04d}.mkv"
    frames = sample_frames(clip, probe_dir, interval_sec=2.0,
                           ffmpeg_exe=settings.ffmpeg_exe)
    total = female = 0
    for f in frames:
        data = np.fromfile(str(f), dtype=np.uint8)
        img = cv2.imdecode(data, cv2.IMREAD_COLOR)
        ss = eng.process_frame(img, timestamp_sec=0, det_thresh=0.5,
                               min_face=80, min_quality=0.30,
                               max_yaw_deg=80, max_pitch_deg=40,
                               clip_profile_max=0.95)
        total += len(ss)
        female += sum(1 for s in ss if s.female_prob >= 0.5)
    print(f"{Path(clip).name}: frames={len(frames)} samples={total} female={female}")
    cleanup_frames(probe_dir)
