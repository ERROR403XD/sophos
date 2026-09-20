"""一次性引导：创建 D:\SophosStress 结构 + 解析源片路径写入 _source.txt。"""
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import STRESS_ROOT, ensure_cap  # noqa: E402

bootstrap_mod = __import__("_common")
bootstrap_mod.bootstrap(STRESS_ROOT / "data_tmp")
from app.services.frame_sampler import locate_ffprobe  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402


def main() -> None:
    for sub in ("clips", "clips_synthetic", "data_query", "data_pipe"):
        (STRESS_ROOT / sub).mkdir(parents=True, exist_ok=True)
    db = Path(__file__).resolve().parents[2] / "data" / "sophos.db"
    eng = create_engine(f"sqlite:///{db.as_posix()}")
    with eng.connect() as conn:
        path = conn.execute(text("SELECT path FROM video WHERE id = 1")).scalar_one()
    r = subprocess.run([str(locate_ffprobe()), "-v", "error",
                        "-show_entries", "format=duration",
                        "-of", "default=nw=1:nk=1", path],
                       capture_output=True, text=True, timeout=120)
    (STRESS_ROOT / "_source.txt").write_text(
        path + "\n" + r.stdout.strip(), encoding="utf-8")
    print("source:", path)
    print("duration:", r.stdout.strip())
    print("root size GB:", round(ensure_cap(STRESS_ROOT) / (1 << 30), 3))


if __name__ == "__main__":
    main()
