"""面容数上限 pass 测试（R5，ADR-023）：同一视频同一人最多保留 N 张面容。

cap_groups 的组件化语义：
- 余弦 ≥ floor 且无共现 → 单链连通分量（"可能同一个人"）；
- 组件内反复合并最高相似对，直到 ≤ max_groups；
- 共现一票否决（组件建边与合并候选都不豁免）；
- 无可并对（全部被共现否决/低于 floor）→ 宁超限不误并；
- 多个不同人共存的视频各封各的顶，不跨组件强并。
"""
import numpy as np

from app.services.clustering import cap_groups


def _emb(*components) -> np.ndarray:
    v = np.asarray(components, dtype=np.float32)
    return v / np.linalg.norm(v)


def _singletons(n: int, embs: np.ndarray) -> tuple[np.ndarray, list[list[int]]]:
    """n 个单样本组（碎片化基线：R4 后大量单样本 identity 的形态）。"""
    return np.stack(embs), [[i] for i in range(n)]


def test_cap_noop_and_disabled():
    e = np.stack([_emb(1, 0), _emb(0, 1), _emb(1, 1)])
    groups = [[0], [1], [2]]
    assert cap_groups(e, groups, 5) == groups          # 未超限
    assert cap_groups(e, groups, 0) == groups          # 0 = 不限制


def test_cap_merges_within_person_component_only():
    """25 个同人碎片（组件）→ 上限 5：并到 5 个，全部同人。"""
    rng = np.random.default_rng(7)
    xs = [_emb(1, 0) + rng.normal(0, 0.01, 2) for _ in range(25)]
    embs, groups = _singletons(25, xs)
    capped = cap_groups(embs, groups, max_groups=5, floor=0.5)
    assert len(capped) == 5
    assert sorted(i for g in capped for i in g) == list(range(25))


def test_cap_multi_person_independent_caps():
    """8 个 A 碎片 + 3 个 B 碎片 → 上限 5：A 并到 5、B 保持 3（共 8 组）。"""
    rng = np.random.default_rng(9)
    xs = [_emb(1, 0) + rng.normal(0, 0.01, 2) for _ in range(8)]
    ys = [_emb(0, 1) + rng.normal(0, 0.01, 2) for _ in range(3)]
    embs, groups = _singletons(11, xs + ys)
    capped = cap_groups(embs, groups, max_groups=5, floor=0.5)
    assert len(capped) == 8  # 5 + 3，绝不为凑总数跨人合并
    for g in capped:
        base = g[0] < 8  # 前 8 个是 A，后 3 个是 B
        assert all((i < 8) == base for i in g)


def test_cap_respects_cooccurrence_veto():
    """共现一票否决：0/1 同帧出现过，二者绝不直接并入（即使链路经 2 也要绕）。

    全同 embedding + ts=[1,1,60]：(0,2) 可并、并后时间戳并集使 (0∪2, 1)
    经 0 的 ts=1.0 被否决 → 停在 2 组（max=1 宁超限）。
    """
    ts = [1.0, 1.0, 60.0]
    e = _emb(1, 0)
    embs = np.stack([e, e, e])
    capped = cap_groups(embs, [[0], [1], [2]], max_groups=1, floor=0.5,
                        sample_timestamps=ts)
    assert len(capped) == 2
    assert {0, 2} in [set(g) for g in capped]  # 并入的是 (0,2)，不是 (0,1)


def test_cap_respects_floor():
    """全部对都低于 floor（明显不同人）→ 不强并，保持超限。"""
    embs = np.stack([_emb(1, 0, 0), _emb(0, 1, 0), _emb(0, 0, 1), _emb(-1, 0, 0)])
    groups = [[i] for i in range(4)]
    assert cap_groups(embs, groups, max_groups=1, floor=0.5) == groups


def test_cap_components_independent_without_bridge():
    """A/B 两组碎片互不相似（全对 < floor）→ 两个独立组件，各封到 3。"""
    rng = np.random.default_rng(11)
    a = [_emb(1, 0) + rng.normal(0, 0.01, 2) for _ in range(6)]
    b = [_emb(0, 1) + rng.normal(0, 0.01, 2) for _ in range(6)]
    embs, groups = _singletons(12, a + b)
    capped = cap_groups(embs, groups, max_groups=3, floor=0.5)
    assert len(capped) == 6  # 3 + 3
    for g in capped:
        assert all((i < 6) == (g[0] < 6) for i in g)


def test_cap_bridge_links_components():
    """桥接碎片（与 A、B 都 ≥ floor）把两个组件链成一个 → 组件内封顶 3，
    允许经桥跨 A/B 合并（单链语义，护栏仅共现与 floor）。"""
    rng = np.random.default_rng(13)
    a = [_emb(1, 0) + rng.normal(0, 0.005, 2) for _ in range(6)]
    b = [_emb(0, 1) + rng.normal(0, 0.005, 2) for _ in range(6)]
    bridge = [_emb(0.71, 0.71)]  # cos ≈ 0.707 与两侧都 ≥ floor
    embs, groups = _singletons(13, a + bridge + b)
    capped = cap_groups(embs, groups, max_groups=3, floor=0.5)
    assert len(capped) == 3  # 单组件 13 → 3
    seen = {i for g in capped for i in g}
    assert seen == set(range(13))
