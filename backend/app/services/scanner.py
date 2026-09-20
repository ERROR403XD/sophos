"""视频文件扫描器（M2 实现）。

职责（docs/ARCHITECTURE.md §2.1）：
- 遍历工作目录（支持多个、递归，跳过隐藏目录）
- 按扩展名识别视频（VIDEO_EXTS）
- 增量语义（path 为主键，resolve 规范化）：
  新增 → INSERT status=pending
  存在但 size/mtime 变化 → 置 pending 重处理
  DB 有记录但磁盘消失 → status=missing（不删行，保留分数历史）
  之前 missing 的文件重新出现 → 回 pending（无面容时）或 done
- 只维护路径，不复制/移动文件

幂等：重复扫描无副作用。进度回调按根目录粒度（job 进度展示用）。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Video
from app.services.streamer import probe_codec

VIDEO_EXTS = {
    ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm",
    ".m4v", ".ts", ".mpg", ".mpeg", ".rmvb", ".3gp",
}


@dataclass
class ScanReport:
    added: int = 0
    changed: int = 0
    missing: int = 0
    unchanged: int = 0

    def as_dict(self) -> dict:
        return {
            "added": self.added, "changed": self.changed,
            "missing": self.missing, "unchanged": self.unchanged,
        }


def find_video_files(root: str | Path) -> list[Path]:
    root_path = Path(root)
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root_path):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in filenames:
            if Path(name).suffix.lower() in VIDEO_EXTS:
                found.append(Path(dirpath) / name)
    return found


def _norm(path: Path) -> str:
    return str(path.resolve())


def _scan_root(root: str, session: Session, existing: dict[str, Video],
               seen: set[str], report: ScanReport) -> None:
    root_path = Path(root)
    if not root_path.is_dir():
        return
    for f in find_video_files(root_path):
        p = _norm(f)
        if p in seen:  # 同一文件被多个根目录覆盖时只处理一次
            continue
        seen.add(p)
        st = f.stat()
        row = existing.get(p)
        if row is None:
            row = Video(path=p, filename=f.name, dir_path=_norm(root_path),
                        size_bytes=st.st_size, mtime=st.st_mtime, status="pending")
            # R1(P4)：落库即探测编码（同步，单条 <100ms；失败留空播放期再试）
            row.vcodec, row.acodec = probe_codec(str(f))
            session.add(row)
            existing[p] = row
            report.added += 1
        elif row.size_bytes != st.st_size or (row.mtime or 0.0) != st.st_mtime:
            row.size_bytes = st.st_size
            row.mtime = st.st_mtime
            row.status = "pending"
            row.status_msg = None
            row.vcodec, row.acodec = probe_codec(str(f))  # 文件已变化，重探测
            report.changed += 1
        elif row.status == "missing":
            # 文件重新出现且未变化：有面容结果则恢复 done，否则回 pending
            row.status = "done" if row.identity_count else "pending"
            row.status_msg = None
            report.unchanged += 1
        else:
            report.unchanged += 1


def _mark_missing(session: Session, seen: set[str], report: ScanReport) -> None:
    rows = session.execute(select(Video).where(Video.status != "missing")).scalars().all()
    for row in rows:
        if row.path in seen:
            continue
        if not Path(row.path).is_file():
            row.status = "missing"
            row.status_msg = "file not found on disk"
            report.missing += 1


def scan_all(work_dirs: list[str], session: Session,
             progress_cb: Callable[[int, int], None] | None = None,
             check_cb: Callable[[], None] | None = None) -> ScanReport:
    """扫描全部工作目录；每个根目录提交一次，便于 job 进度展示。

    check_cb（R5）：每个根目录开始前调用一次，供 job 层做暂停/取消检查点
    （抛出 JobInterrupted 即中断扫描；已完成目录的增量结果已提交，幂等）。
    """
    report = ScanReport()
    existing = {v.path: v for v in session.execute(select(Video)).scalars()}
    seen: set[str] = set()
    total = max(1, len(work_dirs))
    for i, root in enumerate(work_dirs, start=1):
        if check_cb:
            check_cb()
        _scan_root(root, session, existing, seen, report)
        session.commit()
        if progress_cb:
            progress_cb(i, total)
    _mark_missing(session, seen, report)
    session.commit()
    return report
