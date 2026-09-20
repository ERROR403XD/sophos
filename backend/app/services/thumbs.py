"""缩略图存储（R5 大规模改造，ADR-024）。

原实现：data/thumbs/{id}.jpg 单层平铺（StaticFiles 托管）。数万视频 =>
数十万张缩略图，平铺目录在 NTFS/SMB 上检索明显变慢（打分页图片出现缓慢、
目录遍历困难）。

改造：
- **分片存储**：thumbs/{id//1000:03d}/{id}.jpg，单目录 ≤1000 文件；
- **动态端点**（api/thumbs.py）：按 id 计算分片路径返回，带
  Cache-Control（配合 ?v={updated_at} 内容版本参数实现长缓存）；
- **旧库兼容**：find_thumb 先查分片路径，回落旧平铺路径（存量文件无需
  迁移即可继续服务；重处理后自然落入分片路径）。
"""
from __future__ import annotations

import logging
from pathlib import Path

import cv2
import numpy as np

log = logging.getLogger("sophos.thumbs")

SHARD_SIZE = 1000  # 每分片目录文件数上限


def thumbs_root() -> Path:
    from app.config import settings

    return Path(settings.data_dir) / "thumbs"


def shard_rel(identity_id: int) -> str:
    """分片子目录名（三位数字符串，如 '012'）。"""
    return f"{max(0, identity_id) // SHARD_SIZE:03d}"


def thumb_path(identity_id: int) -> Path:
    """新写入的标准路径（分片）。"""
    return thumbs_root() / shard_rel(identity_id) / f"{identity_id}.jpg"


def find_thumb(identity_id: int) -> Path | None:
    """定位缩略图：分片路径优先，旧平铺路径兜底（存量部署兼容）。"""
    p = thumb_path(identity_id)
    if p.is_file():
        return p
    legacy = thumbs_root() / f"{identity_id}.jpg"
    return legacy if legacy.is_file() else None


def save_thumb(identity_id: int, img_bgr: np.ndarray, quality: int = 82) -> bool:
    """编码并写入分片路径（目录按需创建）；失败返回 False 不阻塞流水线。"""
    d = thumb_path(identity_id).parent
    d.mkdir(parents=True, exist_ok=True)
    ok, buf = cv2.imencode(".jpg", img_bgr, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        return False
    buf.tofile(str(thumb_path(identity_id)))  # Unicode 安全写入
    return True


def remove_thumb(identity_id: int) -> None:
    """删除缩略图（新旧路径都尝试；静默，缩略图缺失不视为错误）。"""
    for p in (thumb_path(identity_id),
              thumbs_root() / f"{identity_id}.jpg"):
        try:
            p.unlink(missing_ok=True)
        except OSError:
            log.warning("thumb remove failed: %s", p)
