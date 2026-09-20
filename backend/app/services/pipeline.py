"""任务处理器注册表（M2 起；M3 补齐 process 全流水线；R1 调整面容准入 ADR-014）。

- scan：调用 scanner.scan_all（M2；R5 每目录前有暂停/取消检查点）
- process（R1 顺序）：抽帧 → 样本级门控（尺寸/截断/质量/姿态）+ 全候选 embed
  → 视频内聚类 → identity 级性别裁决 → 合并 pass → 面容上限 pass（R5）
  → 样本修剪/rep 优选 → 面容库 + 缩略图
  R5 起按批处理（SOPHOS_PROCESS_BATCH_SIZE，runtime_settings 可覆盖）：
  单个 job 只处理自己的 video_ids，完成后检查点通过才链式提交下一批——
  批与批之间排在队列尾，其他任务（scan/train）可自然插队（ADR-022）
- train：M6 实现（personalizer）
"""
from __future__ import annotations

import json
import logging
import shutil
import traceback
from pathlib import Path

import cv2
import numpy as np
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.config import settings
from app.db.models import Face, FaceIdentity, FaceScore, Job, PairComparison, UserRating, Video
from app.services import frame_sampler
from app.services import aggregator
from app.services import runtime_settings
from app.services.face_engine import FaceEngine, FaceSample, portrait_crop
from app.services.jobs import JobInterrupted, check_point, register_handler, submit_job
from app.services.kv import get_work_dirs
from app.services.clustering import cap_groups, cluster_embeddings, merge_groups
from app.services.scorer import BaseScorer
from app.services.scanner import scan_all
from app.services import thumbs

log = logging.getLogger("sophos.pipeline")

# R1/P1：单样本 identity 无投票修正，女性裁决用更严阈值（PLAN_v1.0.1 §1.2，写死）
SINGLE_SAMPLE_FEMALE_THRESHOLD = 0.65

# R6 保底（ADR-028）：性别门把视频全部 cluster 拒掉时，保底保留 1 个"证据
# 尚可"的 cluster 的下限——fp 与 CLIP 女性均值都须 ≥ 此值（两票各过半）。
# rep 质量下限 = min_quality × 此比例（"有效面容"仍须基本清晰，宁缺毋滥）。
RESCUE_FEMALE_MIN = 0.50
RESCUE_QUALITY_RATIO = 0.5

_OCCLUSION_CLEAN_BELOW = 0.6  # occlusion_score < 此值视为"干净样本"（PLAN §2.3）
_POSE_RANK = {"frontal": 0, "near": 1, "side": 2}

_engine: FaceEngine | None = None
_scorer: BaseScorer | None | str = None  # None=未初始化, "missing"=模型缺失（只警告一次）


def get_face_engine() -> FaceEngine:
    """进程内单例；模型缺失时抛 FileNotFoundError（job 会记为 failed）。"""
    global _engine
    if _engine is None:
        _engine = FaceEngine(
            models_dir=settings.final_models_dir(),
            det_name=settings.model_det,
            emb_name=settings.model_emb,
            gender_name=settings.model_gender,
        )
    return _engine


def get_scorer() -> BaseScorer | None:
    """颜值打分器单例；beauty_scut.onnx 缺失时返回 None（跳过打分，不阻塞流水线）。"""
    global _scorer
    if _scorer is None:
        path = settings.final_models_dir() / "beauty_scut.onnx"
        if path.is_file():
            _scorer = BaseScorer(path)
        else:
            log.warning("beauty model missing (%s); scoring skipped "
                        "(run scripts/convert_beauty_model.py)", path)
            _scorer = "missing"
    return None if _scorer == "missing" else _scorer


def _set_progress(session: Session, job: Job, done: int, total: int) -> None:
    job.done = done
    job.total = total
    session.commit()


def scan_handler(session: Session, job: Job, params: dict) -> None:
    dirs = params.get("work_dirs") or get_work_dirs(session)
    job.total = max(1, len(dirs))
    session.commit()
    # R5：每个根目录开始前做暂停/取消检查（check_cb 抛 JobInterrupted）
    report = scan_all(dirs, session,
                      progress_cb=lambda d, t: _set_progress(session, job, d, t),
                      check_cb=lambda: check_point(session, job))
    job.result = json.dumps(report.as_dict(), ensure_ascii=False)
    job.done = job.total
    # R6.3 联动：自动扫描发现新增/变更视频且"自动处理面容"开启 → 立即提交
    # 处理链首批（不等 automation 的下一 tick；has_active_job 防重复）
    if (params.get("auto") and report.added + report.changed > 0
            and runtime_settings.get_value(session, "auto_process_enabled")):
        from app.services.automation import _submit_process_batch
        from app.services.jobs import has_active_job
        if not has_active_job(session, "process"):
            _submit_process_batch(session)
            log.info("auto scan found %d new/changed videos; auto process chained",
                     report.added + report.changed)


