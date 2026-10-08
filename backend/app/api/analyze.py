"""外部视频/图片分析 API（R6.3；R11 支持图片）——上传/投放目录 + 触发 + 结果/缩略图。

两条传入通道（视频可能很大，容器化下的取舍）：
- **HTTP 上传**：`POST /api/analyze/upload`（multipart，FastAPI 流式落盘到
  {data_dir}/inbox/，不占内存）——任意机器浏览器可直接推，体积默认不限
  （SOPHOS_ANALYZE_MAX_UPLOAD_BYTES 可设上限，0=不限）；
- **投放目录**：直接把文件拷进 {data_dir}/inbox/（Docker 下即 ./data/inbox
  卷；NAS 大文件走 SMB/NFS 拷贝比 HTTP 稳）→ `GET /api/analyze/inbox` 列出。

两通道汇合到 `POST /api/analyze/start` {filename}：提交 analyze 任务
（单 worker 串行），完成后 `GET /api/analyze/{token}` 取结果、
`GET /api/analyze/{token}/face_{n}.jpg` 取面容缩略图。视频与图片都支持；
图片整图即一帧（media_type=image，前端据此隐藏时长/帧数/时间戳）。
分析完全不触碰主库（见 services/analyzer 隔离语义）。
"""
from __future__ import annotations

import shutil
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.config import settings
from app.db.session import get_db
from app.services import analyzer
from app.services.jobs import job_detail, submit_job

router = APIRouter(prefix="/analyze", tags=["analyze"])

_UPLOAD_CONTENT_TYPES = {"application/octet-stream"}


def _inbox_path(filename: str) -> Path:
    try:
        return analyzer.resolve_inbox_file(filename)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={
            "code": "INVALID_FILENAME", "message": str(exc)}) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail={
            "code": "SOURCE_NOT_FOUND",
            "message": f"inbox file not found: {filename}"}) from exc


def _analysis_dir(token: str) -> Path | None:
    if analyzer.TOKEN_RE.fullmatch(token) is None:
        raise HTTPException(status_code=400, detail={
            "code": "INVALID_TOKEN", "message": "invalid analysis token"})
    root = analyzer.analyze_root()
    candidate = root / token
    if candidate.is_symlink():
        return None
    try:
        root = root.resolve()
        resolved = candidate.resolve()
        resolved.relative_to(root)
    except (OSError, ValueError):
        return None
    if resolved.parent != root:
        return None
    return candidate


@router.post("/upload")
async def upload_video(file: UploadFile) -> dict:
    """上传外部视频/图片到投放目录（流式写盘，大文件友好）；返回存储文件名。"""
    if not file.filename:
        raise HTTPException(status_code=400, detail={
            "code": "NO_FILENAME", "message": "upload requires a filename"})
    original = Path(file.filename.replace("\\", "/")).name
    if Path(original).suffix.lower() not in analyzer.VIDEO_EXTS | analyzer.IMAGE_EXTS:
        raise HTTPException(status_code=415, detail={
            "code": "UNSUPPORTED_MEDIA_TYPE",
            "message": "upload requires a supported video or image extension"})
    content_type = (file.content_type or "").split(";", 1)[0].strip().lower()
    if (content_type and not content_type.startswith("video/")
            and not content_type.startswith("image/")
            and content_type not in _UPLOAD_CONTENT_TYPES):
        raise HTTPException(status_code=415, detail={
            "code": "UNSUPPORTED_MEDIA_TYPE",
            "message": "upload requires a video/image or binary content type"})
    inbox = analyzer.inbox_root()
    inbox.mkdir(parents=True, exist_ok=True)
    stored = f"{uuid.uuid4().hex[:8]}_{analyzer.safe_inbox_name(original)}"
    dest = inbox / stored
    limit = settings.analyze_max_upload_bytes  # 0 = 不限制（R11 默认）
    size = 0
    try:
        with dest.open("xb") as f:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if limit and size > limit:
                    raise HTTPException(status_code=413, detail={
                        "code": "UPLOAD_TOO_LARGE",
                        "message": (f"upload exceeds the configured size limit "
                                    f"({limit / 1024 / 1024:.0f} MiB); raise or clear "
                                    f"SOPHOS_ANALYZE_MAX_UPLOAD_BYTES (0 = unlimited)")})
                f.write(chunk)
    except HTTPException:
        dest.unlink(missing_ok=True)
        raise
    except OSError as exc:
        dest.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail={
            "code": "UPLOAD_WRITE_FAILED",
            "message": "failed to store upload"}) from exc
    return {"filename": stored, "original": original, "size_bytes": size}


