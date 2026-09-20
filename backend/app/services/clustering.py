"""视频内人脸聚类（M3 实现；R4 强化合并 pass，ADR-018）。

设计（docs/ARCHITECTURE.md §3.2、ADR-007/018）：
- 单位：同一 video 内的女性人脸 embedding（MVP 不做跨视频合并，R4 重申不做）
- 算法：Chinese Whispers（余弦相似度 ≥ 阈值建边，随机序迭代分配邻居主流标签）；
  样本量小（单视频人脸数通常 <10³），实现简单、不过度链接（优于单链 union-find）
- 产出：分组（每组为样本下标），由调用方写 face_identity 并选代表帧
  （组内 quality_score 最高样本）
- R4：merge pass 双层判定（高置信直接合并 / 中等相似谨慎合并）+ 同帧共现
  一票否决（PLAN_v1.0.4 §2-§6）
- R5：cap_groups 面容数上限 pass（同一视频同一人最多保留 N 张，ADR-023）
"""
from __future__ import annotations

import random

import numpy as np


def cosine_matrix(embs: np.ndarray) -> np.ndarray:
    """L2 归一化向量的余弦相似度矩阵（clip 到 [0,1] 只留正相似）。"""
    e = embs / np.maximum(1e-9, np.linalg.norm(embs, axis=1, keepdims=True))
    sim = e @ e.T
    return np.clip(sim, 0.0, 1.0)


def cluster_embeddings(embs: np.ndarray, threshold: float = 0.68,
                       iterations: int = 5, seed: int = 42) -> list[list[int]]:
    """返回分组列表（每组为 embs 的行下标）；单样本自成一组。"""
    n = len(embs)
    if n == 0:
        return []
    labels = list(range(n))
    if n == 1:
        return [labels]
    sim = cosine_matrix(np.asarray(embs, dtype=np.float32))
    neighbors = [np.where(sim[i] >= threshold)[0].tolist() for i in range(n)]
    rng = random.Random(seed)
    for _ in range(iterations):
        order = list(range(n))
        rng.shuffle(order)
        changed = False
        for i in order:
            nb = [j for j in neighbors[i] if j != i]
            if not nb:
                continue
            counts: dict[int, int] = {}
            for j in nb:
                counts[labels[j]] = counts.get(labels[j], 0) + 1
            best = max(counts.items(), key=lambda kv: (kv[1], -kv[0]))[0]
            if best != labels[i]:
                labels[i] = best
                changed = True
        if not changed:
            break
    groups: dict[int, list[int]] = {}
    for i, lb in enumerate(labels):
        groups.setdefault(lb, []).append(i)
    return [sorted(g) for g in groups.values()]


def _cooccurs(ts_a: np.ndarray, ts_b: np.ndarray, tolerance: float) -> bool:
    """两组样本是否存在同帧共现（任意样本时间差 ≤ tolerance）。

    R4(P1)：同一时刻同框的两组 face 几乎必为两个不同的人——共现是禁止合并的
    一票否决证据，优先级高于任何 embedding 相似度（PLAN_v1.0.4 §5）。
    """
    for ta in ts_a:
        for tb in ts_b:
            if abs(float(ta) - float(tb)) <= tolerance:
                return True
    return False


def merge_groups(embs: np.ndarray, groups: list[list[int]], threshold: float = 0.78,
                 max_rounds: int = 10, review_threshold: float = 0.0,
                 cooccur_tolerance: float = 0.5,
                 sample_clean=None, sample_timestamps=None,
                 singleton_threshold: float = 0.0) -> list[list[int]]:
    """聚类后合并 pass（R1/P3 引入，R4 强化为双层判定，ADR-018；R6 第三层）。

    第一层 高置信直接合并：组间全样本 mean_embedding 余弦 ≥ threshold
    （沿用 R1 行为，默认 0.78）。

    第二层 中等相似谨慎合并（review_threshold=0 时关闭）：
    review_threshold ≤ 余弦 < threshold，且同时满足辅助条件才合并：
    - 两组都 ≥2 个干净样本（非遮挡，"多个稳定样本"）；
    - 两组的干净样本均值余弦也 ≥ review_threshold（均值由非遮挡样本产生）；
    - 两组无同帧共现（一票否决，与相似度无关）。
    原则（PLAN §3）：宁可留下少量重复 identity，不能把不同的人合成一个人。
    性别一致性不在此重复校验——pipeline 中 merge 恒在 identity 级性别裁决之后。

    第三层 单样本同人合并（R6，ADR-028；singleton_threshold=0 时关闭）：
    双方**均只有一个样本**、余弦 ∈ [singleton_threshold, threshold)、无同帧
    共现 → 合并。R4 实测基线 ~78% identity 为单样本，而谨慎层要求双方 ≥2
    干净样本，单样本碎片永远合不上——这是"同一个人被识别成不同的人"的
    主因（用户复检反馈）。阈值取 0.74（高于谨慎层下限 0.72：单样本无投票
    修正，证据弱一档须更严）；共现否决同样一票否决。

    - 只在组均值级贪心合并（非样本链式）；每轮取可合并对中相似度最高的一对
      （样本并集），合并后组均值变化 → 重算，迭代至无合并；
    - sample_clean / sample_timestamps：与 groups 下标对齐的样本级数组
      （干净掩码 / 时间戳秒），由 pipeline 从 FaceSample 提供；单测可省略
      （省略时视为全部干净、无时间戳=无共现证据）。
    """
    merged = [list(g) for g in groups]
    if len(merged) <= 1:
        return merged
    E = np.asarray(embs, dtype=np.float32)
    clean_of = [g if sample_clean is None else
                [i for i in g if sample_clean[i]] for g in merged]
    ts_of = ([np.asarray([sample_timestamps[i] for i in g])
              if sample_timestamps is not None else None for g in merged])
    for _ in range(max_rounds):
        means = np.stack([E[g].mean(axis=0) for g in merged])
        sim = cosine_matrix(means)
        n = len(merged)
        best_i, best_j, best_s = -1, -1, -1.0
        for i in range(n):
            for j in range(i + 1, n):
                s = float(sim[i, j])
                if s < threshold:
                    both_single = len(merged[i]) == 1 and len(merged[j]) == 1
                    layer3 = (singleton_threshold > 0 and both_single
                              and s >= singleton_threshold)
                    layer2 = (review_threshold > 0
                              and review_threshold <= s < threshold
                              and not both_single)
                    if not (layer2 or layer3):
                        continue
                if (ts_of[i] is not None
                        and _cooccurs(ts_of[i], ts_of[j], cooccur_tolerance)):
                    continue  # 同帧共现 → 禁止合并（即使 embedding 很高）
                if s < threshold and review_threshold <= s < threshold \
                        and not (len(merged[i]) == 1 and len(merged[j]) == 1):
                    # 谨慎层（第二层）辅助校验：双方都有多个稳定干净样本，
                    # 且干净样本均值也过线
                    ci, cj = clean_of[i], clean_of[j]
                    if len(ci) < 2 or len(cj) < 2:
                        continue
                    mi, mj = E[ci].mean(axis=0), E[cj].mean(axis=0)
                    denom = float(np.linalg.norm(mi) * np.linalg.norm(mj))
                    if denom < 1e-9 or float(mi @ mj) / denom < review_threshold:
                        continue
                if s > best_s:
                    best_s, best_i, best_j = s, i, j
        if best_i < 0:
            break
        merged[best_i] = merged[best_i] + merged[best_j]
        del merged[best_j]
        # 合并后重算两层的辅助信息（clean 下标 / 时间戳随组并集）
        clean_of[best_i] = (merged[best_i] if sample_clean is None else
                            [i for i in merged[best_i] if sample_clean[i]])
        ts_of[best_i] = (np.asarray([sample_timestamps[i] for i in merged[best_i]])
                         if sample_timestamps is not None else None)
        del clean_of[best_j]
        del ts_of[best_j]
    return [sorted(g) for g in merged]


