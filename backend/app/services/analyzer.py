"""外部视频/图片面容分析（R6.3；R11 支持图片）——一次性分析，不进主库。

用途：外部传入的视频（HTTP 上传或投放到 inbox 目录）跑一遍
抽帧 → 检测/门控 → 聚类/合并 → 打分 → 聚合，给出综合结果；
图片（R11）整图即一"帧"：检测/门控 → 聚类 → 打分，无抽帧/合并/上限 pass。

**隔离语义**（用户要求"不影响 Sophos 自己管理的面容库和视频库"）：
- 不写 video / face / face_identity / face_score 任何表（只经 job 系统记录
  任务本身）；不进评分队列、不参与训练；
- 产物落在 {data_dir}/analyze/{token}/（result.json + face_N.jpg），
  与主库缩略图/目录完全隔离；保留最近 analyze_keep 个分析，旧的自动清理。

复用主链路的口径（同一引擎单例、同一门控/聚类/合并/上限阈值），保证分析
分数与入库后的 base_score 可比。流程与 pipeline._process_video 的差别只在
"结果不入库，改写 result.json"。
"""
from __future__ import annotations

import io
import json
import re
import shutil
import subprocess
import time
from pathlib import Path

import cv2
import numpy as np

from app.config import settings
from app.services import frame_sampler, runtime_settings
from app.services.clustering import cap_groups, cluster_embeddings, merge_groups
from app.services.face_engine import portrait_crop

ANALYZE_DIR_NAME = "analyze"
INBOX_DIR_NAME = "inbox"
VIDEO_EXTS = {
    ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm",
    ".m4v", ".ts", ".mpg", ".mpeg", ".rmvb", ".3gp",
}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}  # R11：图片分析
TOKEN_RE = re.compile(r"[0-9a-f]{12}\Z")


def analyze_root() -> Path:
    return Path(settings.data_dir) / ANALYZE_DIR_NAME


def inbox_root() -> Path:
    return Path(settings.data_dir) / INBOX_DIR_NAME


def resolve_inbox_file(filename: str) -> Path:
    """Resolve an inbox item while allowing only a direct, regular video/image file."""
    if (not filename or filename in {".", ".."} or Path(filename).name != filename
            or any(ord(char) < 32 or ord(char) == 127 for char in filename)):
        raise ValueError("invalid inbox filename")
    if Path(filename).suffix.lower() not in VIDEO_EXTS | IMAGE_EXTS:
        raise ValueError("inbox file must be a video or image")

    root = inbox_root()
    candidate = root / filename
    if candidate.is_symlink():
        raise ValueError("inbox symlinks are not allowed")
    try:
        resolved = candidate.resolve(strict=True)
        resolved_root = root.resolve(strict=True)
        resolved.relative_to(resolved_root)
    except (OSError, ValueError) as exc:
        raise FileNotFoundError(filename) from exc
    if resolved.parent != resolved_root or not candidate.is_file():
        raise FileNotFoundError(filename)
    return candidate


