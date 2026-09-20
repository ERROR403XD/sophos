"""聚类测试（M3）：合成 embedding 两组分离 → 恰好两簇；单样本自成一组。
R1(P3) 追加：合并 pass（组间余弦二次合并）。"""
import numpy as np

from app.services.clustering import cluster_embeddings, merge_groups


def _cluster(direction, n, seed):
    rng = np.random.default_rng(seed)
    e = np.tile(np.asarray(direction, dtype=np.float32), (n, 1)) \
        + rng.normal(0, 0.02, (n, len(direction))).astype(np.float32)
    return e / np.linalg.norm(e, axis=1, keepdims=True)


def test_two_tight_clusters():
    a = _cluster([1, 0, 0, 0], 5, seed=1)
    b = _cluster([0, 1, 0, 0], 4, seed=2)
    embs = np.vstack([a, b])
    groups = cluster_embeddings(embs, threshold=0.68)
    assert len(groups) == 2
    flat = sorted(i for g in groups for i in g)
    assert flat == list(range(9))
    label = {}
    for gi, g in enumerate(groups):
        for i in g:
            label[i] = gi
    assert len({label[i] for i in range(5)}) == 1      # a 组内同簇
    assert len({label[i] for i in range(5, 9)}) == 1   # b 组内同簇
    assert label[0] != label[5]                        # 两组互斥


def test_single_and_empty():
    assert cluster_embeddings(np.zeros((0, 8), dtype=np.float32)) == []
    g = cluster_embeddings(np.array([[1.0, 0, 0, 0]], dtype=np.float32))
    assert g == [[0]]


def test_threshold_splits_loose_cluster():
    # 同方向但噪声较大：高阈值应分裂成多组
    rng = np.random.default_rng(3)
    base = np.array([1, 0, 0, 0], dtype=np.float32)
    e = base[None, :] + rng.normal(0, 0.35, (8, 4)).astype(np.float32)
    e /= np.linalg.norm(e, axis=1, keepdims=True)
    groups = cluster_embeddings(e, threshold=0.995)
    assert len(groups) >= 2


# ---------------- R1(P3)：合并 pass ----------------

def test_merge_groups_fuses_fragments():
    """同人碎片组（组均值互相 ≥0.78）合并为 1 组，样本并集保留。"""
    a = _cluster([1, 0, 0, 0], 3, seed=11)
    b = _cluster([0.93, 0.36, 0, 0], 2, seed=12)   # 与 a 方向余弦 ≈0.93
    c = _cluster([0, 0, 1, 0], 3, seed=13)         # 异人（正交）
    embs = np.vstack([a, b, c])
    fragments = [[0, 1, 2], [3, 4], [5, 6, 7]]
    merged = merge_groups(embs, fragments, threshold=0.78)
    assert len(merged) == 2                        # a+b 并组，c 独立
    flat = sorted(i for g in merged for i in g)
    assert flat == list(range(8))                  # 样本并集不丢
    ab = next(g for g in merged if 0 in g)
    assert set(ab) == {0, 1, 2, 3, 4}


def test_merge_groups_keeps_distinct_identities():
    """正交方向（余弦≈0）不合并。"""
    a = _cluster([1, 0, 0, 0], 2, seed=21)
    b = _cluster([0, 1, 0, 0], 2, seed=22)
    merged = merge_groups(np.vstack([a, b]), [[0, 1], [2, 3]], threshold=0.78)
    assert sorted(merged) == [[0, 1], [2, 3]]


def test_merge_groups_idempotent_and_empty():
    embs = np.array([[1.0, 0], [0.99, 0.01]], dtype=np.float32)
    embs /= np.linalg.norm(embs, axis=1, keepdims=True)
    once = merge_groups(embs, [[0], [1]], threshold=0.78)
    again = merge_groups(embs, once, threshold=0.78)
    assert once == again == [[0, 1]]
    assert merge_groups(np.zeros((0, 4), dtype=np.float32), []) == []


# ---------------- R4(P1)：双层 merge + 同帧共现否决（ADR-018） ----------------

def _unit(v):
    v = np.asarray(v, dtype=np.float32)
    return v / np.linalg.norm(v)


def test_merge_layer2_cautious_merges_fragments():
    """谨慎层：组均值余弦落在 [0.72, 0.78)，双方多个干净样本且无共现 → 合并。"""
    a = _cluster([1, 0, 0, 0], 3, seed=31)                       # 组均值方向 x
    b = _cluster([0.75, 0.6614, 0, 0], 2, seed=32)               # 与 a 余弦 ≈0.75
    c = _cluster([0, 0, 1, 0], 3, seed=33)
    embs = np.vstack([a, b, c])
    groups = [[0, 1, 2], [3, 4], [5, 6, 7]]
    # review_threshold=0（旧口径）→ 不合并
    assert len(merge_groups(embs, groups, threshold=0.78)) == 3
    # R4 谨慎层 → a+b 合并
    merged = merge_groups(embs, groups, threshold=0.78, review_threshold=0.72)
    assert len(merged) == 2
    assert set(next(g for g in merged if 0 in g)) == {0, 1, 2, 3, 4}


