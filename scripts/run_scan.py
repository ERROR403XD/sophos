"""命令行触发一次增量扫描（M2，调试用）。

用法（在项目根或 backend 目录运行）：
    python scripts/run_scan.py [目录1 目录2 ...]
不传目录时使用 kv_setting.work_dirs（WebUI/API 配置过或 SOPHOS_WORK_DIRS 播种过）。
注意相对路径 data_dir 基于当前目录，建议设置 SOPHOS_DATA_DIR 或从 backend 目录运行。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.config import settings  # noqa: E402
from app.db import session as db_session_mod  # noqa: E402
from app.db.session import init_engine  # noqa: E402
from app.services.kv import get_work_dirs  # noqa: E402
from app.services.scanner import scan_all  # noqa: E402


def main() -> None:
    init_engine(settings)
    with db_session_mod.SessionLocal() as session:
        dirs = sys.argv[1:] or get_work_dirs(session)
        if not dirs:
            print("no workdirs configured (add via API or SOPHOS_WORK_DIRS)")
            return
        report = scan_all(dirs, session)
        print(report.as_dict())


if __name__ == "__main__":
    main()
