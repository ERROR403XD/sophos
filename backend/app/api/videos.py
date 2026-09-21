"""视频列表 / 详情 / Range 流式播放（M2；R1 三档 ADR-015；R4 stream_url 契约 + 错误码分类 ADR-020；
R8 HLS 会话端点 ADR-029）。

分数字段由 M4 的 aggregator 填充；当前 LEFT JOIN video_score 返回 null。

R4（ADR-020）：
- 列表/详情返回 stream_url（后端是 stream endpoint 的唯一真源，前端不再自行拼接）；
- 404 分型：VIDEO_NOT_FOUND（库中无此 id）/ SOURCE_NOT_FOUND（源文件已不存在）；
- ffmpeg 未安装 → FFMPEG_NOT_FOUND（500）；remux/transcode 起播即失败 →
  STREAM_FAILED / TRANSCODE_FAILED（500，带 stderr 尾部）；流中途失败记录完整
  上下文日志（video_id/source/mode/cmd/return_code/stderr tail）。

R8（ADR-029）：`hls_url` + HLS 会话端点——移动端（iOS Safari 必须_RANGE_206，
fMP4 管道流 200+chunked 起播即败）改走 HLS：playlist/segment 由文件服务提供
（segment 天然 Range 兼容），会话生命周期见 services/hls.py。
"""
import asyncio
import logging
import re
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.config import settings
from app.db.models import Video, VideoScore
from app.db.session import get_db
from app.services import hls, streamer

router = APIRouter(prefix="/videos", tags=["videos"])

log = logging.getLogger("sophos.videos")

_SORTABLE = {
    "final_score": VideoScore.final_score,
    "base_final": VideoScore.base_final,
    "updated_at": Video.updated_at,
    "filename": Video.filename,
    "size_bytes": Video.size_bytes,
    "duration_sec": Video.duration_sec,
}
SORTABLE = _SORTABLE  # 兼容既有引用/测试

_HLS_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_HLS_SEGMENT_RE = re.compile(r"^seg_\d+\.ts$")


def _score_payload(score: VideoScore | None) -> dict:
    # 注意：不含 identity_count，避免覆盖 video 行的面容数（M2 曾因此返回 null）
    return {
        "final_score": score.final_score if score else None,
        "base_final": score.base_final if score else None,
        "personalized_final": score.personalized_final if score else None,
        "score_updated_at": score.updated_at if score else None,
    }


def _item(video: Video, score: VideoScore | None) -> dict:
    return {
        "id": video.id,
        "path": video.path,
        "filename": video.filename,
        "dir_path": video.dir_path,
        "duration_sec": video.duration_sec,
        "size_bytes": video.size_bytes,
        "status": video.status,
        "identity_count": video.identity_count,
        "container": video.container,  # R9：实测容器（扩展名可能骗人）
        "stream_mode": streamer.decide_mode(
            video.path, video.vcodec, video.acodec, settings.transcode_enabled,
            container=video.container),
        # R4(ADR-020)：stream URL 由后端统一下发（前端不自行拼接）
        "stream_url": f"/api/videos/{video.id}/stream",
        # R8(ADR-029)：HLS 会话端点基路径（前端拼 {token}/index.m3u8?ss=&fallback=）
        "hls_url": f"/api/videos/{video.id}/hls",
        "updated_at": video.updated_at,
        **_score_payload(score),
    }


