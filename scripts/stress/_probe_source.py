"""压测前置探测：确认 ffmpeg/ffprobe 可用 + 源片时长（输出给 gen_clips 用）。"""
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))
from app.services.frame_sampler import locate_ffmpeg, locate_ffprobe  # noqa: E402

from sqlalchemy import create_engine, text  # noqa: E402


def main() -> None:
    print("ffmpeg:", locate_ffmpeg())
    print("ffprobe:", locate_ffprobe())
    db = Path(__file__).resolve().parents[2] / "data" / "sophos.db"
    eng = create_engine(f"sqlite:///{db.as_posix()}")
    with eng.connect() as conn:
        rows = conn.execute(text(
            "SELECT id, path, duration_sec FROM video ORDER BY id")).all()
    for vid, path, dur in rows[:5]:
        print("db video:", vid, dur, Path(path).name[:40])
    target = rows[0][1]
    exe = locate_ffprobe()
    r = subprocess.run([str(exe), "-v", "error", "-show_entries", "format=duration",
                        "-of", "default=nw=1:nk=1", target],
                       capture_output=True, text=True, timeout=120)
    print("probe duration:", r.stdout.strip(), r.stderr[:200])


if __name__ == "__main__":
    main()