def test_merge_cooccurrence_forbids_layer2():
    """谨慎层：时间戳同帧重叠（|Δt| ≤ 容差）→ 禁止合并。"""
    a = _cluster([1, 0, 0, 0], 3, seed=41)
    b = _cluster([0.75, 0.6614, 0, 0], 2, seed=42)
    embs = np.vstack([a, b])
    groups = [[0, 1, 2], [3, 4]]
    ts = [0.0, 2.0, 4.0, 3.9, 4.1]  # 样本 2（t=4）与样本 3/4（t≈4）同帧
    merged = merge_groups(embs, groups, threshold=0.78, review_threshold=0.72,
                          sample_timestamps=ts)
    assert len(merged) == 2  # 仍为两组
    # 同组内时间戳不重叠 → 恢复合并
    ts_far = [0.0, 2.0, 4.0, 10.0, 12.0]
    merged2 = merge_groups(embs, groups, threshold=0.78, review_threshold=0.72,
                           sample_timestamps=ts_far)
    assert len(merged2) == 1


def test_merge_cooccurrence_forbids_even_strong_similarity():
    """同帧共现一票否决优先于高置信层：余弦 ≈0.93 也不合并（PLAN §5）。"""
    a = _cluster([1, 0, 0, 0], 3, seed=51)
    b = _cluster([0.93, 0.36, 0, 0], 2, seed=52)  # 与 a 余弦 ≈0.93（强）
    embs = np.vstack([a, b])
    groups = [[0, 1, 2], [3, 4]]
    assert len(merge_groups(embs, groups, threshold=0.78)) == 1  # 无时间戳：照常合并
    ts = [0.0, 2.0, 4.0, 4.0, 6.0]
    merged = merge_groups(embs, groups, threshold=0.78,
                          sample_timestamps=ts)
    assert len(merged) == 2  # 同帧共现 → 即使强相似也禁止


def test_merge_layer2_requires_multiple_clean_samples():
    """谨慎层：干净样本不足（单样本组 / 全遮挡组）→ 不合并。"""
    a = _cluster([1, 0, 0, 0], 3, seed=61)
    b = _cluster([0.75, 0.6614, 0, 0], 2, seed=62)
    embs = np.vstack([a, b])
    groups = [[0, 1, 2], [3, 4]]
    # 组 b 仅 1 个干净样本（另一个被遮挡）→ 谨慎层条件不满足
    clean = [True, True, True, False, True]
    merged = merge_groups(embs, groups, threshold=0.78, review_threshold=0.72,
                          sample_clean=clean)
    assert len(merged) == 2
    # 干净样本充足 → 合并
    clean_ok = [True] * 5
    merged2 = merge_groups(embs, groups, threshold=0.78, review_threshold=0.72,
                           sample_clean=clean_ok)
    assert len(merged2) == 1


def test_merge_layer2_uses_clean_sample_means():
    """谨慎层：干净样本均值不过线 → 拒绝（全样本均值过线也不行）。"""
    a_dir = _unit([1, 0, 0, 0])
    d1 = _unit([0.62, 0.7846, 0, 0])          # 与 a 余弦 0.62（干净均值，不过线）
    d2 = _unit([0.92, 0.392, 0, 0])           # 与 a 余弦 0.92（被遮挡样本，拉高全样本均值）
    embs = np.stack([a_dir, a_dir, a_dir, d1, d1, d2])
    groups = [[0, 1, 2], [3, 4, 5]]
    # 全样本均值余弦 ≈0.74 落入谨慎区间，但干净均值 0.62 < 0.72 → 拒绝
    clean = [True, True, True, True, True, False]
    merged = merge_groups(embs, groups, threshold=0.78, review_threshold=0.72,
                          sample_clean=clean)
    assert len(merged) == 2
    # 对照：遮挡样本转干净后干净均值=全样本均值 ≈0.74 → 合并
    clean_all = [True] * 6
    merged2 = merge_groups(embs, groups, threshold=0.78, review_threshold=0.72,
                           sample_clean=clean_all)
    assert len(merged2) == 1


# ---------------- R6(P1)：第三层单样本同人合并（ADR-028） ----------------

def test_merge_singleton_tier_merges_single_samples():
    """双方均单样本、余弦 ∈ [0.74, 0.78)、无共现 → 合并。

    R4 谨慎层要求双方 ≥2 干净样本，单样本碎片永远合不上（基线 ~78% 单样本
    identity 的主因，用户复检反馈"同一个人识别成不同的人"）。
    """
    a = _unit([1, 0, 0, 0])
    b = _unit([0.75, 0.6614, 0, 0])   # 与 a 余弦 ≈0.75
    embs = np.stack([a, b])
    # R4 口径：单样本对不合并（谨慎层拒绝）
    assert merge_groups(embs, [[0], [1]], threshold=0.78,
                        review_threshold=0.72) == [[0], [1]]
    # R6 第三层 → 合并
    merged = merge_groups(embs, [[0], [1]], threshold=0.78, review_threshold=0.72,
                          singleton_threshold=0.74)
    assert merged == [[0, 1]]


def test_merge_singleton_tier_respects_floor_and_cooccurrence():
    """第三层护栏：低于下限不并；同帧共现一票否决。"""
    a = _unit([1, 0, 0, 0])
    low = _unit([0.60, 0.8, 0, 0])    # 余弦 ≈0.60 < 0.74 → 不并
    embs = np.stack([a, low])
    assert merge_groups(embs, [[0], [1]], threshold=0.78,
                        singleton_threshold=0.74) == [[0], [1]]

    same = _unit([0.75, 0.6614, 0, 0])  # ≈0.75 过线但同帧共现
    embs2 = np.stack([a, same])
    merged = merge_groups(embs2, [[0], [1]], threshold=0.78, singleton_threshold=0.74,
                          sample_timestamps=[2.0, 2.2])
    assert merged == [[0], [1]]
