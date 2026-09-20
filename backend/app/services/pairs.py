"""两两对比选对逻辑（M5，ADR-013；R4 人物优先 ADR-019；R5 层级重排 ADR-023）。

策略：
- diverse（R4 起默认）：人物优先的多样化对比。R5 按用户修订的优先级分层
  （ADR-023，覆盖 ADR-019 的旧层级）：
  ① 跨视频不同人物（embedding 明显不同优先，证据不足但无同人迹象次之）
  ② 同视频不同人物（同帧共现 / embedding 明显不同）
  ③ 跨视频可能同一人（很像，降级不禁止）
  ④ 同视频证据不足/疑似同人（R5 面容上限 pass 已尽力合并同人碎片，
     剩余的此类对信息量最低，最后才出现）
  层内按 分差接近 + 随机抖动 排序。"不同人物"是软判断——
  用 face_identity.mean_embedding 余弦 + 同帧共现估计 different_person_confidence，
  **只做抽样排序信号**：不写库、不产生人物关系、不触发跨视频合并，也
  不永久禁止跨视频同演员 pair（候选不足时自然作为 fallback 出现）。
- similar：保留 base_score 接近优先（band 默认 10）的主要语义，仅在相近
  候选内轻度偏向明显不同人物。
- random：任意未对比过的对（只排除完全相同的已比对）。
- 已对比过的对不重复出现；全部对比完返回 None（前端提示"暂无可对比的面容"）。
"""
from __future__ import annotations

import random

import numpy as np
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import Face, FaceIdentity, FaceScore, PairComparison

DEFAULT_BAND = 10.0
DEFAULT_JITTER = 5.0  # diverse 同层内分数抖动上限（分），提供少量随机性

# different_person_confidence 档位（不落库）：同帧共现 > embedding 明显不同
# > 证据不足 > embedding 很接近（很可能是同一人/极相似，跨视频同人降级语义）
_CONF_COOCCUR = 1.0
_CONF_CLEARLY_DIFFERENT = 0.9
_CONF_UNKNOWN = 0.5
_CONF_LIKELY_SAME = 0.1

# ---------------- 大规模数据访问（R5，ADR-024） ----------------
# 数万视频 => 数十万 identity / 百万级 face 行。原实现每次选对全量加载
# embeddings + 全部 face 行 + 全部已比对，规模下不可用。改造：
# 1) 轻量行（id/video_id/score）+ 超库抽样（pair_sample_size，默认 500）；
# 2) 同帧共现改为单条 SQL（video_id+timestamp 复合索引）+ 指纹缓存
#    （face 行数/最大 id 不变即复用）；
# 3) 已比对检查改为逐对索引查询（每对一次，替代全量加载）。


def _total_identities(session: Session) -> int:
    return session.execute(
        select(func.count()).select_from(FaceIdentity)).scalar_one()