def cap_groups(embs: np.ndarray, groups: list[list[int]], max_groups: int,
               floor: float = 0.50, cooccur_tolerance: float = 0.5,
               sample_timestamps=None) -> list[list[int]]:
    """面容数上限 pass（R5，ADR-023）：每个"可能同人组件"内封顶 max_groups 组。

    需求："同一视频文件的同一个人，最多保留 N 张面容"。人物真值不可得，
    先以余弦 ≥ floor（默认 0.50，介于 diverse"明显不同 0.40"与"很像 0.60"
    之间）做单链连通分量 = "可能同一个人"的组件（共现对**不建边**——同框
    必为两人），再对每个组件独立执行：
    - 反复合并组件内当前相似度最高、且无同帧共现、且 ≥ floor 的一对，
      直到组件内组数 ≤ max_groups；
    - 无可合并对（全部被共现否决或低于 floor）→ 停止，宁超限不误并。

    组件化保证多个不同人共存的视频各封各的顶，不会为凑总数把不同人强并
    （上限=5 时尤其关键）。护栏与 merge pass 同口径；max_groups ≤ 0 表示
    不限制。返回组内样本下标升序；时间戳数组与 groups 下标对齐（单测可省略）。
    """
    capped = [list(g) for g in groups]
    if max_groups is None or max_groups <= 0 or len(capped) <= max_groups:
        return capped
    E = np.asarray(embs, dtype=np.float32)
    ts_of = ([np.asarray([sample_timestamps[i] for i in g])
              if sample_timestamps is not None else None for g in capped])

    def _means_mat() -> np.ndarray:
        return np.stack([E[g].mean(axis=0) for g in capped])

    # 1) 单链连通分量（sim ≥ floor 且无共现才建边）——"可能同一个人"的候选域
    means = _means_mat()
    sim = cosine_matrix(means)
    n = len(capped)
    parent = list(range(n))

    def _find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i in range(n):
        for j in range(i + 1, n):
            if sim[i, j] < floor:
                continue
            if (ts_of[i] is not None
                    and _cooccurs(ts_of[i], ts_of[j], cooccur_tolerance)):
                continue
            ri, rj = _find(i), _find(j)
            if ri != rj:
                parent[ri] = rj
    components: dict[int, list[int]] = {}
    for i in range(n):
        components.setdefault(_find(i), []).append(i)

    # 2) 组件内封顶：每轮合并该组件内最优可并对（全局重算均值，组数小）
    for members in components.values():
        while len(members) > max_groups:
            means = _means_mat()
            sim = cosine_matrix(means)
            best_i, best_j, best_s = -1, -1, -1.0
            for ii in range(len(members)):
                for jj in range(ii + 1, len(members)):
                    i, j = members[ii], members[jj]
                    s = float(sim[i, j])
                    if s < floor or s <= best_s:
                        continue
                    if (ts_of[i] is not None
                            and _cooccurs(ts_of[i], ts_of[j], cooccur_tolerance)):
                        continue
                    best_s, best_i, best_j = s, i, j
            if best_i < 0:
                break  # 组件内无可强并对：宁可超限也不误并
            capped[best_i] = capped[best_i] + capped[best_j]
            ts_of[best_i] = (np.asarray([sample_timestamps[i] for i in capped[best_i]])
                             if sample_timestamps is not None else None)
            members.remove(best_j)
    kept = sorted(m for members in components.values() for m in members)
    return [sorted(capped[i]) for i in kept]