@router.get("")
def list_videos(page: int = 1, page_size: int = 50, q: str | None = None,
                status: str | None = None, library: str | None = None,
                sort: str = "final_score", order: str = "desc",
                db: Session = Depends(get_db)) -> dict:
    """R1(P5)：library = 工作目录（dir_path）精确匹配，与 q/status 叠加。"""
    page = max(1, page)
    page_size = min(max(1, page_size), 200)
    filters = []
    if q:
        filters.append(Video.filename.contains(q))
    if status:
        filters.append(Video.status == status)
    if library:
        filters.append(Video.dir_path == library)

    count_stmt = select(func.count()).select_from(Video)
    for f in filters:
        count_stmt = count_stmt.where(f)
    total = db.execute(count_stmt).scalar_one()

    stmt = select(Video, VideoScore).outerjoin(VideoScore, VideoScore.video_id == Video.id)
    for f in filters:
        stmt = stmt.where(f)
    col = SORTABLE.get(sort, SORTABLE["final_score"])
    stmt = stmt.order_by(col.desc() if order == "desc" else col.asc(), Video.id.desc())
    rows = db.execute(stmt.offset((page - 1) * page_size).limit(page_size)).all()
    # R9（ADR-030）：本页容器未知的行惰性补嗅探并回写（上限=页大小 ≤200，单次
    # 读 600B）。用户在列表里看到的 play 方式与实际播放档位由此保持一致——
    # 否则".mp4 实为 TS"在列表上仍显示"直出"，移动端据此走渐进流白失败一轮。
    changed = False
    for v, _s in rows:
        if v.container is None:
            v.container = streamer.sniff_container(v.path)
            changed = True
    if changed:
        db.commit()
    return {"items": [_item(v, s) for v, s in rows],
            "total": total, "page": page, "page_size": page_size}


@router.get("/libraries")
def list_libraries(db: Session = Depends(get_db)) -> dict:
    """R1(P5)：库下拉选项 = DB 内 distinct dir_path（含已删除工作目录的历史库）。

    前端与 GET /api/workdirs 的当前库取并集展示。
    """
    rows = db.execute(select(Video.dir_path).distinct().order_by(Video.dir_path)).all()
    return {"libraries": [r[0] for r in rows]}


@router.get("/{video_id}")
def get_video(video_id: int, db: Session = Depends(get_db)) -> dict:
    video = db.get(Video, video_id)
    if video is None:
        raise HTTPException(status_code=404, detail={
            "code": "VIDEO_NOT_FOUND", "message": f"video {video_id} not found",
        })
    return _item(video, db.get(VideoScore, video_id))


def _load_playable_video(video_id: int, db: Session) -> tuple[Video, Path, str | None, str | None]:
    """stream/hls 共用预检：取视频行（404 分型）+ 源文件存在性 + 编码/容器补探测回写。"""
    video = db.get(Video, video_id)
    if video is None:
        raise HTTPException(status_code=404, detail={
            "code": "VIDEO_NOT_FOUND", "message": f"video {video_id} not found",
        })
    p = Path(video.path)
    if not p.is_file():
        raise HTTPException(status_code=404, detail={
            "code": "SOURCE_NOT_FOUND",
            "message": "source video file no longer exists",
        })
    dirty = False
    # 编码缺失（旧数据/扫描期探测失败）→ 播放期再试并回写
    vcodec, acodec = video.vcodec, video.acodec
    if vcodec is None:
        probed = streamer.probe_codec(str(p))
        vcodec, acodec = probed
        video.vcodec, video.acodec = probed
        dirty = True
    # R9（ADR-030）：容器未知（老库/刚迁移）→ 播放前嗅探并回写。不回填的话
    # ".mp4 实为 TS" 又会走 direct 误判，用户必须先重扫才能播。
    if video.container is None:
        video.container = streamer.sniff_container(str(p))
        dirty = True
    if dirty:
        db.commit()
    return video, p, vcodec, acodec


def _resolve_mode(video: Video, vcodec: str | None, acodec: str | None,
                  fallback: int) -> str:
    """播放档位决策 + fallback=1 强制转码（ADR-027）；unsupported → 415 分型。"""
    mode = streamer.decide_mode(video.path, vcodec, acodec,
                                settings.transcode_enabled,
                                container=video.container)
    if fallback and mode in ("direct", "remux") and settings.transcode_enabled:
        mode = "transcode"
    if mode == "unsupported":
        detail = ("codec probe failed" if vcodec is None
                  else f"codec not supported: {vcodec}/{acodec or 'none'}")
        raise HTTPException(status_code=415, detail={
            "code": "FORMAT_UNSUPPORTED",
            "message": f"format not streamable: {Path(video.path).suffix} ({detail})"
                       + ("" if settings.transcode_enabled else "; transcoding disabled"),
        })
    return mode


