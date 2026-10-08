"""两两对比 + 用户评分端点测试（M5，ADR-013）。"""
import pytest
from sqlalchemy.orm import Session

from app.db.models import FaceIdentity, PairComparison, UserRating, Video
from app.services.pairs import pick_pair


@pytest.fixture()
def four_identities(db):
    """4 个面容：90 / 80 / 70 / 10 分（SQLite 未启用外键强制，video_id=0 即可）。"""
    from app.db.models import FaceScore

    ids = []
    for base in (90.0, 80.0, 70.0, 10.0):
        ident = FaceIdentity(video_id=0, n_samples=2)
        db.add(ident)
        db.flush()
        db.add(FaceScore(identity_id=ident.id, base_score=base, base_model_version="t"))
        db.commit()
        ids.append(ident.id)
    return ids


def test_pick_pair_similar_band(db, four_identities):
    """similar 策略：分差 ≤10 才入选；已对比过的对被排除。"""
    i90, i80, i70, _i10 = four_identities
    db.add(PairComparison(winner_identity_id=i90, loser_identity_id=i80))
    db.commit()

    for _ in range(50):
        pair = pick_pair(db, strategy="similar", band=10.0)
        assert pair is not None
        # (90,80) 分差合格但已对比；(90,70)/(80,10)/(70,10)/(90,10) 分差超 band
        # → 唯一可选对是 (80,70)
        assert set(pair) == {i80, i70}


def test_pick_pair_random_excludes_compared(db, four_identities):
    i90, i80, i70, i10 = four_identities
    db.add(PairComparison(winner_identity_id=i90, loser_identity_id=i80))
    db.commit()
    for _ in range(50):
        pair = pick_pair(db, strategy="random")
        assert frozenset(pair) != frozenset((i90, i80))


def test_pick_pair_none_when_exhausted(db, four_identities):
    i90, i80, i70, i10 = four_identities
    for w, l in ((i90, i80), (i90, i70), (i90, i10), (i80, i70), (i80, i10), (i70, i10)):
        db.add(PairComparison(winner_identity_id=w, loser_identity_id=l))
    db.commit()
    assert pick_pair(db, strategy="random") is None


def test_pair_api_flow(client, db, four_identities):
    r = client.get("/api/faces/pair?strategy=random")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"a", "b"}
    a, b = body["a"], body["b"]
    assert a["id"] != b["id"]
    assert a["rep_thumb"].startswith("/api/thumbs/")
    assert a["base_score"] is not None

    r = client.post("/api/faces/pair/compare",
                    json={"winner_id": a["id"], "loser_id": b["id"]})
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert db.query(PairComparison).count() == 1

    # 非法输入
    assert client.post("/api/faces/pair/compare",
                       json={"winner_id": a["id"], "loser_id": a["id"]}).status_code == 400
    assert client.post("/api/faces/pair/compare",
                       json={"winner_id": 9999, "loser_id": a["id"]}).status_code == 404
    assert client.get("/api/faces/pair?strategy=bad").status_code == 400


def test_rating_endpoints(client, db, four_identities):
    target = four_identities[0]

    r = client.post(f"/api/faces/{target}/rating", json={"type": "score", "value": 8})
    assert r.status_code == 200
    r = client.post(f"/api/faces/{target}/rating", json={"type": "thumbs", "value": "up"})
    assert r.status_code == 200

    # 非法值
    assert client.post(f"/api/faces/{target}/rating",
                       json={"type": "score", "value": 11}).status_code == 400
    assert client.post(f"/api/faces/{target}/rating",
                       json={"type": "thumbs", "value": "meh"}).status_code == 400
    assert client.post(f"/api/faces/{target}/rating",
                       json={"type": "bad", "value": 5}).status_code == 422
    assert client.post("/api/faces/9999/rating",
                       json={"type": "score", "value": 5}).status_code == 404

    # my_rating = 最新一条（up）
    items = client.get("/api/faces").json()["items"]
    mine = [i for i in items if i["id"] == target][0]
    assert mine["my_rating"] == "up"

    # 统计
    stats = client.get("/api/ratings/stats").json()
    assert stats["score_count"] == 1
    assert stats["thumbs_count"] == 1
    assert stats["score_histogram"]["8"] == 1
    assert stats["unique_faces_rated"] == 1
    assert stats["pair_count"] == 0

    # unrated 过滤
    unrated = client.get("/api/faces", params={"unrated": True}).json()
    assert target not in [i["id"] for i in unrated["items"]]
    assert unrated["total"] == 3


