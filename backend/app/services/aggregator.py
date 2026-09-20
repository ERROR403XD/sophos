"""视频综合分聚合与滚动更新（M4 实现）。

设计（docs/ARCHITECTURE.md §3.3/§3.4）：
- 聚合：video.final_score = personalized_final（存在且启用）否则 base_final
- *_final = mean(top-K identity 分数)，K=SOPHOS_TOPK 默认 3
- 路人过滤：identity 需满足 n_samples>=2 或代表帧质量分 >= QUALITY_FLOOR(0.35)
- 滚动更新触发点：视频处理完成（M4）、新评分/新模型启用（M6 接 dirty 重算）
"""
from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db.models import Face, FaceIdentity, FaceScore, VideoScore

QUALITY_FLOOR = 0.35  # 代表帧质量下限（低于此且样本数<2 的面容视为路人/误检）


def eligible_scores(session: Session, video_id: int) -> list[tuple[FaceIdentity, FaceScore, float]]:
    """返回 [(identity, score_row, rep_quality)]，按分高低排序后的合格面容。"""
    rows = session.execute(
        select(FaceIdentity, FaceScore, Face.quality_score)
        .join(FaceScore, FaceScore.identity_id == FaceIdentity.id)
        .outerjoin(Face, Face.id == FaceIdentity.rep_face_id)
        .where(FaceIdentity.video_id == video_id)
    ).all()
    eligible = [
        (identity, score, quality or 0.0)
        for identity, score, quality in rows
        if score.base_score is not None
        and (identity.n_samples >= 2 or (quality or 0.0) >= QUALITY_FLOOR)
    ]
    eligible.sort(key=lambda r: r[1].base_score or 0.0, reverse=True)
    return eligible


def recompute_video(session: Session, video_id: int, commit: bool = True) -> dict | None:
    """重算单个视频的综合分（滚动更新；无合格面容时写 null 并保留行）。

    commit=False 供 recompute_all 批量路径使用（分块统一提交，
    R5 大规模：数万视频逐条提交在网络盘上是分钟级放大）。
    """
    from app.db.models import KVSetting

    eligible = eligible_scores(session, video_id)
    topk = max(1, settings.topk)
    picked = eligible[:topk]

    base_vals = [score.base_score for _, score, _ in picked]
    base_final = round(sum(base_vals) / len(base_vals), 2) if base_vals else None
    pers_vals = [score.personalized_score for _, score, _ in picked
                 if score.personalized_score is not None]
    pers_final = round(sum(pers_vals) / len(pers_vals), 2) if pers_vals else None

    active_model = None
    kv = session.get(KVSetting, "active_pers_model")
    if kv is not None:
        try:
            active_model = __import__("json").loads(kv.value)
        except ValueError:
            active_model = None
    final = pers_final if (pers_final is not None and active_model) else base_final

    detail = [
        {"identity_id": identity.id, "base_score": score.base_score,
         "personalized_score": score.personalized_score,
         "n_samples": identity.n_samples}
        for identity, score, _ in picked
    ]
    row = session.get(VideoScore, video_id)
    if row is None:
        row = VideoScore(video_id=video_id)
        session.add(row)
    row.final_score = final
    row.base_final = base_final
    row.personalized_final = pers_final
    row.identity_count = len(eligible)
    row.topk_detail = json.dumps(detail, ensure_ascii=False)
    row.score_model_version = (
        f"pers:{active_model}" if (pers_final is not None and active_model)
        else f"base:{picked[0][1].base_model_version if picked else 'none'}")
    if commit:
        session.commit()
    return {"final": final, "base": base_final, "personalized": pers_final,
            "eligible": len(eligible)}


def recompute_all(session: Session, chunk_size: int = 500) -> int:
    """批量重算全部视频（模型启用/切换后调用）。

    R5 大规模：分块提交（每 chunk_size 个视频 commit 一次），避免数万
    视频的单个巨型事务；结果语义不变。
    R6：块间 sleep(0.02) 留读窗口（同 personalizer.apply_version——回滚
    日志模式下写提交的 EXCLUSIVE 锁不再连续挤占在线读请求）。
    """
    import time as _time

    video_ids = session.execute(select(FaceIdentity.video_id).distinct()).scalars().all()
    n = 0
    for vid in video_ids:
        recompute_video(session, vid, commit=False)
        n += 1
        if n % max(1, chunk_size) == 0:
            session.commit()
            _time.sleep(0.02)
    session.commit()
    return n
