"""缩略图分片存储与动态端点测试（R5，ADR-024）。"""
import numpy as np
from sqlalchemy import select

from app.db.models import FaceIdentity
from app.services import thumbs


def test_shard_path_layout():
    assert thumbs.shard_rel(0) == "000"
    assert thumbs.shard_rel(999) == "000"
    assert thumbs.shard_rel(1234) == "001"
    assert thumbs.thumb_path(1234).as_posix().endswith("thumbs/001/1234.jpg")


def test_save_find_roundtrip_and_legacy_fallback():
    img = np.full((64, 48, 3), 128, dtype=np.uint8)
    assert thumbs.save_thumb(7, img)
    p = thumbs.find_thumb(7)
    assert p is not None and p.as_posix().endswith("thumbs/000/7.jpg")

    # 旧平铺文件回落（存量部署兼容）
    legacy = thumbs.thumbs_root() / "8.jpg"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_bytes(b"legacy")
    assert thumbs.find_thumb(8) == legacy

    thumbs.remove_thumb(7)
    thumbs.remove_thumb(8)
    assert thumbs.find_thumb(7) is None and thumbs.find_thumb(8) is None


def test_thumb_endpoint_shard_and_cache(client, db, tmp_path):
    ident = FaceIdentity(video_id=1, n_samples=1)
    db.add(ident)
    db.commit()
    img = np.full((64, 48, 3), 100, dtype=np.uint8)
    assert thumbs.save_thumb(ident.id, img)

    r = client.get(f"/api/thumbs/{ident.id}.jpg")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/jpeg")
    assert "immutable" in r.headers["cache-control"]

    assert client.get(f"/api/thumbs/{ident.id}.jpg?v=x").status_code == 200
    assert client.get("/api/thumbs/999999.jpg").status_code == 404


def test_pipeline_writes_sharded_thumb(client, db, tmp_path):
    """pipeline 重处理应清理旧缩略图（无孤儿文件累积，R5）。"""
    import cv2

    from app.services import pipeline

    ident = FaceIdentity(video_id=1, n_samples=1)
    db.add(ident)
    db.commit()
    img = np.full((64, 48, 3), 100, dtype=np.uint8)
    thumbs.save_thumb(ident.id, img)
    assert thumbs.find_thumb(ident.id) is not None

    pipeline._reset_video_faces(db, 1)  # 存在用户评分才拒绝；此处无评分
    assert thumbs.find_thumb(ident.id) is None
    assert db.execute(select(FaceIdentity)).scalars().all() == []