def test_rate_all_faces_in_one_video(client, db):
    video = Video(path="X:/v/a.mp4", filename="a.mp4", dir_path="X:/v",
                  size_bytes=1, mtime=1.0, status="done")
    db.add(video)
    db.flush()
    first = FaceIdentity(video_id=video.id, n_samples=1)
    second = FaceIdentity(video_id=video.id, n_samples=2)
    empty = Video(path="X:/v/b.mp4", filename="b.mp4", dir_path="X:/v",
                  size_bytes=1, mtime=1.0, status="done")
    db.add_all([first, second, empty])
    db.commit()

    r = client.post("/api/faces/rate-video",
                    json={"video_id": video.id, "verdict": "up"})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "video_id": video.id,
                        "rated": [first.id, second.id]}
    rows = db.query(UserRating).order_by(UserRating.id).all()
    assert [(row.identity_id, row.rating_type, row.rating_value) for row in rows] == [
        (first.id, "thumbs", "up"), (second.id, "thumbs", "up")]

    assert client.post("/api/faces/rate-video",
                       json={"video_id": empty.id, "verdict": "up"}).status_code == 409
    assert client.post("/api/faces/rate-video",
                       json={"video_id": 9999, "verdict": "up"}).status_code == 404
    assert client.post("/api/faces/rate-video",
                       json={"video_id": video.id, "verdict": "meh"}).status_code == 422


# ---------------- R12：同时好评/差评 + 撤销 ----------------

def test_rate_both_endpoint(client, db, four_identities):
    """对比页"同时好评/差评"：同一 verdict 给两张面容各写一条 thumbs 评分。"""
    a, b = four_identities[0], four_identities[1]
    r = client.post("/api/faces/pair/rate-both",
                    json={"identity_ids": [a, b], "verdict": "up"})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "rated": [a, b]}
    rows = db.query(UserRating).order_by(UserRating.id).all()
    assert [(x.identity_id, x.rating_type, x.rating_value) for x in rows] == [
        (a, "thumbs", "up"), (b, "thumbs", "up")]

    # 非法输入：同 id / 非法 verdict / 不存在的面容 / 数量不对
    assert client.post("/api/faces/pair/rate-both",
                       json={"identity_ids": [a, a], "verdict": "up"}).status_code == 400
    assert client.post("/api/faces/pair/rate-both",
                       json={"identity_ids": [a, b], "verdict": "meh"}).status_code == 400
    assert client.post("/api/faces/pair/rate-both",
                       json={"identity_ids": [a, 9999], "verdict": "up"}).status_code == 404
    assert client.post("/api/faces/pair/rate-both",
                       json={"identity_ids": [a], "verdict": "up"}).status_code == 400


def test_undo_cross_table_latest_first(client, db, four_identities):
    """撤销 = 跨表最近一次：评分 → 对比 → 评分 依次撤销，顺序严格正确。"""
    a, b, c, d = four_identities
    assert client.post("/api/faces/undo").status_code == 404

    client.post(f"/api/faces/{a}/rating", json={"type": "score", "value": "5"})
    client.post("/api/faces/pair/compare", json={"winner_id": b, "loser_id": c})
    client.post(f"/api/faces/{d}/rating", json={"type": "thumbs", "value": "up"})

    r = client.post("/api/faces/undo")
    assert r.status_code == 200
    assert r.json() == {"kind": "rating", "identity_id": d,
                        "type": "thumbs", "value": "up"}
    assert db.query(UserRating).filter(UserRating.identity_id == d).count() == 0

    r = client.post("/api/faces/undo").json()
    assert r == {"kind": "pair", "winner_id": b, "loser_id": c}
    assert db.query(PairComparison).count() == 0

    r = client.post("/api/faces/undo").json()
    assert r["kind"] == "rating" and r["identity_id"] == a
    assert db.query(UserRating).count() == 0
    assert client.post("/api/faces/undo").status_code == 404


def test_undo_rate_then_pair_same_second(client, db, four_identities):
    """同秒内"先评分后对比"（R12 起时间戳为微秒精度）：撤销必须命中对比。"""
    a, b, c, _d = four_identities
    client.post(f"/api/faces/{a}/rating", json={"type": "thumbs", "value": "up"})
    client.post("/api/faces/pair/compare", json={"winner_id": b, "loser_id": c})
    r = client.post("/api/faces/undo").json()
    assert r["kind"] == "pair"