@router.get("/{video_id}/stream")
async def stream_video(video_id: int, request: Request, ss: float | None = None,
                       fallback: int = 0, db: Session = Depends(get_db)):
    """播放三档自动选择（ADR-015）；R4 错误分型 + 起播预检（ADR-020）。

    R5：`ss`（秒）为服务端 seek，仅对 remux/transcode 管道流生效——这两档
    是无 HTTP Range 的 fMP4 渐进流，浏览器拖到未缓冲区域会发出注定失败的
    Range 请求并报 MediaError；前端拦截 seek 后带 ?ss= 重新起流。
    direct 档有真 Range，忽略 ss。

    R6：`fallback=1`（ADR-027）把 direct/remux 决策强制改走 transcode——
    用于前端播放失败的自动降级（h264 High10 等探测兼容但浏览器实际解不了
    的编码、扫描期探测失败的乐观直出、容器怪癖等）。transcode 关闭时维持
    原档不变（该部署形态明确不要转码）。

    R5.2 缺陷修复：端点改 async 并挂**断连看门狗**——客户端断开（关页/换流/
    重载）时直接 kill ffmpeg，防僵尸进程与管道线程泄漏（压测实测 1.7GB
    僵尸 ffmpeg 占满转码信号量导致后续播放全部挂死，见 STRESS_LOG）。
    R6：看门狗轮询 1s → 0.35s（快速拖动换流时更快回收旧 ffmpeg）。
    R7：remux 档补 aac_adtstoasc（TS 容器 AAC 为 ADTS，直接 copy 进 fMP4
    会报 Malformed AAC 并中止——实测整条流只剩 fMP4 头，前端永远转圈，
    见 streamer.build_ffmpeg_cmd）；remux 不再占用转码并发槽（-c copy 无
    CPU 压力，见 streamer.iter_ffmpeg_pipe）。残余死流（未知怪癖）无法在
    服务端预知——ffmpeg 写满管道缓冲后阻塞等消费者，退出时机取决于读取方
    ——由前端"无进展看门狗"兜底自动降级。
    """
    video, p, vcodec, acodec = _load_playable_video(video_id, db)
    mode = _resolve_mode(video, vcodec, acodec, fallback)

    if mode == "direct":
        return FileResponse(p, media_type=streamer.media_type_for(video.path),
                            headers=streamer.stream_mode_header(mode))

    # remux / transcode：ffmpeg → fMP4 管道（无 Range；X-Sophos-Stream-Mode 标注）
    start_sec = max(0.0, float(ss)) if ss is not None and ss > 0 else None
    try:
        cmd = streamer.build_ffmpeg_cmd(
            mode, str(p), ffmpeg_exe=settings.ffmpeg_exe,
            preset=settings.transcode_preset, start_sec=start_sec,
            max_height=settings.transcode_max_height)
    except RuntimeError as exc:
        # ffmpeg 可执行文件缺失：服务端配置问题（R4：不再是 415/404 语义）
        raise HTTPException(status_code=500, detail={  # noqa: B904
            "code": "FFMPEG_NOT_FOUND",
            "message": f"cannot stream: {exc}",
        })

    fail_code = "TRANSCODE_FAILED" if mode == "transcode" else "STREAM_FAILED"
    proc_holder: dict = {}
    gen = streamer.iter_ffmpeg_pipe(cmd, mode, proc_holder)
    try:
        # 起播预检：首块产出前失败 → 可转真实 HTTP 错误（阻塞读放线程池）
        first = await run_in_threadpool(lambda: next(gen, None))
    except streamer.StreamBusy as exc:
        raise HTTPException(status_code=503, detail={  # noqa: B904
            "code": "STREAM_BUSY",
            "message": "转码通道被另一路播放占用，请稍后重试或关闭其他播放",
        })
    except streamer.StreamFailure as exc:
        log.error("stream start failed: video_id=%s source=%s mode=%s "
                  "return_code=%s stderr_tail=%s cmd=%s",
                  video_id, video.path, mode, exc.return_code,
                  exc.stderr_tail.strip().replace("\n", " | "), cmd)
        raise HTTPException(status_code=500, detail={  # noqa: B904
            "code": fail_code,
            "message": f"{mode} failed: {exc.stderr_tail.strip()[-300:] or exc}",
        })

    # R7 说明：无法在服务端把"首块后很快死掉的流"转成 HTTP 错误——ffmpeg 写满
    # 管道缓冲后会阻塞等消费者排空，退出（进而死亡）只在有人持续读取时发生，
    # 起播预检无法预知（实测 wait(3s) 在 ffmpeg 阻塞写管道时只能得到 None）。
    # 残余的死流场景（未知编码怪癖等）由前端"无进展看门狗"兜底自动降级。

    async def _watch_disconnect() -> None:
        try:
            while True:
                if await request.is_disconnected():
                    proc = proc_holder.get("proc")
                    if proc is not None and proc.poll() is None:
                        log.info("stream client disconnected: kill ffmpeg "
                                 "video_id=%s mode=%s", video_id, mode)
                        proc.kill()
                    # R6（ADR-027）：断连路径上 Starlette（spec≥2.4）不会关闭同步
                    # 生成器——它永远挂在 yield 上，finally 的槽位释放不执行；
                    # 看门狗在此兜底释放（幂等，与生成器 finally 双路只放一次）
                    release = proc_holder.get("release")
                    if release is not None:
                        release()
                    return
                await asyncio.sleep(0.35)
        except asyncio.CancelledError:
            return

    watcher = asyncio.create_task(_watch_disconnect())

    def _chained():
        try:
            yield first
            yield from gen
        except streamer.StreamFailure as exc:
            # 响应头已发出（200），无法改状态码：记录完整上下文供定位
            log.error("stream interrupted: video_id=%s source=%s mode=%s "
                      "return_code=%s stderr_tail=%s cmd=%s",
                      video_id, video.path, mode, exc.return_code,
                      exc.stderr_tail.strip().replace("\n", " | "), cmd)
            raise
        finally:
            watcher.cancel()

    return StreamingResponse(_chained(), media_type="video/mp4",
                             headers=streamer.stream_mode_header(mode))


