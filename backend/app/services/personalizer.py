"""用户偏好接续训练（M6 实现）。

设计（docs/ARCHITECTURE.md §3.6/§3.7、ADR-008/013）：
- 训练单元：identity 级。特征 x = [mean_embedding(512), base_score/100, rep_quality]。
- 标签：
  - 绝对评分（score 1-10 → (v-1)/9*100；thumbs up→100 / down→10，ADR-008 定稿）；
    同一 identity 取**最新一条**评分（跨 type）。
  - 两两对比（winner/loser），y=±1（ADR-013）。
- 模型：**线性打分头 z(x)=w·x+b**，联合损失 =
    w_abs·MSE(绝对样本) + w_pair·softplus(-y·(s_i-s_j)) + λ‖w‖²
  （pairwise logistic 即 RankNet/Bradley-Terry 式；与绝对评分**共用同一打分头**）。
  全批梯度下降（numpy），样本量小（几十~几千）CPU 秒级。
- **分数去饱和（R14/ADR-035；R14.2 定稿为百分位映射）**：pairwise 无界拉开 +
  512 维 embedding 对未评分人脸的外推，使原始 z 可达 ±数百（实测 ||w||≈20 时
  raw∈[-840,-41]）——任何固定尺度的有界映射都只能把并列点挪个位置
  （clip(0,100) → 大片 0/100；软饱和 tanh((z-50)/50) → 大片 99.99，用户复测证实）。
  定稿方案 = apply 时对全库 raw 做**排名百分位**映射：score = 0.01 + rank/(n-1)·99.98
  （4 位小数）——分布无关、保序、铺满整个区间，从数学上排除"大片并列"；
  语义变为"库内偏好百分位"（越高 = 越合口味），随库规模滚动变化属预期。
  曾试验 ①tanh 有界训练头（ds/dz≤50 放大梯度，第一步即饱和冻结，否决）
  ②软饱和 squash（并列点位移，治标，否决）；训练端保持原线性头。
  说明：开发计划原定 sklearn Ridge/MLP，因 pairwise 联合训练需自定义损失，
  改为 numpy 手写头（零新增依赖）；非线性模型列为后续可选。
- 版本化：data/models/personalizer/v{n}.npz（w/b/x_mean/x_std）+ v{n}.meta.json
  （n_abs/n_pair/mae/pair_acc/created_at）。
- 发布门槛：总样本（绝对+对比）≥ MIN_TOTAL_SAMPLES(20) 才允许训练；
  指标仅供人工判断，启用（activate）是显式动作。
- 启用：kv.active_pers_model = version；apply 批量写 face_score.personalized_score
  并触发 aggregator.recompute_all（滚动更新）。deactivate 回退基础分。
- 导入/导出（R11）：单个版本导出为一个 zip（{version}.npz + meta.json，几 KB，
  只含用户偏好本体，不含评分记录/面容库/DB）；导入校验后落盘为新版本，
  启用仍是显式动作（与训练后语义一致）。
"""
from __future__ import annotations

import io
import json
import math
import re
import time
import zipfile
from pathlib import Path

import numpy as np
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.db.models import Face, FaceIdentity, FaceScore, KVSetting, PairComparison, UserRating
from app.services import aggregator

ACTIVE_KEY = "active_pers_model"
MIN_TOTAL_SAMPLES = 20
# R14/ADR-035：LR 0.05→0.01 + 梯度范数裁剪。0.05 在小型库上会振荡发散
# （resid 50→253→1080→…，真实库 v1 的 mae=NaN 即此因；大库碰巧收敛）；
# 裁剪保证任何数据下步长有界。ITERS 1500→3000 补偿步长变小（仍秒级）。
LR = 0.01
ITERS = 3000
L2 = 1e-4
GRAD_CLIP_NORM = 2.0

# 特征布局 [embedding(512), base/100, rep_quality] 的总维度。导入包校验用：
# 维度不符 = 导出方与本实例的特征定义不同（跨大版本），启用必然写坏分，
# 必须在导入时就拒绝（训练特征布局变更时须同步改这里并考虑旧包兼容）。
FEATURE_DIM = 514

