"""diverse pair 策略测试（R4 ADR-019 引入；R5 ADR-023 层级重排）。

R5 优先级（用户修订）：①跨视频不同人物 > ②同视频不同人物（共现/明显不同）
> ③跨视频可能同人 > ④同视频证据不足/疑似同人；层内分差接近 + 随机抖动。
embedding 均为合成向量（cosine 可控）。
"""
import numpy as np

from app.db.models import Face, FaceIdentity, FaceScore, PairComparison
from app.services.pairs import pick_pair


def _emb(*components) -> np.ndarray:
    v = np.asarray(components, dtype=np.float32)
    return v / np.linalg.norm(v)


E_X = _emb(1, 0, 0)
E_Y = _emb(0, 1, 0)
E_Z = _emb(0, 0, 1)
E_XY = _emb(1, 1, 0)  # 与 X/Y 均 45°（cos 0.707 ≥ likely_same → 同演员语义）


def _identity(db, video_id, emb=None, score=None) -> int:
    ident = FaceIdentity(video_id=video_id, n_samples=2)
    if emb is not None:
        ident.mean_embedding = emb.tobytes()
    db.add(ident)
    db.flush()
    if score is not None:
        db.add(FaceScore(identity_id=ident.id, base_score=score,
                         base_model_version="t"))
    db.commit()
    return ident.id


def _face(db, video_id, identity_id, ts) -> None:
    db.add(Face(video_id=video_id, identity_id=identity_id, timestamp_sec=ts))
    db.commit()


def test_diverse_prefers_clearly_different_people(db):
    """Tier A：embedding 明显不同（正交）优先于跨视频高相似（同演员）。"""
    a = _identity(db, 1, emb=E_X, score=50)        # video 1
    b = _identity(db, 2, emb=E_Y, score=80)        # video 2，与 a 正交
    c = _identity(db, 3, emb=E_XY, score=50)       # video 3，与 a/b 都 cos 0.707
    for _ in range(20):
        pair = pick_pair(db, strategy="diverse")
        assert frozenset(pair) == frozenset((a, b))


def test_diverse_cooccurrence_beats_cross_video_likely_same(db):
    """② 同视频共现对压过 ③ 跨视频可能同人（即使分差 100 vs 2）。"""
    p = _identity(db, 4, emb=E_X, score=50)
    q = _identity(db, 4, emb=E_Y, score=150)   # 与 p 共现（同帧）→ ②，分差 100
    r = _identity(db, 5, emb=E_X, score=50)    # 与 p 同演员（cos 1）→ ③，分差 2
    s = _identity(db, 6, emb=E_X, score=52)
    _face(db, 4, p, 4.0)
    _face(db, 4, q, 4.1)
    # 先把跨视频污染对记掉：q 对 r/s 是正交跨视频（①），p 对 r/s 是 ③ 同层
    for w, l in ((q, r), (q, s), (p, r), (p, s)):
        db.add(PairComparison(winner_identity_id=w, loser_identity_id=l))
    db.commit()
    for _ in range(20):
        assert frozenset(pick_pair(db, strategy="diverse")) == frozenset((p, q))


def test_diverse_cross_video_different_beats_same_video_cooccurrence(db):
    """① 跨视频不同人物压过 ② 同视频共现对（R5 用户修订的新层级）。"""
    a = _identity(db, 1, emb=E_X, score=50)
    b = _identity(db, 1, emb=E_Y, score=55)   # 同视频共现 → ②
    c = _identity(db, 2, emb=E_Z, score=50)   # 与 a/b 均正交跨视频 → ①
    _face(db, 1, a, 4.0)
    _face(db, 1, b, 4.1)
    for _ in range(20):
        pair = pick_pair(db, strategy="diverse")
        assert c in pair


def test_diverse_cross_video_likely_same_beats_same_video_unknown(db):
    """③ 跨视频可能同人压过 ④ 同视频证据不足（信息量最低）。

    a/b 无 embedding → 它们与 c/d 的跨视频对为 ①（证据未知），先记掉，
    剩 (a,b) ④ vs (c,d) ③ → (c,d) 胜。
    """
    a = _identity(db, 1, score=50)   # 同视频、无 embedding → ④
    b = _identity(db, 1, score=51)
    c = _identity(db, 2, emb=E_Z, score=50)   # 跨视频同演员对 → ③
    d = _identity(db, 3, emb=E_Z, score=52)
    for w, l in ((a, c), (a, d), (b, c), (b, d)):
        db.add(PairComparison(winner_identity_id=w, loser_identity_id=l))
    db.commit()
    for _ in range(20):
        pair = pick_pair(db, strategy="diverse")
        assert frozenset(pair) == frozenset((c, d))