# ---------------- R8（ADR-029）：HLS 会话端点（移动端播放修复） ----------------
#
# GET  /api/videos/{id}/hls/{token}/index.m3u8?ss=&fallback=   playlist（event 型）
# GET  /api/videos/{id}/hls/{token}/{segment}                  init.mp4 / seg_*.m4s
#
# token 由前端每次起流/热切换随机生成：新 token = 新会话（同 ss 重复请求旧
# token 幂等续用已有会话）。会话由 services/hls.py 登记/挤占/回收；切片文件
# 经 FileResponse 下发（天然支持 Range——这正是 iOS Safari 起播的硬要求）。
# 注意路由顺序：playlist（字面量）必须先于 {segment} 通配注册。

@router.get("/{video_id}/hls/{token}/index.m3u8")
def hls_playlist(video_id: int, token: str, ss: float | None = None,
                 fallback: int = 0, db: Session = Depends(get_db)):
    """HLS playlist：起流（首片就绪后返回）或续读已回收会话的存量 playlist。

    - direct/remux 探测结果在 HLS 里统一映射为 remux（-c copy 切片，CPU≈0）；
      transcode → libx264+aac 切片（受转码并发信号量约束，忙时 503）。
    - `ss` 语义与 stream 端点一致：ffmpeg 输入 seek，playlist 时间轴从 0 重排。
    """
    if not _HLS_TOKEN_RE.match(token):
        raise HTTPException(status_code=400, detail={
            "code": "BAD_TOKEN", "message": "invalid hls session token",
        })
    video, p, vcodec, acodec = _load_playable_video(video_id, db)
    mode = _resolve_mode(video, vcodec, acodec, fallback)
    hls_mode = "transcode" if mode == "transcode" else "remux"

    start_sec = max(0.0, float(ss)) if ss is not None and ss > 0 else 0.0
    sess = hls.get_session(video_id, token)
    if sess is not None:
        hls.touch(sess)
    else:
        # 会话已回收（暂停/回看心跳超时）但目录仍在 TTL 窗口：续读存量切片，
        # 不再起第二条 ffmpeg（起流请换 token——前端每次热切换本就换新 token）
        existing = hls.session_dir(settings, video_id, token) / "index.m3u8"
        if existing.is_file():
            try:
                return Response(existing.read_bytes(),
                                media_type="application/vnd.apple.mpegurl",
                                headers={"Cache-Control": "no-store",
                                         **streamer.stream_mode_header(hls_mode)})
            except OSError:
                pass  # 目录恰好被 TTL 清掉 → 走正常起流

    try:
        sess = hls.get_or_start(video_id, token, str(p), hls_mode, start_sec,
                                preset=settings.transcode_preset,
                                max_height=settings.transcode_max_height)
    except streamer.StreamBusy as exc:
        raise HTTPException(status_code=503, detail={  # noqa: B904
            "code": "STREAM_BUSY",
            "message": "转码通道被另一路播放占用，请稍后重试或关闭其他播放",
        }) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail={  # noqa: B904
            "code": "FFMPEG_NOT_FOUND",
            "message": f"cannot stream: {exc}",
        }) from exc

    ok, err = hls.wait_playlist(sess)
    if not ok:
        if "superseded" in err:
            # R9：客户端已换流/关闭（快速退出、快速拖动）——这是正常的竞态收尾，
            # 不是服务端故障：安静地回 409，不打 error 日志（前端按 epoch 丢弃）。
            raise HTTPException(status_code=409, detail={
                "code": "STREAM_SUPERSEDED",
                "message": "播放会话已被新的起流请求替换",
            })
        log.error("hls playlist failed: video_id=%s token=%s mode=%s err=%s",
                  video_id, token, hls_mode, err.strip().replace("\n", " | "))
        raise HTTPException(status_code=500, detail={
            "code": "TRANSCODE_FAILED" if hls_mode == "transcode" else "STREAM_FAILED",
            "message": err.strip()[-300:] or "hls playlist failed",
        })
    try:
        content = hls.read_playlist(sess)
    except OSError as exc:
        raise HTTPException(status_code=500, detail={  # noqa: B904
            "code": "STREAM_FAILED", "message": f"playlist read failed: {exc}",
        }) from exc
    return Response(content=content, media_type="application/vnd.apple.mpegurl",
                    headers={"Cache-Control": "no-store",
                             **streamer.stream_mode_header(hls_mode)})