def _light_rows(session: Session, sample_size: int) -> dict[int, dict]:
    """id -> {id, video_id, score}（个性化分优先；不加载 embedding 大字段）。

    库规模 ≤ sample_size 时全量（与 R4 精确语义一致）；超过时随机抽样
    （排序信号允许近似，见 ADR-024），候选规模被钳制在 sample_size²/2。
    抽样用**分段连续 id 段**（10 段 × 段内连续）替代逐个随机 id——随机
    点查在 SMB + blob 宽表上是随机寻道（608 点查 ≈ 3.5s，STRESS_LOG S6），
    连续段把寻道次数降到段数级别，段内顺序读。
    """
    total = _total_identities(session)
    base = (
        select(FaceIdentity.id, FaceIdentity.video_id,
               FaceScore.base_score, FaceScore.personalized_score)
        .outerjoin(FaceScore, FaceScore.identity_id == FaceIdentity.id)
    )
    if total <= sample_size:
        rows = session.execute(base).all()
    else:
        rng = random.Random()
        max_id = session.execute(select(func.max(FaceIdentity.id))).scalar_one()
        rows: list = []
        seg_len = max(8, sample_size // 8)
        for _attempt in range(4):  # 补齐因 id 空洞缺行的目标数
            need = sample_size - len(rows)
            if need <= 0:
                break
            picks: list[int] = []
            n_seg = max(2, -(-need // seg_len))
            for s in range(n_seg):
                start = rng.randint(1, max(1, max_id - seg_len))
                picks.extend(range(start, min(start + seg_len, max_id + 1)))
            seen: set[int] = set()
            picks = [p for p in picks if not (p in seen or seen.add(p))]
            for i in range(0, len(picks), 900):  # SQLite 变量数上限保护
                sub = base.where(FaceIdentity.id.in_(picks[i:i + 900]))
                rows.extend(session.execute(sub).all())
        rows = rows[:sample_size]
    return {iid: {"id": iid, "video_id": vid,
                  "score": pers if pers is not None else base_}
            for iid, vid, base_, pers in rows}


def _load_embeddings(session: Session, ids: list[int]) -> dict[int, np.ndarray]:
    """按需加载指定 identity 的 mean_embedding（仅候选集，不扫全库）。"""
    if not ids:
        return {}
    rows = session.execute(
        select(FaceIdentity.id, FaceIdentity.mean_embedding)
        .where(FaceIdentity.id.in_(ids))).all()
    return {iid: np.frombuffer(emb, dtype=np.float32)
            for iid, emb in rows if emb is not None}


def _pair_compared(session: Session, a: int, b: int) -> bool:
    """逐对检查是否已对比过（走 winner/loser 单列索引）。"""
    row = session.execute(
        select(PairComparison.id).where(
            ((PairComparison.winner_identity_id == a)
             & (PairComparison.loser_identity_id == b))
            | ((PairComparison.winner_identity_id == b)
               & (PairComparison.loser_identity_id == a))
        ).limit(1)).scalar_one_or_none()
    return row is not None


def _total_comparisons(session: Session) -> int:
    return session.execute(
        select(func.count()).select_from(PairComparison)).scalar_one()


def _cooccur_pairs(session: Session, tolerance: float,
                   identity_ids: list[int]) -> set[frozenset]:
    """给定 identity 集内的同帧共现对（同 video 内样本时间差 ≤ tolerance）。

    同框出现 = 几乎必为不同人，是比 embedding 更直接的"不同人物"证据
    （PLAN §14）。R5 压测修正：只取**候选 identity 自身**的 face 行
    （抽样下 ≤ 数千行）按视频滑窗——原"全表 96 万行 + 全局指纹缓存"
    冷计算 17s 且缓存对随机抽样无意义；候选对必由两个候选 identity
    组成，其共现证据只涉及它们自己的 face 行，无需全库。
    """
    co: set[frozenset] = set()
    tol = float(tolerance)
    ids = list(identity_ids)
    for i in range(0, len(ids), 900):  # SQLite 变量数上限保护
        rows = session.execute(
            select(Face.video_id, Face.identity_id, Face.timestamp_sec)
            .where(Face.identity_id.in_(ids[i:i + 900]),
                   Face.timestamp_sec.is_not(None))
            .order_by(Face.video_id, Face.timestamp_sec)).all()
        by_video: dict[int, list[tuple[float, int]]] = {}
        for vid, iid, ts in rows:
            by_video.setdefault(vid, []).append((float(ts), iid))
        for items in by_video.values():
            items.sort()
            for k, (t1, i1) in enumerate(items):
                for t2, i2 in items[k + 1:]:
                    if t2 - t1 > tol:
                        break
                    if i1 != i2:
                        co.add(frozenset((i1, i2)))
    return co


def _cosine(a: np.ndarray, b: np.ndarray) -> float:
    na, nb = float(np.linalg.norm(a)), float(np.linalg.norm(b))
    if na < 1e-9 or nb < 1e-9:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


def _different_person_confidence(row_a: dict, row_b: dict,
                                 emb_a: np.ndarray | None,
                                 emb_b: np.ndarray | None,
                                 cooccur: set[frozenset],
                                 sim_clearly_different: float,
                                 sim_likely_same: float) -> float:
    """估计两个 identity 是不同人的置信度（0~1，仅用于排序，不落库）。"""
    key = frozenset((row_a["id"], row_b["id"]))
    if key in cooccur:
        return _CONF_COOCCUR
    if emb_a is None or emb_b is None:
        return _CONF_UNKNOWN
    sim = _cosine(emb_a, emb_b)
    if sim <= sim_clearly_different:
        return _CONF_CLEARLY_DIFFERENT
    if sim >= sim_likely_same:
        return _CONF_LIKELY_SAME
    return _CONF_UNKNOWN


def _compared_pairs_within(session: Session, ids: list[int]) -> set[frozenset]:
    """已比对集合，仅限候选集内部的对（按 winner 索引一次查询，规模有界）。"""
    idset = set(ids)
    rows = session.execute(
        select(PairComparison.winner_identity_id, PairComparison.loser_identity_id)
        .where(PairComparison.winner_identity_id.in_(ids))).all()
    return {frozenset((w, l)) for w, l in rows if l in idset}


def _pair_triu_index(n: int, i: int, j: int) -> int:
    """triu_indices 行主序下 (i, j)（i<j）的一维下标。"""
    return n * i - i * (i + 1) // 2 + (j - i - 1)


def _pick_diverse(session: Session, max_tries: int) -> tuple[int, int] | None:
    from app.config import settings

    rows = _light_rows(session, max(50, settings.pair_sample_size))
    ids = list(rows)
    if len(ids) < 2:
        return None
    total = _total_identities(session)
    if _total_comparisons(session) >= total * (total - 1) // 2:
        return None

    cooccur = _cooccur_pairs(session, settings.merge_cooccur_tolerance_sec, ids)
    embs = _load_embeddings(session, ids)  # 仅候选集，不扫全库
    compared = _compared_pairs_within(session, ids)

    # R5 层级重排（ADR-023）：0=跨视频不同人 1=同视频不同人（共现/明显不同）
    # 2=跨视频可能同人 3=同视频证据不足/疑似同人。层内按 分差+抖动。
    # R5 压测向量化：12.5 万候选对的余弦/分层原为 Python 逐对循环（6.4s 暖态
    # 热点），改 500×500 矩阵乘 + numpy 分层（毫秒级），语义与逐对版一致。
    n = len(ids)
    id_index = {iid: k for k, iid in enumerate(ids)}
    dim = len(next(iter(embs.values()))) if embs else 0
    E = np.zeros((n, dim), dtype=np.float32) if dim else np.zeros((n, 0), np.float32)
    has = np.zeros(n, dtype=bool)
    for iid, emb in embs.items():
        if len(emb) == dim:
            E[id_index[iid]] = emb
            has[id_index[iid]] = True
    iu0, iu1 = np.triu_indices(n, 1)
    npair = len(iu0)
    v_arr = np.array([rows[iid]["video_id"] for iid in ids])
    s_arr = np.array([rows[iid]["score"] if rows[iid]["score"] is not None
                      else np.nan for iid in ids], dtype=np.float64)

    En = np.zeros_like(E)
    if dim:
        norms = np.maximum(np.linalg.norm(E, axis=1, keepdims=True), 1e-9)
        En = E / norms
    S = En @ En.T  # 全矩阵一次乘法；fancy-index 的 125k×512 临时矩阵是压测热点
    sims = np.clip(S[iu0, iu1], 0.0, 1.0)
    both_has = has[iu0] & has[iu1]

    conf = np.where(both_has,
                    np.where(sims <= settings.pair_sim_clearly_different,
                             _CONF_CLEARLY_DIFFERENT,
                             np.where(sims >= settings.pair_sim_likely_same,
                                      _CONF_LIKELY_SAME, _CONF_UNKNOWN)),
                    _CONF_UNKNOWN)
    cross = v_arr[iu0] != v_arr[iu1]
    coo_mask = np.zeros(npair, dtype=bool)
    for fs in cooccur:
        a, b = tuple(fs)
        if a in id_index and b in id_index:
            ia, ib = id_index[a], id_index[b]
            if ia > ib:
                ia, ib = ib, ia
            coo_mask[_pair_triu_index(n, ia, ib)] = True
    conf[coo_mask] = _CONF_COOCCUR

    tier = np.select(
        [(conf <= _CONF_LIKELY_SAME) & cross,
         (conf <= _CONF_LIKELY_SAME) & ~cross,
         cross,
         conf >= _CONF_CLEARLY_DIFFERENT],
        [2, 3, 0, 1], default=3)
    conf_key = np.where((tier == 0) | (tier == 1), -conf, 0.0)
    both_score = np.isfinite(s_arr[iu0]) & np.isfinite(s_arr[iu1])
    diff = np.where(both_score, np.abs(s_arr[iu0] - s_arr[iu1]), np.inf)
    jitter = np.random.default_rng().uniform(0, DEFAULT_JITTER, npair)

    cmp_mask = np.zeros(npair, dtype=bool)
    for fs in compared:
        a, b = tuple(fs)
        if a in id_index and b in id_index:
            ia, ib = id_index[a], id_index[b]
            if ia > ib:
                ia, ib = ib, ia
            cmp_mask[_pair_triu_index(n, ia, ib)] = True
    tier = np.where(cmp_mask, 99, tier)  # 已比对对排除出候选

    order = np.lexsort((diff + jitter, conf_key, tier))
    if len(order) == 0 or tier[order[0]] == 99:
        return None
    k = order[0]
    return ids[int(iu0[k])], ids[int(iu1[k])]


def pick_pair(session: Session, strategy: str = "similar",
              band: float = DEFAULT_BAND, max_tries: int = 300) -> tuple[int, int] | None:
    from app.config import settings

    if strategy == "diverse":
        return _pick_diverse(session, max_tries)

    rows = _light_rows(session, max(50, settings.pair_sample_size))
    ids = list(rows)
    if len(ids) < 2:
        return None
    total = _total_identities(session)
    if _total_comparisons(session) >= total * (total - 1) // 2:
        return None

    cooccur = (_cooccur_pairs(session, settings.merge_cooccur_tolerance_sec, ids)
               if strategy == "similar" else set())
    embs = _load_embeddings(session, ids) if strategy == "similar" else {}
    compared = _compared_pairs_within(session, ids)

    # similar 轻度人物偏好（R4 §18）：band 内先找"明显不同人物"的对；
    # 找不到再退回原语义（band 内任意未对比对）
    fallback: tuple[int, int] | None = None
    for _ in range(max_tries):
        a, b = random.sample(ids, 2)
        if frozenset((a, b)) in compared:
            continue
        if strategy == "similar":
            sa, sb = rows[a]["score"], rows[b]["score"]
            if sa is not None and sb is not None and abs(sa - sb) > band:
                continue
            conf = _different_person_confidence(
                rows[a], rows[b], embs.get(a), embs.get(b), cooccur,
                settings.pair_sim_clearly_different,
                settings.pair_sim_likely_same)
            if conf >= _CONF_CLEARLY_DIFFERENT:
                return a, b
            if fallback is None:
                fallback = (a, b)
            continue
        return a, b

    if fallback is not None:
        return fallback

    # band 内找不到：放宽 similar 约束，取任意未对比对
    for _ in range(max_tries):
        a, b = random.sample(ids, 2)
        if frozenset((a, b)) not in compared:
            return a, b
    return None