# ---------------- process 流水线（M3） ----------------

def _reset_video_faces(session: Session, video_id: int) -> None:
    """幂等：重处理前删除该视频的派生数据；存在用户评分则拒绝（保护用户数据）。"""
    identity_ids = session.execute(
        select(FaceIdentity.id).where(FaceIdentity.video_id == video_id)
    ).scalars().all()
    if identity_ids:
        rated = session.execute(
            select(UserRating.id).where(UserRating.identity_id.in_(identity_ids)).limit(1)
        ).scalar_one_or_none()
        compared = session.execute(
            select(PairComparison.id).where(
                PairComparison.winner_identity_id.in_(identity_ids)).limit(1)
        ).scalar_one_or_none()
        if rated is not None or compared is not None:
            raise RuntimeError("video has user ratings; refusing auto-reprocess "
                               "(manual reset required)")
        session.execute(delete(FaceScore).where(FaceScore.identity_id.in_(identity_ids)))
        session.execute(delete(PairComparison).where(
            PairComparison.winner_identity_id.in_(identity_ids)))
        session.execute(delete(PairComparison).where(
            PairComparison.loser_identity_id.in_(identity_ids)))
        session.execute(delete(UserRating).where(UserRating.identity_id.in_(identity_ids)))
        # R5：同步清理缩略图（旧实现遗留孤儿文件——大库下无限累积）
        for iid in identity_ids:
            thumbs.remove_thumb(iid)
    session.execute(delete(Face).where(Face.video_id == video_id))
    session.execute(delete(FaceIdentity).where(FaceIdentity.video_id == video_id))
    session.commit()


def _pick_frames_dir(video_id: int) -> Path:
    return Path(settings.data_dir) / "frames" / str(video_id)


# ---------------- R1(P1/P2/P3)：identity 级裁决与整理（ADR-014） ----------------

def _is_clean(s: FaceSample) -> bool:
    """干净样本 = 非疑似遮挡（occlusion_score 低于阈值）。"""
    return s.occlusion_score is None or s.occlusion_score < _OCCLUSION_CLEAN_BELOW


def _sample_rank(s: FaceSample) -> tuple[int, int, float]:
    """组内排序键：干净优先 → 姿态正面优先（frontal>near>side）→ 质量降序。

    rep 优选（P3/3c）与 mean_embedding 修剪（P3/3b）共用：同 identity 有正面
    清晰样本时绝不选模糊侧面做代表（直接影响缩略图与 base_score 输入）。
    """
    return (0 if _is_clean(s) else 1,
            _POSE_RANK.get(s.pose_class or "", 3),
            -(s.quality_score or 0.0))


def _gender_filter_detailed(groups: list[list[int]], samples: list[FaceSample]
                            ) -> tuple[list[list[int]], list[dict]]:
    """identity 级女性裁决，同时返回被拒 cluster 的证据（R6 保底用）。

    返回 (kept, rejected)：rejected 元素为 {group, mean_fp, clip_mean}。
    """
    keep: list[list[int]] = []
    rejected: list[dict] = []
    for group in groups:
        mean_fp = float(np.mean([samples[i].female_prob for i in group]))
        threshold = (settings.female_identity_threshold if len(group) > 1
                     else SINGLE_SAMPLE_FEMALE_THRESHOLD)
        clip_mean = None
        clip_fps = [samples[i].clip_female_prob for i in group]
        if clip_fps and all(v is not None for v in clip_fps):
            clip_mean = float(np.mean(clip_fps))
        if mean_fp < threshold or (clip_mean is not None
                                   and clip_mean < settings.clip_gender_min):
            rejected.append({"group": group, "mean_fp": mean_fp,
                             "clip_mean": clip_mean})
            continue
        keep.append(group)
    return keep, rejected


