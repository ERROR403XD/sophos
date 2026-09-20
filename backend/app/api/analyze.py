"""外部视频分析 API（R6.3）——上传/投放目录 + 触发 + 结果/缩略图。

两条传入通道（视频可能很大，容器化下的取舍）：
- **HTTP 上传**：`POST /api/analyze/upload`（multipart，FastAPI 流式落盘到
  {data_dir}/inbox/，不占内存）——任意机器浏览器可直接推，适合百 MB~数 GB；
- **投放目录**：直接把文件拷进 {data_dir}/inbox/（Docker 下即 ./data/inbox
  卷；NAS 大文件走 SMB/NFS 拷贝比 HTTP 稳）→ `GET /api/analyze/inbox` 列出。

两通道汇合到 `POST /api/analyze/start` {filename}：提交 analyze 任务
（单 worker 串行），完成后 `GET /api/analyze/{token}` 取结果、
`GET /api/analyze/{token}/face_{n}.jpg` 取面容缩略图。
分析完全不触碰主库（见 services/analyzer 隔离语义）。
"""
from __future__ import annotations

import shutil
import uuid

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.config import settings
from app.db.session import get_db
from app.services import analyzer
from app.services.jobs import job_detail, submit_job

router = APIRouter(prefix="/analyze", tags=["analyze"])

_IMAGE_EXT = {".jpg", ".jpeg"}


def _inbox_path(filename: str):
    return analyzer.inbox_root() / filename


@router.post("/upload")
async def upload_video(file: UploadFile) -> dict:
    """上传外部视频到投放目录（流式写盘，支持大文件）；返回存储文件名。"""
    if not file.filename:
        raise HTTPException(status_code=400, detail={
            "code": "NO_FILENAME", "message": "upload requires a filename"})
    inbox = analyzer.inbox_root()
    inbox.mkdir(parents=True, exist_ok=True)
    stored = f"{uuid.uuid4().hex[:8]}_{analyzer.safe_inbox_name(file.filename)}"
    dest = inbox / stored
    size = 0
    with dest.open("wb") as f:
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            f.write(chunk)
    return {"filename": stored, "original": file.filename, "size_bytes": size}


@router.get("/inbox")
def list_inbox() -> dict:
    """投放目录内的待分析文件（排除图片等非视频杂项）。"""
    inbox = analyzer.inbox_root()
    items = []
    if inbox.is_dir():
        for p in sorted(inbox.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            if p.is_file() and p.suffix.lower() not in _IMAGE_EXT:
                items.append({"filename": p.name,
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
    job = submit_job(db, "analyze", {"token": token, "path": str(src),
                                     "filename": filename})
    return {"token": token, "job": job_detail(job)}


@router.get("/list")
def list_all() -> dict:
    return {"items": analyzer.list_analyses()}


@router.get("/{token}")
def get_result(token: str) -> dict:
    result = analyzer.load_result(token)
    if result is None:
        raise HTTPException(status_code=404, detail={
            "code": "ANALYSIS_NOT_FOUND",
            "message": "analysis not found or still running (check jobs)"})
    return result


@router.get("/{token}/face_{idx}.jpg")
def get_face_thumb(token: str, idx: int) -> FileResponse:
    p = analyzer.analyze_root() / token / f"face_{idx}.jpg"
    if not p.is_file():
        raise HTTPException(status_code=404, detail={
            "code": "THUMB_NOT_FOUND", "message": "face thumbnail not found"})
    return FileResponse(p, media_type="image/jpeg",
                        headers={"Cache-Control": "immutable"})


@router.delete("/{token}")
def delete_analysis(token: str) -> dict:
    """删除分析结果与其源文件（外部临时数据，不影响主库）。"""
    d = analyzer.analyze_root() / token
    result = analyzer.load_result(token)
    if not d.exists() and result is None:
        raise HTTPException(status_code=404, detail={
            "code": "ANALYSIS_NOT_FOUND", "message": "analysis not found"})
    shutil.rmtree(d, ignore_errors=True)
    if result:
        # 源文件统一在 inbox 下（结果里的 path 是它的绝对路径）
        src = _inbox_path(result.get("filename", ""))
        if src.is_file():
            src.unlink(missing_ok=True)
    return {"ok": True}
