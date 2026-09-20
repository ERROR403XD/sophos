"""面容列表 stream 字段下发 + 随机顺序测试（R5.2）。"""
import pytest

from app.db.models import Face, FaceIdentity, FaceScore, UserRating, Video


@pytest.fixture()
def video_with_faces(db, tmp_path):
    v = Video(path=str(tmp_path / "v1.mp4"), filename="v1.mp4",
              dir_path=str(tmp_path), status="done",
              vcodec="h264", acodec="aac")
    db.add(v)
    db.commit()
    idents = []
    for k in range(5):
        ident = FaceIdentity(video_id=v.id, n_samples=1)
        db.add(ident)
        db.flush()
        face = Face(video_id=v.id, identity_id=ident.id, timestamp_sec=10.0 * k)
        db.add(face)
        db.flush()
        ident.rep_face_id = face.id  # 列表端点的 timestamp_sec 取自代表帧
        idents.append(ident.id)
        db.add(FaceScore(identity_id=ident.id, base_score=50.0 + k,
                         base_model_version="t"))
    db.commit()
    return v, idents


def test_faces_item_has_stream_fields(client, db, video_with_faces):
    """评分/对比页直接播放：列表项下发 stream_url/stream_mode/video_path。"""
    r = client.get("/api/faces")
    assert r.status_code == 200
    item = r.json()["items"][0]
    assert item["stream_url"] == "/api/videos/1/stream"
    assert item["stream_mode"] == "direct"  # mp4 h264+aac
    assert item["video_path"].endswith("v1.mp4")
    assert item["timestamp_sec"] is not None


def test_faces_order_random(client, db, video_with_faces):
    """order=random：返回有效元素；非法 order 400。"""
    r1 = client.get("/api/faces", params={"order": "random", "page_size": 3})
    assert r1.status_code == 200
    items = r1.json()["items"]
    assert 1 <= len(items) <= 3
    assert all(i["stream_url"] for i in items)

    assert client.get("/api/faces", params={"order": "bad"}).status_code == 400


def test_faces_unrated_excludes_rated_random(client, db, video_with_faces):
    v, idents = video_with_faces
    db.add(UserRating(identity_id=idents[0], rating_type="score", rating_value="7"))
    db.commit()
    body = client.get("/api/faces",
                      params={"unrated": True, "order": "random", "page_size": 50}).json()
    got = {i["id"] for i in body["items"]}
    assert idents[0] not in got and len(got) >= 2
    assert body["total"] == 4