def probe_duration_sec(path: str | Path) -> float | None:
    """ffprobe 读容器时长（秒）；失败返回 None（不影响分析本身）。"""
    exe = frame_sampler.locate_ffprobe()
    if exe is None:
        return None
    try:
        proc = subprocess.run(
            [str(exe), "-v", "error", "-show_entries", "format=duration",
             "-of", "json", str(path)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20)
        return round(float(json.loads(proc.stdout)["format"]["duration"]), 2)
    except (OSError, subprocess.SubprocessError, ValueError, KeyError):
        return None


def prune_old_analyses(keep: int, exclude: str | None = None) -> int:
    """只保留最近 keep 个分析目录（按目录 mtime），旧的删除。"""
    root = analyze_root()
    if not root.is_dir():
        return 0
    dirs = [d for d in root.iterdir() if d.is_dir() and d.name != exclude]
    dirs.sort(key=lambda d: d.stat().st_mtime, reverse=True)
    removed = 0
    for d in dirs[max(0, keep):]:
        shutil.rmtree(d, ignore_errors=True)
        removed += 1
    return removed


def analyze_video(session, job, token: str, path: str | Path, filename: str,
                  set_detail=None) -> dict:
    """完整分析一个外部视频；结果写 {analyze}/{token}/result.json 并返回。

    session/job：任务系统注入（进度 = 抽帧数；暂停/取消在逐帧检查点生效）。
    """
    # 延迟导入：复用主流水线的引擎单例与裁决/整理口径（pipeline 不反向依赖本模块）
    from app.services.jobs import check_point
    from app.services.pipeline import (_gender_filter_detailed, _is_clean,
                                       _sample_rank, get_face_engine, get_scorer)

    out_dir = analyze_root() / token
    out_dir.mkdir(parents=True, exist_ok=True)
    frames_dir = Path(settings.data_dir) / "frames" / f"analyze_{token}"
    duration = probe_duration_sec(path)
    engine = get_face_engine()
    scorer = get_scorer()
    interval = settings.sample_interval_sec

    result: dict = {"token": token, "filename": filename, "media_type": "video",
                    "duration_sec": duration,
                    "topk": settings.topk, "n_frames": 0, "n_samples": 0,
                    "n_faces": 0, "final_score": None, "max_score": None,
                    "faces": [], "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                             time.gmtime())}
    try:
        frames = frame_sampler.sample_frames(
            path, frames_dir, interval_sec=interval, ffmpeg_exe=settings.ffmpeg_exe)
        result["n_frames"] = len(frames)
        job.total = max(1, len(frames))
        job.done = 0
        session.commit()

        samples = []
        for i, frame_path in enumerate(frames):
            if set_detail is not None:
                set_detail(f"{filename} 第 {i + 1}/{len(frames)} 帧")
            check_point(session, job)
            data = np.fromfile(str(frame_path), dtype=np.uint8)
            img = cv2.imdecode(data, cv2.IMREAD_COLOR)
            if img is None:
                continue
            frame_samples = engine.process_frame(
                img, timestamp_sec=i * interval,
                det_thresh=settings.det_threshold,
                min_face=settings.min_face_size,
                min_quality=settings.min_quality,
                max_yaw_deg=settings.max_yaw_deg,
                max_pitch_deg=settings.max_pitch_deg,
                clip_profile_max=settings.clip_profile_max,
                source_frame=str(frame_path))
            if scorer is not None:
                for s in frame_samples:
                    try:
                        s.base_score = scorer.score(img, s.bbox)
                    except ValueError:
                        pass
            samples.extend(frame_samples)
            job.done = i + 1
            if (i + 1) % 20 == 0:
                session.commit()  # 进度落库（网络盘上逐帧提交过重，20 帧一批）
        session.commit()
        result["n_samples"] = len(samples)

        if samples:
            embs = np.stack([s.embedding for s in samples])
            groups = cluster_embeddings(embs, threshold=settings.cluster_threshold)
            groups, _rejected = _gender_filter_detailed(groups, samples)
            groups = merge_groups(
                embs, groups, threshold=settings.merge_threshold,
                review_threshold=settings.merge_review_threshold,
                cooccur_tolerance=settings.merge_cooccur_tolerance_sec,
                sample_clean=[_is_clean(s) for s in samples],
                sample_timestamps=[s.timestamp_sec for s in samples],
                singleton_threshold=settings.merge_singleton_threshold)
            max_per_person = runtime_settings.get_value(session, "max_faces_per_person")
            if max_per_person > 0 and len(groups) > max_per_person:
                groups = cap_groups(
                    embs, groups, max_per_person,
                    floor=settings.cap_merge_floor,
                    cooccur_tolerance=settings.merge_cooccur_tolerance_sec,
                    sample_timestamps=[s.timestamp_sec for s in samples])

            for gi, group in enumerate(groups):
                ranked = sorted(group, key=lambda idx: _sample_rank(samples[idx]))
                rep = samples[ranked[0]]
                # 缩略图与主链路同口径：人像取景；R12 起 rep 实帧优先（近似
                # 定位仅在样本缺 source_frame 时兜底——与 pipeline._persist_groups 同因）
                thumb_name = f"face_{gi}.jpg"
                frame_file = rep.source_frame
                if frame_file is None:
                    idx = min(max(int(round(rep.timestamp_sec / interval)), 0),
                              len(frames) - 1)
                    frame_file = str(frames[idx])
                data = np.fromfile(frame_file, dtype=np.uint8)
                frame_img = cv2.imdecode(data, cv2.IMREAD_COLOR)
                img = (portrait_crop(frame_img, rep.bbox)
                       if frame_img is not None else rep.aligned)
                ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 90])
                if ok:
                    buf.tofile(str(out_dir / thumb_name))
                clip_fps = [samples[k].clip_female_prob for k in group]
                clip_mean = (round(float(np.mean(clip_fps)), 4)
                             if clip_fps and all(v is not None for v in clip_fps) else None)
                result["faces"].append({
                    "index": gi,
                    "score": rep.base_score,
                    "quality": rep.quality_score,
                    "timestamp_sec": rep.timestamp_sec,
                    "female_prob": round(float(np.mean(
                        [samples[k].female_prob for k in group])), 4),
                    "clip_female_mean": clip_mean,
                    "pose_class": rep.pose_class,
                    "n_samples": len(group),
                    "thumb": f"/api/analyze/{token}/{thumb_name}" if ok else None,
                })

        scored = sorted((f for f in result["faces"] if f["score"] is not None),
                        key=lambda f: f["score"], reverse=True)
        if scored:
            top = scored[:max(1, settings.topk)]
            # 聚合与主链 aggregator 同口径：top-K 均值
            result["final_score"] = round(sum(f["score"] for f in top) / len(top), 2)
            result["max_score"] = scored[0]["score"]
        result["n_faces"] = len(result["faces"])
    finally:
        frame_sampler.cleanup_frames(frames_dir)

    (out_dir / "result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    prune_old_analyses(settings.analyze_keep, exclude=token)
    return result


def _decode_image(path: Path):
    """读图 → EXIF 方向转正（手机照片常带旋转标记，cv2.imdecode 不处理）。

    Pillow 为 requirements 内依赖；PIL 打不开时退回 cv2.imdecode（覆盖
    Pillow 不认的个别格式）。两者都失败返回 None，由调用方报错。
    """
    data = np.fromfile(str(path), dtype=np.uint8)  # 非 ASCII 路径安全（Windows）
    try:
        from PIL import Image, ImageOps

        with Image.open(io.BytesIO(data.tobytes())) as im:
            im = ImageOps.exif_transpose(im)
            return cv2.cvtColor(np.asarray(im.convert("RGB")), cv2.COLOR_RGB2BGR)
    except Exception:  # noqa: BLE001 —— 任何 PIL 失败都退回 cv2 解码
        return cv2.imdecode(data, cv2.IMREAD_COLOR)


def analyze_image(session, job, token: str, path: str | Path, filename: str) -> dict:
    """单张图片一次性分析（R11）；结果结构与 analyze_video 一致（media_type=image）。

    与视频流的差别：整图即一"帧"，无抽帧/进度；跳过 merge/cap pass——
    单图内样本 timestamp 全为 0，同帧共现否决（ADR-018）本就禁止任何合并，
    显式跳过避免语义混淆（拼图里同一人出现两次就是两张面容卡）。
    门控/聚类/打分/缩略图与主链路同口径，保证与入库分数可比；不入主库。
    """
    from app.services.pipeline import (_gender_filter_detailed, _sample_rank,
                                       get_face_engine, get_scorer)

    out_dir = analyze_root() / token
    out_dir.mkdir(parents=True, exist_ok=True)
    engine = get_face_engine()
    scorer = get_scorer()

    result: dict = {"token": token, "filename": filename, "media_type": "image",
                    "duration_sec": None,
                    "topk": settings.topk, "n_frames": 1, "n_samples": 0,
                    "n_faces": 0, "final_score": None, "max_score": None,
                    "faces": [], "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                             time.gmtime())}
    img = _decode_image(Path(path))
    if img is None:
        raise ValueError(f"cannot decode image: {Path(path).name}")
    job.total = 1
    job.done = 0
    session.commit()

    samples = engine.process_frame(
        img, timestamp_sec=0.0,
        det_thresh=settings.det_threshold,
        min_face=settings.min_face_size,
        min_quality=settings.min_quality,
        max_yaw_deg=settings.max_yaw_deg,
        max_pitch_deg=settings.max_pitch_deg,
        clip_profile_max=settings.clip_profile_max,
        source_frame=str(path))
    if scorer is not None:
        for s in samples:
            try:
                s.base_score = scorer.score(img, s.bbox)
            except ValueError:
                pass  # 裁剪过小
    job.done = 1
    session.commit()
    result["n_samples"] = len(samples)

    if samples:
        embs = np.stack([s.embedding for s in samples])
        groups = cluster_embeddings(embs, threshold=settings.cluster_threshold)
        groups, _rejected = _gender_filter_detailed(groups, samples)

        for gi, group in enumerate(groups):
            ranked = sorted(group, key=lambda idx: _sample_rank(samples[idx]))
            rep = samples[ranked[0]]
            thumb_name = f"face_{gi}.jpg"
            # 缩略图与主链路同口径：人像取景
            thumb = portrait_crop(img, rep.bbox)
            ok, buf = cv2.imencode(".jpg", thumb, [cv2.IMWRITE_JPEG_QUALITY, 90])
            if ok:
                buf.tofile(str(out_dir / thumb_name))
            clip_fps = [samples[k].clip_female_prob for k in group]
            clip_mean = (round(float(np.mean(clip_fps)), 4)
                         if clip_fps and all(v is not None for v in clip_fps) else None)
            result["faces"].append({
                "index": gi,
                "score": rep.base_score,
                "quality": rep.quality_score,
                "timestamp_sec": 0.0,
                "female_prob": round(float(np.mean(
                    [samples[k].female_prob for k in group])), 4),
                "clip_female_mean": clip_mean,
                "pose_class": rep.pose_class,
                "n_samples": len(group),
                "thumb": f"/api/analyze/{token}/{thumb_name}" if ok else None,
            })

    scored = sorted((f for f in result["faces"] if f["score"] is not None),
                    key=lambda f: f["score"], reverse=True)
    if scored:
        top = scored[:max(1, settings.topk)]
        # 聚合与主链 aggregator 同口径：top-K 均值
        result["final_score"] = round(sum(f["score"] for f in top) / len(top), 2)
        result["max_score"] = scored[0]["score"]
    result["n_faces"] = len(result["faces"])

    (out_dir / "result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    prune_old_analyses(settings.analyze_keep, exclude=token)
    return result


def load_result(token: str) -> dict | None:
    if TOKEN_RE.fullmatch(token) is None:
        return None
    root = analyze_root()
    out_dir = root / token
    if out_dir.is_symlink():
        return None
    try:
        out_dir.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return None
    p = out_dir / "result.json"
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except ValueError:
        return None


def list_analyses(limit: int = 50) -> list[dict]:
    root = analyze_root()
    if not root.is_dir():
        return []
    out = []
    for d in sorted(root.iterdir(), key=lambda d: d.stat().st_mtime, reverse=True)[:limit]:
        r = load_result(d.name)
        if r is None:
            continue  # 无 result.json = 分析未完成
        out.append({"token": r["token"], "filename": r["filename"],
                    "media_type": r.get("media_type", "video"),
                    "final_score": r["final_score"], "max_score": r["max_score"],
                    "n_faces": r["n_faces"], "n_frames": r["n_frames"],
                    "duration_sec": r.get("duration_sec"),
                    "created_at": r.get("created_at")})
    return out


def safe_inbox_name(name: str) -> str:
    """上传文件名净化：保留扩展名与可读部分，去路径分隔与控制字符。"""
    base = name.replace("\\", "/").split("/")[-1]
    base = "".join(ch for ch in base if ch.isalnum() or ch in " .-_（）()#&[]").strip()
    return base or "video.mp4"