def _gender_filter(groups: list[list[int]], samples: list[FaceSample]) -> list[list[int]]:
    """identity 级女性裁决（R1/P1，ADR-014；R2 加 CLIP 一致同意票）。兼容旧签名。

    - 组均值稀释男性偶发误判（组内绝大多数帧 fp<0.5 才会被拒）；
    - 单样本 identity 无投票修正，用更严的 SINGLE_SAMPLE_FEMALE_THRESHOLD；
    - R2：CLIP 可用时须**一致同意**（组内 CLIP 女性均值 ≥ clip_gender_min）——
      genderage 在部分男性脸上高置信误判（实测 fp≥0.83），双模型合取堵漏；
    - 未过阈值 → 整组连同样本删除（面容库只含女性语义）。
    """
    keep, _rejected = _gender_filter_detailed(groups, samples)
    return keep


def _rescue_group(rejected: list[dict], samples: list[FaceSample]) -> list[list[int]]:
    """性别门全拒时的保底（R6，ADR-028）：每个视频原则上保留 ≥1 张有效面容。

    用户语义（宁缺毋滥的两面）：面容库不为凑数保留低质量面容，但一个
    明明有女性出镜的视频也不应因门控边际误判而颗粒无收。规则：
    - 证据下限：被拒 cluster 的 fp 均值 ≥ RESCUE_FEMALE_MIN，且 CLIP 女性均值
      ≥ RESCUE_FEMALE_MIN（CLIP 可用时）——双模型各过半，纯男性视频仍为 0；
    - 质量下限：rep（组内最优样本）质量 ≥ min_quality × RESCUE_QUALITY_RATIO，
      且 rep 非侧脸（与 allow_side_rep=false 的主链口径一致）；
    - 只保质量最优的 1 个 cluster（不凑数）；全遮挡组不保底。
    """
    best: tuple[tuple, list[int]] | None = None
    for item in rejected:
        group = item["group"]
        if item["mean_fp"] < RESCUE_FEMALE_MIN:
            continue
        if item["clip_mean"] is not None and item["clip_mean"] < RESCUE_FEMALE_MIN:
            continue
        ranked = sorted(group, key=lambda idx: _sample_rank(samples[idx]))
        rep = samples[ranked[0]]
        q = rep.quality_score or 0.0
        if q < settings.min_quality * RESCUE_QUALITY_RATIO:
            continue
        if _POSE_RANK.get(rep.pose_class or "", 3) >= 2:
            continue  # 侧脸 rep：整组是侧脸，保底也应保"完整面庞"
        clean = [idx for idx in ranked if _is_clean(samples[idx])]
        if not clean:
            continue  # 全遮挡：进库也会被置空 embedding，无个性化价值
        key = (_POSE_RANK.get(rep.pose_class or "", 3), -q)
        if best is None or key < best[0]:
            best = (key, group)
    return [best[1]] if best else []