@router.get("/inbox")
def list_inbox() -> dict:
    """投放目录内的待分析文件（视频与图片）。"""
    inbox = analyzer.inbox_root()
    items = []
    if inbox.is_dir():
        for p in sorted(inbox.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            if (p.is_file() and not p.is_symlink()
                    and p.suffix.lower() in analyzer.VIDEO_EXTS | analyzer.IMAGE_EXTS):
                items.append({"filename": p.name,
                              "kind": "image" if p.suffix.lower() in analyzer.IMAGE_EXTS
                                      else "video",
                              "size_bytes": p.stat().st_size,
                              "mtime": int(p.stat().st_mtime)})
    return {"items": items}


@router.post("/start", status_code=202)
def start_analysis(body: dict, db: Session = Depends(get_db)) -> dict:
    """对 inbox 中指定文件发起分析任务；返回 token 与 job。"""
    filename = body.get("filename")
    if not isinstance(filename, str) or not filename:
        raise HTTPException(status_code=400, detail={
            "code": "INVALID_FILENAME", "message": "filename is required"})
    src = _inbox_path(filename)
    if not src.is_file():
        raise HTTPException(status_code=404, detail={
            "code": "SOURCE_NOT_FOUND", "message": f"inbox file not found: {filename}"})
    token = uuid.uuid4().hex[:12]
    job = submit_job(db, "analyze", {"token": token, "filename": filename})
    return {"token": token, "job": job_detail(job)}


@router.get("/list")
def list_all() -> dict:
    return {"items": analyzer.list_analyses()}


@router.get("/{token}")
def get_result(token: str) -> dict:
    if analyzer.TOKEN_RE.fullmatch(token) is None:
        raise HTTPException(status_code=400, detail={
            "code": "INVALID_TOKEN", "message": "invalid analysis token"})
    d = _analysis_dir(token)
    result = analyzer.load_result(token) if d is not None else None
    if result is None:
        raise HTTPException(status_code=404, detail={
            "code": "ANALYSIS_NOT_FOUND",
            "message": "analysis not found or still running (check jobs)"})
    return result


@router.get("/{token}/face_{idx}.jpg")
def get_face_thumb(token: str, idx: int) -> FileResponse:
    if idx < 0:
        raise HTTPException(status_code=400, detail={
            "code": "INVALID_FACE_INDEX",
            "message": "face index must be non-negative"})
    d = _analysis_dir(token)
    p = d / f"face_{idx}.jpg" if d is not None else None
    if p is None or not p.is_file():
        raise HTTPException(status_code=404, detail={
            "code": "THUMB_NOT_FOUND", "message": "face thumbnail not found"})
    return FileResponse(p, media_type="image/jpeg",
                        headers={"Cache-Control": "immutable"})


@router.delete("/{token}")
def delete_analysis(token: str) -> dict:
    """删除分析结果与其源文件（外部临时数据，不影响主库）。"""
    d = _analysis_dir(token)
    result = analyzer.load_result(token) if d is not None else None
    if (d is None or not d.is_dir()) and result is None:
        raise HTTPException(status_code=404, detail={
            "code": "ANALYSIS_NOT_FOUND", "message": "analysis not found"})
    shutil.rmtree(d, ignore_errors=True)
    if result:
        filename = result.get("filename", "")
        if isinstance(filename, str):
            try:
                src = analyzer.resolve_inbox_file(filename)
            except (ValueError, FileNotFoundError):
                src = None
            if src is not None:
                src.unlink(missing_ok=True)
    return {"ok": True}