@router.get("/{video_id}/hls/{token}/{name}")
def hls_segment(video_id: int, token: str, name: str):
    """HLS 切片下发（seg_*.ts，MPEG-TS）。FileResponse 支持 Range——
    移动端（iOS Safari 起播探测 bytes=0-1 → 206）修复的关键传输差异。"""
    if not _HLS_TOKEN_RE.match(token) or not _HLS_SEGMENT_RE.match(name):
        raise HTTPException(status_code=400, detail={
            "code": "BAD_TOKEN", "message": "invalid hls session token or segment name",
        })
    sess = hls.get_session(video_id, token)
    if sess is not None:
        hls.touch(sess)
    path = hls.session_dir(settings, video_id, token) / name
    if not path.is_file():
        raise HTTPException(status_code=404, detail={
            "code": "HLS_SEGMENT_NOT_FOUND",
            "message": "segment expired or session not started",
        })
    return FileResponse(path, media_type="video/mp2t",
                        headers={"Cache-Control": "private, max-age=3600"})


@router.delete("/{video_id}/hls/{token}", status_code=204)
def hls_stop(video_id: int, token: str) -> Response:
    """显式结束 HLS 会话（R9）：前端关闭播放器/换视频时调用，立即 kill ffmpeg。

    不这样做的话，快速退出后转码 ffmpeg 会继续占满 CPU 直到 120s 心跳超时
    ——正是"退出播放后页面不定期失去响应"的一大来源。幂等：无会话返回 204。
    """
    if not _HLS_TOKEN_RE.match(token):
        raise HTTPException(status_code=400, detail={
            "code": "BAD_TOKEN", "message": "invalid hls session token",
        })
    hls.stop_session(video_id, token)
    return Response(status_code=204)
