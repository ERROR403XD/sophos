"""R1(P5) 视频按库筛选测试：library 参数、/libraries 端点、与 q/status 叠加。"""
import pytest

from app.db.models import Video


@pytest.fixture()
def two_libs(db):
    va = Video(path="X:/libA/a.mp4", filename="a.mp4", dir_path="X:/libA",
               size_bytes=1, mtime=1.0, status="done")
    vb = Video(path="X:/libB/b.mp4", filename="b.mp4", dir_path="X:/libB",
               size_bytes=2, mtime=2.0, status="pending")
    vc = Video(path="X:/libB/c.mp4", filename="c.mp4", dir_path="X:/libB",
               size_bytes=3, mtime=3.0, status="done")
    db.add_all([va, vb, vc])
    db.commit()
    return va, vb, vc


def test_library_filter(client, two_libs):
    va, vb, vc = two_libs
    body = client.get("/api/videos", params={"library": "X:/libB"}).json()
    assert body["total"] == 2
    assert {i["id"] for i in body["items"]} == {vb.id, vc.id}

    body = client.get("/api/videos", params={"library": "X:/libA"}).json()
    assert body["total"] == 1 and body["items"][0]["id"] == va.id

    # 不存在的库 → 0（精确匹配，非子串）
    assert client.get("/api/videos", params={"library": "X:/lib"}).json()["total"] == 0


def test_library_combined_with_q_and_status(client, two_libs):
    va, vb, vc = two_libs
    r = client.get("/api/videos", params={"library": "X:/libB", "status": "done"})
    assert {i["id"] for i in r.json()["items"]} == {vc.id}

    r = client.get("/api/videos", params={"library": "X:/libB", "q": "b"})
    assert {i["id"] for i in r.json()["items"]} == {vb.id}

    r = client.get("/api/videos", params={"library": "X:/libA", "q": "b"})
    assert r.json()["total"] == 0


def test_libraries_endpoint(client, two_libs):
    body = client.get("/api/videos/libraries")
    assert body.status_code == 200
    assert body.json()["libraries"] == ["X:/libA", "X:/libB"]