# 版本号口径：v{正整数}。所有以 version 拼接文件名的入口都先过此校验（防穿越），
# _load 在 activate 链路上，export/import 独立校验。
_VERSION_RE = re.compile(r"v\d+\Z")
_IMPORT_MAX_BYTES = 64 * 1024 * 1024  # 导入包防呆上限（真实模型 npz ≈ 8KB）


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

    def norm(x):
        # R14/ADR-035：标准化后按维裁剪 ±10。训练池某维近乎常数时 x_std 触底
        # 1e-6，库内其他人脸该维会被放大 ±1e6——一步梯度即数值发散（真实库
        # v1 的 mae=NaN 即此因），溢出后整库分数变 NaN。裁剪既稳住训练，
        # 也限制外推幅度（配合 apply 端软饱和）。
        return np.clip((x - x_mean) / x_std, -10.0, 10.0)

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
        # R14：梯度范数裁剪（w/b 联合）——病态数据（小库/近常数维）下
        # MSE 梯度爆发会振荡发散，裁剪后步长恒有界，收敛性数据无关。
        gnorm = float(np.sqrt(np.float64(grad_w @ grad_w) + grad_b * grad_b))
        if gnorm > GRAD_CLIP_NORM:
            k = np.float32(GRAD_CLIP_NORM / gnorm)
            grad_w *= k
            grad_b *= k
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
    # 目录里混入非数字尾缀的文件（手工备份等）时跳过而不是让训练崩溃
    existing = [int(p.stem[1:]) for p in out_dir.glob("v*.npz")
                if p.stem[1:].isdigit()]
    version = f"v{max(existing) + 1 if existing else 1}"

    np.savez(out_dir / f"{version}.npz", w=w, b=np.float32(b), x_mean=x_mean, x_std=x_std)
    meta = {
        "version": version, "n_abs": n_abs, "n_pair": n_pair,
        "mae_train": mae, "r2_train": r2, "pair_acc": pair_acc,
        "feature_dim": int(dim), "output": "linear",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    (out_dir / f"{version}.meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    return meta


# ---------------- 应用与启用 ----------------

APPLY_CHUNK = 2000  # R5：apply 分块大小（内存与单事务规模钳制，网络盘大事务易超时）


def _load(models_dir: Path, version: str) -> dict:
    if _VERSION_RE.fullmatch(version) is None:
        raise FileNotFoundError(f"personalizer version not found: {version}")
    p = Path(models_dir) / "personalizer" / f"{version}.npz"
    if not p.is_file():
        raise FileNotFoundError(f"personalizer version not found: {version}")
    z = np.load(p)
    # 输出头类型（R14/ADR-035）：tanh50 = 有界头（z 即 tanh 自变量）；
    # 缺省 linear = 旧线性头（raw 即 0-100 分数，apply 时软饱和去重）。
    output = "linear"
    meta_p = Path(models_dir) / "personalizer" / f"{version}.meta.json"
    if meta_p.is_file():
        try:
            output = str(json.loads(meta_p.read_text(encoding="utf-8"))
                         .get("output", "linear"))
        except (ValueError, OSError):
            pass
    return {"w": z["w"], "b": float(z["b"]), "x_mean": z["x_mean"], "x_std": z["x_std"],
            "output": output}


def apply_version(session: Session, models_dir: Path, version: str,
                  progress_cb=None) -> int:
    """对全部 identity 写 personalized_score；返回更新条数。

    R14：progress_cb(done, total)——total 为全库 identity 数（含无 embedding
    的跳过行），done 为已扫描行数；activate 任务据此展示细粒度进度。

    R5 大规模（ADR-024）：原实现一次性加载全库特征（数十万 × 514 维 =
    GB 级）+ 逐行 SELECT + 单事务提交，大库下不可行。改为 keyset 分块
    （id 递增翻页）：每块向量化打分 + SQLite UPSERT 批量写 + 立即提交；
    已是当前版本的行跳过（重复 apply 幂等且近零开销）。

    R6：块间 time.sleep(0.02) 留出读窗口——回滚日志模式（SMB 上无 WAL）
    写提交的 EXCLUSIVE 锁会挤占读请求；连续块提交曾让播放/浏览在应用
    模型期间近乎失去响应（用户实测）。代价是 apply 全程约多几秒。

    R14.1 修复：**移除"已是当前版本则跳过"的幂等优化**。R5 引入它时评分
    语义固定；R14 给 apply 加了去饱和——已 stamped 旧语义分数的行
    （如并列的 100.0）会被跳过而永久保留（用户重新部署+重新启用后仍见
    大片 100 的根因）。activate 已任务化（进度可见），无条件全量重算的
    成本可接受，正确性优先。

    R14.2：**两遍式 + 百分位映射**。第一遍分块扫描全库算 raw（不写库），
    第二遍按库内排名写入百分位分——百分位需要全局分布，必须两遍。
    progress_cb(done, total)：total = 2×identity 数（扫描 + 写入两相）。
    """
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert

    m = _load(models_dir, version)
    total = session.execute(
        select(func.count()).select_from(FaceIdentity)).scalar_one()
    grand_total = total * 2  # 扫描 + 写入两相
    last_id = 0
    scanned = 0
    ids: list[int] = []
    raws: list[float] = []
    if progress_cb:
        progress_cb(0, grand_total)

    # ---- 第一遍：分块扫描全库，计算 raw（不写库）----
    while True:
        rows = session.execute(
            select(FaceIdentity.id, FaceIdentity.mean_embedding,
                   FaceScore.base_score, Face.quality_score)
            .outerjoin(FaceScore, FaceScore.identity_id == FaceIdentity.id)
            .outerjoin(Face, Face.id == FaceIdentity.rep_face_id)
            .where(FaceIdentity.id > last_id)
            .order_by(FaceIdentity.id)
            .limit(APPLY_CHUNK)).all()
        if not rows:
            break
        last_id = rows[-1][0]
        for iid, emb, base_score, quality in rows:
            if emb is None:
                continue  # fully-occluded：无干净样本 embedding，剔出个性化链路
            base = (base_score if base_score is not None else 50.0) / 100.0
            q = quality if quality is not None else 0.0
            x = np.concatenate([np.frombuffer(emb, dtype=np.float32),
                                np.array([base, q], dtype=np.float32)])
            nx = np.clip((x - m["x_mean"]) / m["x_std"], -10.0, 10.0)
            ids.append(iid)
            raws.append(float(m["w"] @ nx + m["b"]))
        scanned += len(rows)
        if progress_cb:
            progress_cb(min(scanned, total), grand_total)

    # ---- 百分位映射：score = 0.01 + rank/(n-1)·99.98（R14.2/ADR-035）----
    # 分布无关、保序；raw 并列时按 stable argsort 的 id 顺序稳定打破。
    n = len(ids)
    if n == 0:
        return 0
    raw_arr = np.asarray(raws, dtype=np.float64)
    order = np.argsort(raw_arr, kind="stable")
    rank = np.empty(n, dtype=np.float64)
    rank[order] = np.arange(n, dtype=np.float64)
    frac = rank / (n - 1) if n > 1 else np.full(n, 0.5)
    scores = np.round(0.01 + frac * 99.98, 4)

    # ---- 第二遍：分块 UPSERT（id 升序 = 扫描序，直接切片）----
    updated = 0
    for start in range(0, n, APPLY_CHUNK):
        chunk_ids = ids[start:start + APPLY_CHUNK]
        chunk_scores = scores[start:start + APPLY_CHUNK]
        payload = [{"identity_id": iid,
                    "personalized_score": float(sc),
                    "pers_model_version": version}
                   for iid, sc in zip(chunk_ids, chunk_scores)]
        stmt = sqlite_insert(FaceScore).values(payload)
        stmt = stmt.on_conflict_do_update(
            index_elements=[FaceScore.identity_id],
            set_={"personalized_score": stmt.excluded.personalized_score,
                  "pers_model_version": stmt.excluded.pers_model_version})
        session.execute(stmt)
        session.commit()
        updated += len(payload)
        if progress_cb:
            progress_cb(total + min(updated, total), grand_total)
        time.sleep(0.02)  # R6：写锁间歇，给在线读请求让出窗口
    if progress_cb:
        progress_cb(grand_total, grand_total)
    return updated


def active_version(session: Session) -> str | None:
    row = session.get(KVSetting, ACTIVE_KEY)
    if row is None:
        return None
    try:
        return json.loads(row.value)
    except ValueError:
        return None


def activate(session: Session, models_dir: Path, version: str,
             apply_progress=None, aggregate_progress=None) -> dict:
    """启用版本（R14）：apply 与聚合两相各自的进度回调（activate 任务用）。"""
    from app.services.kv import set_kv

    previous = active_version(session)
    try:
        apply_version(session, models_dir, version, progress_cb=apply_progress)
        set_kv(session, ACTIVE_KEY, version)
        n = aggregator.recompute_all(session, progress_cb=aggregate_progress)
    except Exception:
        session.rollback()
        if previous == version:
            raise
        try:
            if previous is None:
                session.execute(update(FaceScore).values(
                    personalized_score=None, pers_model_version=None
                ).where(FaceScore.pers_model_version == version))
                session.commit()
                set_kv(session, ACTIVE_KEY, None)
            else:
                apply_version(session, models_dir, previous)
                set_kv(session, ACTIVE_KEY, previous)
            aggregator.recompute_all(session)
        except Exception as restore_error:
            raise RuntimeError("activation failed and rollback was incomplete") from restore_error
        raise
    return {"version": version, "videos_recomputed": n}


def deactivate(session: Session, aggregate_progress=None) -> int:
    from app.services.kv import set_kv
    set_kv(session, ACTIVE_KEY, None)
    session.execute(
        update(FaceScore).values(personalized_score=None, pers_model_version=None)
    )
    session.commit()
    return aggregator.recompute_all(session, progress_cb=aggregate_progress)


def _json_safe(obj):
    """NaN/Inf → None：极端训练可能产出 NaN 指标（真实库 v1 即如此），而
    Starlette JSONResponse 是 allow_nan=False——带着 NaN 的 meta 会让
    GET /train/versions 直接 500。API 边界与导出包统一清洗。"""
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_json_safe(v) for v in obj]
    return obj


# ---------------- 导入/导出（R11） ----------------

def export_version(models_dir: Path, version: str) -> tuple[bytes, str]:
    """导出一个版本为单个 zip（{version}.npz + {version}.meta.json）。

    轻量语义：用户偏好本体就是这个线性打分头 + 训练指标（几 KB），不含
    评分记录/面容库/DB——换机迁移偏好只需要这一个文件。返回 (zip字节, 文件名)。
    """
    if _VERSION_RE.fullmatch(version) is None:
        raise FileNotFoundError(f"personalizer version not found: {version}")
    out_dir = Path(models_dir) / "personalizer"
    npz_path = out_dir / f"{version}.npz"
    meta_path = out_dir / f"{version}.meta.json"
    if not (npz_path.is_file() and meta_path.is_file()):
        raise FileNotFoundError(f"personalizer version not found: {version}")
    try:
        meta = _json_safe(json.loads(meta_path.read_text(encoding="utf-8")))
    except ValueError as exc:
        raise ValueError(f"corrupt meta for {version}: {exc}") from exc
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(npz_path.name, npz_path.read_bytes())
        zf.writestr(meta_path.name, json.dumps(meta, ensure_ascii=False, indent=1))
    return buf.getvalue(), f"sophos-personalizer-{version}.zip"


def import_version(models_dir: Path, payload: bytes) -> dict:
    """导入导出包，落盘为新版本；返回新 meta。**不自动启用**（启用是显式动作）。

    校验：zip 结构、npz 数组齐全（w/b/x_mean/x_std）、数组间维度一致、
    特征维度 == FEATURE_DIM（跨特征定义的旧包拒绝，防启用后写坏分）、
    meta 含合法 version 字段。版本号沿用包内原号，与本库已有版本冲突时
    重编号为 max+1（meta.version 同步改写，其余训练指标原样保留）。
    """
    if len(payload) > _IMPORT_MAX_BYTES:
        raise ValueError("export archive too large")
    try:
        zf = zipfile.ZipFile(io.BytesIO(payload))
    except zipfile.BadZipFile as exc:
        raise ValueError("not a valid Sophos personalizer export (zip)") from exc
    npz_name = next((n for n in zf.namelist() if n.endswith(".npz")), None)
    meta_name = next((n for n in zf.namelist() if n.endswith(".meta.json")), None)
    if npz_name is None or meta_name is None:
        raise ValueError("export archive must contain a .npz and a .meta.json")
    try:
        meta = json.loads(zf.read(meta_name).decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError("corrupt meta.json in export archive") from exc
    if not isinstance(meta, dict):
        raise ValueError("corrupt meta.json in export archive")
    try:
        z = np.load(io.BytesIO(zf.read(npz_name)))
        w, x_mean, x_std = np.asarray(z["w"]), np.asarray(z["x_mean"]), np.asarray(z["x_std"])
        b = float(z["b"])
    except (zipfile.BadZipFile, KeyError, ValueError, TypeError) as exc:
        raise ValueError(
            "model archive is missing required arrays (w/b/x_mean/x_std)") from exc
    dim = int(w.shape[0]) if w.ndim == 1 else 0
    if dim <= 0 or x_mean.shape != (dim,) or x_std.shape != (dim,):
        raise ValueError("model arrays are inconsistent (w/x_mean/x_std)")
    if dim != FEATURE_DIM:
        raise ValueError(
            f"feature dimension mismatch: archive has {dim}, this Sophos expects "
            f"{FEATURE_DIM} (export likely from a version with a different "
            f"feature layout)")
    if _VERSION_RE.fullmatch(str(meta.get("version") or "")) is None:
        raise ValueError("meta.json is missing a valid 'version' field")

    out_dir = Path(models_dir) / "personalizer"
    out_dir.mkdir(parents=True, exist_ok=True)
    original = str(meta["version"])
    taken = {p.stem for p in out_dir.glob("v*.npz")}
    version = original if original not in taken else f"v{max((int(s[1:]) for s in taken), default=0) + 1}"
    meta = {**meta, "version": version}
    np.savez(out_dir / f"{version}.npz", w=w, b=np.float32(b),
             x_mean=x_mean, x_std=x_std)
    (out_dir / f"{version}.meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    return meta


def version_exists(models_dir: Path, version: str) -> bool:
    """版本是否存在（R14：activate 任务化后 API 层的同步预检）。"""
    if _VERSION_RE.fullmatch(version) is None:
        return False
    return (Path(models_dir) / "personalizer" / f"{version}.npz").is_file()


def list_versions(models_dir: Path) -> list[dict]:
    out_dir = Path(models_dir) / "personalizer"
    versions = []
    # R14.3 修复：按**数字**版本号排序（旧实现按文件名字典序——v10 排在 v2
    # 前，反转后 v10+ 被挤到列表尾部，看起来像"新训练的模型没出现在列表中"，
    # 用户定位）。非数字尾缀（手工文件）排最前，反转后落最后。
    # 注意 Path.stem 只剥最后一个后缀："v10.meta.json".stem == "v10.meta"
    # （首轮修复即栽在此——isdigit 恒 False，排序退化回字典序）。
    # 用正则从完整文件名提取版本号。
    _ver_num = re.compile(r"^v(\d+)\.")

    def _num(p: Path) -> int:
        m = _ver_num.match(p.name)
        return int(m.group(1)) if m else -1

    for meta_path in sorted(out_dir.glob("v*.meta.json"), key=_num):
        try:
            meta = _json_safe(json.loads(meta_path.read_text(encoding="utf-8")))
        except (ValueError, OSError):
            continue
        if not isinstance(meta, dict) or not meta.get("version"):
            continue  # 手工杂项/缺 version 字段的文件不可激活/导出，不入列表
        versions.append(meta)
    return list(reversed(versions))  # 新版本在前
