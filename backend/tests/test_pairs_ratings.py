"""两两对比 + 用户评分端点测试（M5，ADR-013）。"""
import pytest
from sqlalchemy.orm import Session

from app.db.models import FaceIdentity, PairComparison, UserRating
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