def test_diverse_cross_video_when_evidence_unknown(db):
    """人物证据不足（无 embedding、无共现）→ 跨视频（①）优先于同视频（④）。"""
    a = _identity(db, 1)  # video 1，无 embedding
    b = _identity(db, 1)  # video 1
    c = _identity(db, 2)  # video 2
    for _ in range(20):
        pair = pick_pair(db, strategy="diverse")
        assert c in pair          # 跨视频对（a,c)/(b,c) 优先
        assert set(pair) != {a, b}


def test_diverse_cross_video_unknown_beats_same_video_unknown(db):
    """①（跨视频证据不足）必须压过 ④（同视频证据不足）。"""
    a = _identity(db, 1, score=50)
    b = _identity(db, 1, score=51)
    c = _identity(db, 2, score=99)
    assert c in pick_pair(db, strategy="diverse")


def test_diverse_cross_video_same_person_still_fallback(db):
    """跨视频同演员（高相似，③）不被禁止：候选耗尽后仍可被选中（PLAN §9）。"""
    a = _identity(db, 1, emb=E_X, score=50)
    b = _identity(db, 2, emb=E_X, score=52)   # 跨视频同演员 cos 1
    c = _identity(db, 3, emb=E_Y, score=50)
    db.add(PairComparison(winner_identity_id=a, loser_identity_id=c))
    db.add(PairComparison(winner_identity_id=b, loser_identity_id=c))
    db.commit()
    assert frozenset(pick_pair(db, strategy="diverse")) == frozenset((a, b))


def test_diverse_score_diff_is_secondary(db):
    """同层（①，证据同强度）内优先分差小的；层内置信度次序压倒分差。"""
    a = _identity(db, 1, emb=E_X, score=50)
    b = _identity(db, 2, emb=E_Y, score=80)   # 与 a 正交，分差 30
    c = _identity(db, 3, emb=E_Z, score=52)   # 与 a/b 均正交，分差 2
    for _ in range(20):
        assert frozenset(pick_pair(db, strategy="diverse")) == frozenset((a, c))

    # ① 层内：明显不同（conf 0.9）压过证据不足（conf 0.5），即使分差更大——
    # (a,h) 分差 1 < (a,c) 分差 2，但 (a,h) 无 embedding（unknown）仍靠后
    h = _identity(db, 7, score=51)             # 无 embedding
    i = _identity(db, 8, emb=E_Z, score=99)    # 与 a/b 正交（分差 49）
    for _ in range(20):
        assert frozenset(pick_pair(db, strategy="diverse")) == frozenset((a, c))


def test_diverse_excludes_compared_then_none(db):
    """已比较对排除；全部对比完 → None（API 层转 NO_PAIR 404）。"""
    a = _identity(db, 1, emb=E_X, score=50)
    b = _identity(db, 2, emb=E_Y, score=80)
    c = _identity(db, 3, emb=E_XY, score=50)
    seen = []
    while True:
        pair = pick_pair(db, strategy="diverse")
        assert pair is not None
        w, l = pair
        db.add(PairComparison(winner_identity_id=w, loser_identity_id=l))
        db.commit()
        seen.append(frozenset(pair))
        if len(seen) == 3:
            break
    assert len(set(seen)) == 3  # 无重复
    assert pick_pair(db, strategy="diverse") is None


def test_diverse_pair_api_default_and_no_pair(client, db):
    """API 默认策略即 diverse；耗尽后 404 NO_PAIR；非法策略 400。"""
    ids = []
    for vid, emb in ((1, E_X), (2, E_Y), (3, E_XY)):
        ident = FaceIdentity(video_id=vid, n_samples=1, mean_embedding=emb.tobytes())
        db.add(ident)
        db.commit()
        ids.append(ident.id)

    r = client.get("/api/faces/pair")  # 不带 strategy 参数
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"a", "b"}

    # 记满 3 对 → NO_PAIR
    for w, l in ((ids[0], ids[1]), (ids[0], ids[2]), (ids[1], ids[2])):
        client.post("/api/faces/pair/compare", json={"winner_id": w, "loser_id": l})
    r = client.get("/api/faces/pair?strategy=diverse")
    assert r.status_code == 404
    assert r.json()["code"] == "NO_PAIR"

    assert client.get("/api/faces/pair?strategy=bad").status_code == 400
