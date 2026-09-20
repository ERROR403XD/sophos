"""SQLite 在线备份（M7）：VACUUM INTO 生成一致性快照，不影响运行中的服务。

用法（项目根）：
    python scripts/backup_db.py [输出文件]
缺省输出：data/backups/sophos_YYYYmmdd_HHMMSS.db
"""
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

from app.config import settings  # noqa: E402


def main() -> None:
    src = settings.final_db_path()
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else (
        src.parent / "backups" / f"sophos_{datetime.now():%Y%m%d_%H%M%S}.db")
    out.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(src))
    try:
        con.execute("VACUUM INTO ?", (str(out),))
    finally:
        con.close()
    print(f"backup ok: {out} ({out.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
