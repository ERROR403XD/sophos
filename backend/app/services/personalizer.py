"""用户偏好接续训练（M6 实现）。

设计（docs/ARCHITECTURE.md §3.6/§3.7、ADR-008/013）：
- 训练单元：identity 级。特征 x = [mean_embedding(512), base_score/100, rep_quality]。
- 标签：
  - 绝对评分（score 1-10 → (v-1)/9*100；thumbs up→100 / down→10，ADR-008 定稿）；
    同一 identity 取**最新一条**评分（跨 type）。
  - 两两对比（winner/loser），y=±1（ADR-013）。
- 模型：**线性打分头 score(x)=w·x+b**，联合损失 =
    w_abs·MSE(绝对样本) + w_pair·softplus(-y·(s_i-s_j)) + λ‖w‖²
  （pairwise logistic 即 RankNet/Bradley-Terry 式；与绝对评分**共用同一打分头**）。
  全批梯度下降（numpy），样本量小（几十~几千）CPU 毫秒级。
  说明：开发计划原定 sklearn Ridge/MLP，因 pairwise 联合训练需自定义损失，
  改为 numpy 手写线性头（零新增依赖）；非线性模型列为后续可选。
- 版本化：data/models/personalizer/v{n}.npz（w/b/x_mean/x_std）+ v{n}.meta.json
  （n_abs/n_pair/mae/pair_acc/created_at）。
- 发布门槛：总样本（绝对+对比）≥ MIN_TOTAL_SAMPLES(20) 才允许训练；
  指标仅供人工判断，启用（activate）是显式动作。
- 启用：kv.active_pers_model = version；apply 批量写 face_score.personalized_score
  并触发 aggregator.recompute_all（滚动更新）。deactivate 回退基础分。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.db.models import Face, FaceIdentity, FaceScore, KVSetting, PairComparison, UserRating
from app.services import aggregator

ACTIVE_KEY = "active_pers_model"
MIN_TOTAL_SAMPLES = 20
LR = 0.05
ITERS = 1500
L2 = 1e-4


class NotEnoughSamples(RuntimeError):
    pass


# ---------------- 训练数据构建 ----------------

def _rating_to_label(rating_type: str, value: str) -> float:
    if rating_type == "score":
        return (int(value) - 1) / 9.0 * 100.0
    return 100.0 if value == "up" else 10.0  # ADR-008 定稿


def latest_rating_map(session: Session) -> dict[int, tuple[str, str]]:
    """identity -> 最新一条评分（跨 type，按 id 最大）。"""
    rows = session.execute(
        select(UserRating.identity_id, UserRating.rating_type, UserRating.rating_value)
        .order_by(UserRating.id.asc())
    ).all()
    latest: dict[int, tuple[str, str]] = {}
    for iid, t, v in rows:
        latest[iid] = (t, v)  # 升序遍历，后面的覆盖前面的
    return latest


def identity_features(session: Session, identity_ids: list[int] | None = None
                      ) -> dict[int, np.ndarray]:
    """identity -> 特征向量 [embedding(512), base/100, rep_quality]。

    R1(P2)：mean_embedding 为空的 identity（整组无干净样本，fully-occluded）
    不产出特征——既不进训练集，也不写个性化分（其评分仍计入统计与页面展示）。

    R5 大规模（ADR-024）：identity_ids 给定时只加载这些行——训练集是
    评分/对比过的 identity（用户生成，千级），绝不能为训练扫全库加载
    数十万条 512 维 embedding（≈GB 级 IO）；apply 场景则分块传入。
    """
    stmt = (select(FaceIdentity.id, FaceIdentity.mean_embedding,
                   FaceScore.base_score, Face.quality_score)
            .outerjoin(FaceScore, FaceScore.identity_id == FaceIdentity.id)
            .outerjoin(Face, Face.id == FaceIdentity.rep_face_id))
    if identity_ids is not None:
        if not identity_ids:
            return {}
        stmt = stmt.where(FaceIdentity.id.in_(identity_ids))
    feats: dict[int, np.ndarray] = {}
    for iid, emb, base_score, quality in session.execute(stmt).all():
        if emb is None:
            continue  # fully-occluded：无干净样本 embedding，剔除出个性化链路
        base = (base_score if base_score is not None else 50.0) / 100.0
        q = quality if quality is not None else 0.0
        feats[iid] = np.concatenate(
            [np.frombuffer(emb, dtype=np.float32),
             np.array([base, q], dtype=np.float32)])
    return feats


def _training_identity_ids(session: Session) -> list[int]:
    """参与训练的 identity 集 = 有评分的 ∪ 出现在对比中的（R5 大规模口径）。"""
    rated = set(session.execute(select(UserRating.identity_id)).scalars())
    pairs = session.execute(
        select(PairComparison.winner_identity_id, PairComparison.loser_identity_id)).all()
    for w, l in pairs:
        rated.add(w)
        rated.add(l)
    return list(rated)


def build_training_set(session: Session) -> dict:
    """返回 {"abs": [(x, y)], "pairs": [(x_i, x_j, y±1)]}（特征已就绪）。"""
    feats = identity_features(session, _training_identity_ids(session))
    ratings = latest_rating_map(session)

    abs_samples = []
    for iid, (t, v) in ratings.items():
        if iid in feats:
            abs_samples.append((feats[iid], _rating_to_label(t, v)))

    pair_samples = []
    for w, l in session.execute(
            select(PairComparison.winner_identity_id, PairComparison.loser_identity_id)).all():
        if w in feats and l in feats:
            pair_samples.append((feats[w], feats[l], 1.0))

    return {"abs": abs_samples, "pairs": pair_samples}


# ---------------- 训练 ----------------

def _standardize_params(pooled: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    mean = pooled.mean(axis=0)
    std = pooled.std(axis=0)
    return mean.astype(np.float32), np.maximum(std, 1e-6).astype(np.float32)


def _pair_acc(w, b, pairs):
    if not pairs:
        return None
    correct = 0
    for xi, xj, y in pairs:
        correct += 1 if ((w @ xi + b) - (w @ xj + b)) * y > 0 else 0
    return round(correct / len(pairs), 4)


def train(session: Session, models_dir: Path) -> dict:
    """训练新版本并落盘；返回 meta dict（不自动启用）。"""
    data = build_training_set(session)
    n_abs, n_pair = len(data["abs"]), len(data["pairs"])
    total = n_abs + n_pair
    if total < MIN_TOTAL_SAMPLES:
        raise NotEnoughSamples(
            f"not enough samples: {total} (abs={n_abs}, pair={n_pair}) "
            f"< {MIN_TOTAL_SAMPLES}; keep rating/comparing faces first")

    pooled = np.stack([x for x, _ in data["abs"]] +
                      [xi for xi, _, _ in data["pairs"]] +
                      [xj for _, xj, _ in data["pairs"]])
    x_mean, x_std = _standardize_params(pooled)
    norm = lambda x: (x - x_mean) / x_std  # noqa: E731

    dim = pooled.shape[1]
    rng = np.random.default_rng(42)
    w = rng.normal(0, 0.01, dim).astype(np.float32)
    b = 50.0

    abs_X = np.stack([norm(x) for x, _ in data["abs"]]) if n_abs else None
    abs_y = np.array([y for _, y in data["abs"]], dtype=np.float32) if n_abs else None
    w_abs = n_abs / total if n_abs else 0.0
    w_pair = n_pair / total if n_pair else 0.0

    # R5 大规模（ADR-024）：pair 梯度向量化——原逐对 Python 循环每轮重算
    # 归一化，千级 pair × 1500 轮是分钟级热点；预归一化 + 矩阵化后秒级。
    pair_D = pair_Y = None
    if n_pair:
        pair_D = np.stack(
            [norm(xi) - norm(xj) for xi, xj, _ in data["pairs"]]).astype(np.float32)
        pair_Y = np.array([y for _, _, y in data["pairs"]], dtype=np.float32)

    for _ in range(ITERS):
        grad_w = np.zeros(dim, dtype=np.float32) + L2 * w
        grad_b = 0.0
        if n_abs:
            resid = abs_X @ w + b - abs_y
            grad_w += w_abs * (2.0 / n_abs) * (abs_X.T @ resid)
            grad_b += w_abs * (2.0 / n_abs) * float(resid.sum())
        if n_pair:
            # softplus(-y·(s_i-s_j)) 的梯度（矩阵化，等价于原逐对形式）；
            # d 裁剪到 ±30：softplus 梯度在 |d|>30 已饱和为 0，防 exp 溢出
            d = np.clip(pair_Y * (pair_D @ w), -30.0, 30.0)
            coef = w_pair / (n_pair * (1.0 + np.exp(d)))  # d/ds softplus(-d)
            cw = (coef * pair_Y).astype(np.float32)
            grad_w += cw @ pair_D
            grad_b += float(cw.sum())
        w -= LR * grad_w
        b -= LR * grad_b

    mae = r2 = None
    if n_abs:
        pred = abs_X @ w + b
        mae = round(float(np.abs(pred - abs_y).mean()), 2)
        ss_res = float(((pred - abs_y) ** 2).sum())
        ss_tot = float(((abs_y - abs_y.mean()) ** 2).sum())
        r2 = round(1 - ss_res / ss_tot, 4) if ss_tot > 1e-9 else None
    pair_acc = _pair_acc(w, b, [(norm(xi), norm(xj), y) for xi, xj, y in data["pairs"]])

    out_dir = Path(models_dir) / "personalizer"
    out_dir.mkdir(parents=True, exist_ok=True)
    existing = [int(p.stem[1:]) for p in out_dir.glob("v*.npz")]
    version = f"v{max(existing) + 1 if existing else 1}"

    np.savez(out_dir / f"{version}.npz", w=w, b=np.float32(b), x_mean=x_mean, x_std=x_std)
    meta = {
        "version": version, "n_abs": n_abs, "n_pair": n_pair,
        "mae_train": mae, "r2_train": r2, "pair_acc": pair_acc,
        "feature_dim": int(dim),
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    (out_dir / f"{version}.meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    return meta


# ---------------- 应用与启用 ----------------

APPLY_CHUNK = 2000  # R5：apply 分块大小（内存与单事务规模钳制，网络盘大事务易超时）


def _load(models_dir: Path, version: str) -> dict:
    p = Path(models_dir) / "personalizer" / f"{version}.npz"
    if not p.is_file():
        raise FileNotFoundError(f"personalizer version not found: {version}")
    z = np.load(p)
    return {"w": z["w"], "b": float(z["b"]), "x_mean": z["x_mean"], "x_std": z["x_std"]}


def apply_version(session: Session, models_dir: Path, version: str) -> int:
    """对全部 identity 写 personalized_score；返回更新条数。

    R5 大规模（ADR-024）：原实现一次性加载全库特征（数十万 × 514 维 =
    GB 级）+ 逐行 SELECT + 单事务提交，大库下不可行。改为 keyset 分块
    （id 递增翻页）：每块向量化打分 + SQLite UPSERT 批量写 + 立即提交；
    已是当前版本的行跳过（重复 apply 幂等且近零开销）。

    R6：块间 time.sleep(0.02) 留出读窗口——回滚日志模式（SMB 上无 WAL）
    写提交的 EXCLUSIVE 锁会挤占读请求；连续块提交曾让播放/浏览在应用
    模型期间近乎失去响应（用户实测）。代价是 apply 全程约多几秒。
    """
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert

    m = _load(models_dir, version)
    updated = 0
    last_id = 0
    while True:
        rows = session.execute(
            select(FaceIdentity.id, FaceIdentity.mean_embedding,
                   FaceScore.base_score, Face.quality_score,
                   FaceScore.pers_model_version)
            .outerjoin(FaceScore, FaceScore.identity_id == FaceIdentity.id)
            .outerjoin(Face, Face.id == FaceIdentity.rep_face_id)
            .where(FaceIdentity.id > last_id)
            .order_by(FaceIdentity.id)
            .limit(APPLY_CHUNK)).all()
        if not rows:
            break
        last_id = rows[-1][0]
        payload = []
        for iid, emb, base_score, quality, cur_ver in rows:
            if emb is None or cur_ver == version:
                continue  # fully-occluded / 已是当前版本
            base = (base_score if base_score is not None else 50.0) / 100.0
            q = quality if quality is not None else 0.0
            x = np.concatenate([np.frombuffer(emb, dtype=np.float32),
                                np.array([base, q], dtype=np.float32)])
            nx = (x - m["x_mean"]) / m["x_std"]
            score = float(np.clip(m["w"] @ nx + m["b"], 0.0, 100.0))
            payload.append({"identity_id": iid,
                            "personalized_score": round(score, 2),
                            "pers_model_version": version})
        if payload:
            stmt = sqlite_insert(FaceScore).values(payload)
            stmt = stmt.on_conflict_do_update(
                index_elements=[FaceScore.identity_id],
                set_={"personalized_score": stmt.excluded.personalized_score,
                      "pers_model_version": stmt.excluded.pers_model_version})
            session.execute(stmt)
            session.commit()
            updated += len(payload)
            time.sleep(0.02)  # R6：写锁间歇，给在线读请求让出窗口
    return updated


def active_version(session: Session) -> str | None:
    row = session.get(KVSetting, ACTIVE_KEY)
    if row is None:
        return None
    try:
        return json.loads(row.value)
    except ValueError:
        return None


def activate(session: Session, models_dir: Path, version: str) -> dict:
    apply_version(session, models_dir, version)  # 先落分
    from app.services.kv import set_kv
    set_kv(session, ACTIVE_KEY, version)
    n = aggregator.recompute_all(session)
    return {"version": version, "videos_recomputed": n}


def deactivate(session: Session) -> int:
    from app.services.kv import set_kv
    set_kv(session, ACTIVE_KEY, None)
    session.execute(
        update(FaceScore).values(personalized_score=None, pers_model_version=None)
    )
    session.commit()
    return aggregator.recompute_all(session)


def list_versions(models_dir: Path) -> list[dict]:
    out_dir = Path(models_dir) / "personalizer"
    versions = []
    for meta_path in sorted(out_dir.glob("v*.meta.json")):
        try:
            versions.append(json.loads(meta_path.read_text(encoding="utf-8")))
        except (ValueError, OSError):
            continue
    return list(reversed(versions))  # 新版本在前