def _persist_groups(session: Session, video: Video,
                    samples: list[FaceSample], groups: list[list[int]],
                    frames: list[Path] | None = None,
                    interval_sec: float = 2.0) -> int:
    """组 → face_identity/face/缩略图/分 入库（R1 语义 + R3 人像缩略图，独立函数便于单测）。

    - rep 优选：干净 > 姿态正面 > 质量高（P3/3c）；
    - mean_embedding：干净样本内取 top-N（P3/3b）；全遮挡（无干净样本）→ 置空
      （personalizer 据此剔出训练集，P2 特征隔离）；
    - occluded=1 + occluded_source=auto：多数样本疑似遮挡或整组无干净样本；
    - R3：缩略图用**人像取景**（头顶+20%/两侧+20%/向下+80%，见 face_engine.portrait_crop），
      需要原始帧（frames 非空时从抽帧文件读取；None 时退回对齐脸，兼容单测/旧路径）；
      R5：写入走 thumbs 服务分片存储（单目录 ≤1000 文件，大库检索友好）。
    """
    n_identities = 0
    scorer = get_scorer()
    for group in groups:
        ranked = sorted(group, key=lambda idx: _sample_rank(samples[idx]))
        # R2：rep 已是组内最优（干净>正面>清晰）；rep 仍为侧脸 → 整组不入评分库
        #（用户验收反馈"侧脸不入"；组内不可能存在更正面的样本被丢）
        if (not settings.allow_side_rep
                and _POSE_RANK.get(samples[ranked[0]].pose_class or "", 3) >= 2):
            continue
        identity = FaceIdentity(video_id=video.id, n_samples=len(group))
        session.add(identity)
        session.flush()

        clean = [idx for idx in ranked if _is_clean(samples[idx])]
        identity.female_prob_mean = round(
            float(np.mean([samples[i].female_prob for i in group])), 4)  # R1(P1)
        clip_fps = [samples[i].clip_female_prob for i in group]
        if clip_fps and all(v is not None for v in clip_fps):
            identity.clip_female_mean = round(float(np.mean(clip_fps)), 4)  # R2
        pool = clean[:max(1, settings.max_samples_per_identity)]
        if pool:
            identity.mean_embedding = np.mean(
                [samples[i].embedding for i in pool], axis=0
            ).astype(np.float32).tobytes()
        if not clean or (len(group) - len(clean)) * 2 > len(group):
            identity.occluded = 1
            identity.occluded_source = "auto"

        rep_sample = samples[ranked[0]]
        rep_face_id = None
        for idx in group:
            s = samples[idx]
            face = Face(video_id=video.id, identity_id=identity.id,
                        timestamp_sec=s.timestamp_sec,
                        bbox_x1=s.bbox[0], bbox_y1=s.bbox[1],
                        bbox_x2=s.bbox[2], bbox_y2=s.bbox[3],
                        det_score=s.det_score, quality_score=s.quality_score,
                        female_prob=s.female_prob,
                        pose_yaw=s.pose_yaw, pose_pitch=s.pose_pitch,
                        pose_class=s.pose_class,
                        occlusion_score=s.occlusion_score,
                        clip_female_prob=s.clip_female_prob,
                        clip_profile_prob=s.clip_profile_prob,
                        embedding=s.embedding.tobytes())
            session.add(face)
            session.flush()
            if idx == ranked[0]:
                rep_face_id = face.id
        identity.rep_face_id = rep_face_id
        thumb_img = rep_sample.aligned  # 兜底：对齐脸
        if frames:
            idx = min(max(int(round(rep_sample.timestamp_sec / interval_sec)), 0),
                      len(frames) - 1)
            data = np.fromfile(str(frames[idx]), dtype=np.uint8)
            frame_img = cv2.imdecode(data, cv2.IMREAD_COLOR)
            if frame_img is not None:
                thumb_img = portrait_crop(frame_img, rep_sample.bbox)
        thumbs.save_thumb(identity.id, thumb_img)
        if rep_sample.base_score is not None:
            session.add(FaceScore(identity_id=identity.id,
                                  base_score=rep_sample.base_score,
                                  base_model_version=scorer.version if scorer else None))
        session.flush()
        n_identities += 1
    return n_identities


