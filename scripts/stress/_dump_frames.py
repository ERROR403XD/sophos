"""抽帧目检：导出 3 个切片的中间帧（诊断无人脸原因）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import bootstrap  # noqa: E402

bootstrap(r"<stress-data>\data_tmp", r"<project-root>\data\models")
from app.config import settings  # noqa: E402
from app.db.session import init_engine  # noqa: E402

init_engine(settings)
import cv2  # noqa: E402
import numpy as np  # noqa: E402

from app.services.frame_sampler import sample_frames  # noqa: E402

out = Path(r"<stress-data>\probe")
out.mkdir(parents=True, exist_ok=True)
for clip_no in range(3):
    clip = rf"<stress-data>\clips\stress_clip_{clip_no:04d}.mkv"
    frames = sample_frames(clip, out, interval_sec=1.0,
                           ffmpeg_exe=settings.ffmpeg_exe)
    for i, f in enumerate(frames):
        data = np.fromfile(str(f), dtype=np.uint8)
        img = cv2.imdecode(data, cv2.IMREAD_COLOR)
        dst = out / f"clip{clip_no}_f{i}.jpg"
        cv2.imencode(".jpg", img)[1].tofile(str(dst))
    print(clip, "->", len(frames), "frames dumped")
