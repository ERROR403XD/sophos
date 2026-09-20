"""scanner 增量语义测试（M2）：新增/无变化/变更/消失/复现。"""
import os
import time

from sqlalchemy import select

from app.db.models import Video
from app.services.scanner import scan_all


def _mkfile(p, size=100):
    p.write_bytes(b"0" * size)


def _rows(db):
    return {v.filename: v for v in db.execute(select(Video)).scalars()}


def test_scan_incremental(db, tmp_path):
    root = tmp_path / "videos"
    sub = root / "sub"
    sub.mkdir(parents=True)
    _mkfile(root / "a.mp4", 100)
    _mkfile(root / "notes.txt", 10)  # 非视频扩展名，忽略
    _mkfile(sub / "b.webm", 200)     # 递归子目录

    r = scan_all([str(root)], db)
    assert (r.added, r.changed, r.missing, r.unchanged) == (2, 0, 0, 0)
    rows = _rows(db)
    assert set(rows) == {"a.mp4", "b.webm"}
    assert all(v.status == "pending" for v in rows.values())
    assert rows["a.mp4"].size_bytes == 100

    # 无变化 → unchanged
    r2 = scan_all([str(root)], db)
    assert (r2.added, r2.changed, r2.missing, r2.unchanged) == (0, 0, 0, 2)

    # size+mtime 变化 → changed，状态回 pending 重处理
    f = root / "a.mp4"
    _mkfile(f, 123)
    os.utime(f, (time.time() + 10, time.time() + 10))
    r3 = scan_all([str(root)], db)
    assert (r3.added, r3.changed) == (0, 1)
    db.expire_all()
    assert _rows(db)["a.mp4"].status == "pending"

    # 磁盘消失 → missing（保留行，不删）
    (sub / "b.webm").unlink()
    r4 = scan_all([str(root)], db)
    assert r4.missing == 1
    db.expire_all()
    assert _rows(db)["b.webm"].status == "missing"

    # 文件重新出现（size 已变）→ changed，回 pending
    _mkfile(sub / "b.webm", 300)
    r5 = scan_all([str(root)], db)
    assert r5.changed == 1
    db.expire_all()
    assert _rows(db)["b.webm"].status == "pending"


def test_scan_skips_hidden_dirs(db, tmp_path):
    root = tmp_path / "v"
    hidden = root / ".hidden"
    hidden.mkdir(parents=True)
    _mkfile(root / "a.mp4")
    _mkfile(hidden / "b.mp4")

    r = scan_all([str(root)], db)
    assert r.added == 1
    rows = _rows(db)
    assert set(rows) == {"a.mp4"}