def _process_video(session: Session, engine: FaceEngine, video: Video,
                   job: Job | None = None) -> dict:
    _reset_video_faces(session, video.id)
    video.status = "processing"
    video.status_msg = None
    session.commit()

    frames_dir = _pick_frames_dir(video.id)
    scorer = get_scorer()
    try:
        frames = frame_sampler.sample_frames(
            video.path, frames_dir, interval_sec=settings.sample_interval_sec,
            ffmpeg_exe=settings.ffmpeg_exe)
        samples: list[FaceSample] = []
        for i, frame_path in enumerate(frames):
            # R5：抽帧/推理可能持续数分钟，逐帧做暂停/取消检查点
            #（check_point 仅在有请求时才 commit，常态零开销）
            if job is not None:
                check_point(session, job)
            # 等间隔抽帧的第 i 帧近似时间点 = i × interval
            # cv2.imread 对 Windows 非 ASCII 路径不可靠，用 imdecode
            data = np.fromfile(str(frame_path), dtype=np.uint8)
            img = cv2.imdecode(data, cv2.IMREAD_COLOR)
            if img is None:
                continue
            # R1(P1)：不再按性别丢弃样本——全部候选进聚类，identity 级再裁决
            frame_samples = engine.process_frame(
                img, timestamp_sec=i * settings.sample_interval_sec,
                det_thresh=settings.det_threshold,
                min_face=settings.min_face_size,
                min_quality=settings.min_quality,
                max_yaw_deg=settings.max_yaw_deg,
                max_pitch_deg=settings.max_pitch_deg,
                clip_profile_max=settings.clip_profile_max)
            if scorer is not None:
                for s in frame_samples:
                    try:
                        s.base_score = scorer.score(img, s.bbox)
                    except ValueError:
                        pass  # 裁剪过小
            samples.extend(frame_samples)

        n_identities = 0
        dropped_gender_groups = 0
        groups_before_merge = 0
        cap_merged_groups = 0
        rescued_groups = 0
        if samples:
            embs = np.stack([s.embedding for s in samples])
            # R1 流水线顺序（ADR-014）：聚类 → 性别裁决 → 合并 pass → 修剪/入库
            groups = cluster_embeddings(embs, threshold=settings.cluster_threshold)
            n_after_cluster = len(groups)
            # R6：性别裁决同时记下被拒 cluster 的证据（全部被拒时供保底复核）
            groups, rejected_by_gender = _gender_filter_detailed(groups, samples)
            dropped_gender_groups = n_after_cluster - len(groups)
            # R4(P1)：双层 merge（高置信直接 + 中等相似谨慎）+ 同帧共现一票否决；
            # R6(P1)：第三层单样本同人合并（碎片主因），阈值更严（0.74）
            groups_before_merge = len(groups)
            groups = merge_groups(
                embs, groups, threshold=settings.merge_threshold,
                review_threshold=settings.merge_review_threshold,
                cooccur_tolerance=settings.merge_cooccur_tolerance_sec,
                sample_clean=[_is_clean(s) for s in samples],
                sample_timestamps=[s.timestamp_sec for s in samples],
                singleton_threshold=settings.merge_singleton_threshold)
            # R5（ADR-023）：同一视频同一人最多保留 max_faces_per_person 张面容
            #（runtime_settings 可覆盖；0 = 不限制）。余下护栏与合并 pass 同口径。
            max_per_person = runtime_settings.get_value(session, "max_faces_per_person")
            if max_per_person > 0 and len(groups) > max_per_person:
                before_cap = len(groups)
                groups = cap_groups(
                    embs, groups, max_per_person,
                    floor=settings.cap_merge_floor,
                    cooccur_tolerance=settings.merge_cooccur_tolerance_sec,
                    sample_timestamps=[s.timestamp_sec for s in samples])
                cap_merged_groups = before_cap - len(groups)
                log.info("video %s face cap: %d -> %d (max_faces_per_person=%d, "
                         "floor=%.2f)", video.id, before_cap, len(groups),
                         max_per_person, settings.cap_merge_floor)
            # R6（ADR-028）保底：性别门把全视频拒光时，若仍有"两票各过半"
            # 的边际 cluster，保 1 张，避免明明有女性出镜的视频颗粒无收
            if not groups and rejected_by_gender:
                rescued = _rescue_group(rejected_by_gender, samples)
                if rescued:
                    groups = rescued
                    rescued_groups = len(groups)
                    log.info("video %s gender gate rejected all clusters; "
                             "rescued 1 borderline cluster (fp/clip >= %.2f)",
                             video.id, RESCUE_FEMALE_MIN)
            log.info("video %s clustering: %d groups -> gender-filter -%d -> "
                     "merge -> cap -%d -> rescue +%d", video.id, n_after_cluster,
                     dropped_gender_groups, cap_merged_groups, rescued_groups)
            # R3：帧保留到入库后（缩略图人像取景需原始帧），finally 统一清理
            n_identities = _persist_groups(
                session, video, samples, groups,
                frames=frames, interval_sec=settings.sample_interval_sec)
    finally:
        frame_sampler.cleanup_frames(frames_dir)

    video.status = "done"
    video.identity_count = n_identities
    if n_identities == 0:
        # R6：0 张时区分"确实没检出人脸"与"检出但未过有效门控"，便于诊断
        video.status_msg = ("no faces detected" if not samples
                            else "no valid female faces (all gated out)")
    session.commit()
    aggregator.recompute_video(session, video.id)  # 滚动更新视频-分数对照表
    return {"identities": n_identities, "samples": len(samples),
            "gender_rejected_groups": dropped_gender_groups,
            "groups_before_merge": groups_before_merge,
            "cap_merged_groups": cap_merged_groups,
            "rescued_groups": rescued_groups}


def _pending_video_ids(session: Session) -> list[int]:
    return list(session.execute(
        select(Video.id).where(Video.status == "pending").order_by(Video.id)
    ).scalars().all())


def _submit_next_batch(session: Session, current_params: dict) -> Job | None:
    """链式分批（R5，ADR-022）：当前批结束后取下一批 pending 提交新 job。

    每批只挂一个 process job：批间提交的新任务（scan/train/手动批）排在
    队列更前，天然获得插队机会。此处处于 handler 内（当前 job 仍为
    running），process/start 的 409 防重窗口不中断链条。
    """
    batch_size = runtime_settings.get_value(session, "process_batch_size")
    ids = _pending_video_ids(session)[:batch_size]
    if not ids:
        return None
    return submit_job(session, "process", {
        "video_ids": ids, "chain": True,
        "batch_index": (current_params.get("batch_index") or 0) + 1,
    })


def process_handler(session: Session, job: Job, params: dict) -> None:
    engine = get_face_engine()  # 模型缺失 → 异常 → job failed（信息明确）
    if not engine.clip_ready:
        log.warning("CLIP second-opinion model missing (clip_vitb32_face.onnx / "
                    "clip_vitb32_prompts.npz); gender gating degraded to genderage-only "
                    "(run scripts/export_clip_gender.py, see data/models/README.md)")
    if params.get("video_ids"):
        video_ids = [int(v) for v in params["video_ids"]]
        videos = [session.get(Video, v) for v in video_ids]
        videos = [v for v in videos if v is not None]
    else:
        # 兼容旧入口（无显式 ids）：一次性取全部 pending（不分批）
        videos = session.execute(
            select(Video).where(Video.status == "pending").order_by(Video.id)
        ).scalars().all()

    job.total = len(videos)
    job.done = 0
    session.commit()

    ok, failed = 0, 0
    per_video: dict[str, dict] = {}
    for i, video in enumerate(videos, start=1):
        # R5：批内逐视频检查点（暂停/取消在视频边界生效，进度已提交）
        check_point(session, job)
        try:
            info = _process_video(session, engine, video, job=job)
            ok += 1
            per_video[str(video.id)] = info
        except JobInterrupted:
            # 中断发生在 _process_video 内部时该视频可能停在 processing：
            # 回退 pending（帧文件已由其 finally 清理），恢复后续跑重做本视频
            session.rollback()
            v = session.get(Video, video.id)
            if v is not None and v.status == "processing":
                v.status = "pending"
                v.status_msg = None
            session.commit()
            raise
        except Exception as exc:  # noqa: BLE001 —— 单视频失败不阻塞整批
            log.exception("process video %s failed", video.id)
            session.rollback()
            video = session.get(Video, video.id)
            video.status = "failed"
            video.status_msg = f"{type(exc).__name__}: {exc}"
            session.commit()
            failed += 1
            per_video[str(video.id)] = {"error": str(exc)}
        job.done = i
        session.commit()

    next_batch_id = None
    if params.get("chain"):
        # 链式提交下一批；有暂停/取消请求 → check_point 抛出、链条停止
        #（剩余视频保持 pending，恢复 = 暂停任务 resume 或重新处理）
        check_point(session, job)
        nxt = _submit_next_batch(session, params)
        next_batch_id = nxt.id if nxt is not None else None

    job.result = json.dumps({"ok": ok, "failed": failed, "videos": per_video,
                             "next_batch": next_batch_id},
                            ensure_ascii=False)


def train_handler(session: Session, job: Job, params: dict) -> None:
    check_point(session, job)  # R5：训练耗时极短，仅起点检查
    from app.services import personalizer

    meta = personalizer.train(session, settings.final_models_dir())
    job.result = json.dumps(meta, ensure_ascii=False)


def analyze_handler(session: Session, job: Job, params: dict) -> None:
    """外部视频一次性分析（R6.3）：不入主库，结果写 data/analyze/{token}/。"""
    check_point(session, job)
    from app.services import analyzer  # 延迟导入：analyzer 复用本模块的口径助手

    token = params["token"]
    result = analyzer.analyze_video(session, job, token,
                                    params["path"], params.get("filename") or token)
    # job.result 只放摘要（完整结果在 analyze/{token}/result.json，经 API 取）
    job.result = json.dumps({"token": token, "final_score": result["final_score"],
                             "max_score": result["max_score"],
                             "n_faces": result["n_faces"],
                             "n_samples": result["n_samples"]},
                            ensure_ascii=False)


def register_all() -> None:
    register_handler("scan", scan_handler)
    register_handler("process", process_handler)
    register_handler("train", train_handler)
    register_handler("analyze", analyze_handler)
