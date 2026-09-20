"""HLS 会话播放（R8，ADR-029）：移动端播放修复的传输层。

背景（用户实测，iOS Safari 为主）：移动浏览器起播前先发 `Range: bytes=0-1`
探测，服务器必须回 206 Partial Content；stream 端点的 fMP4 管道流无 Range
支持（200 + chunked），remux 与 transcode 两档在 iOS 上双双失败（MediaError
code 4「源文件不存在或格式不支持」）。Jellyfin/Emby 等成熟媒体服务在移动端
一律改走 **HLS**：iOS Safari 原生支持 HLS（video.src = *.m3u8，不要求
Range）；Android/桌面 Chromium 由 hls.js（MSE）接管。ADR-027 遗留的"渐进流
时长只能估计、远距 seek 被浏览器钳制"也由 HLS 切片天然根治（ADR-025 后续项）。

会话模型（无状态 URL，单进程内存登记）：
- URL：`/api/videos/{id}/hls/{token}/index.m3u8?ss=&fallback=`，token 由前端
  每次起流/热切换生成（新 token = 新会话）；segment 相对引用落在同 token
  目录下，由 FileResponse 提供（**segment 天然支持 Range**）；
- ffmpeg `-f hls -hls_segment_type mpegts`（MPEG-TS 切片，兼容性最稳，见
  SEGMENT_NAME_RE 注释）以会话目录为 CWD 启动（裸文件名 + cwd 指向会话目录
  保证 playlist/切片全部正确落盘）；
- `-hls_playlist_type event`：转码推进时 playlist 持续追加、结束后写
  ENDLIST——播放端在 event 阶段周期性重拉 playlist，该心跳即"客户端存活"
  信号，看门狗据此回收僵尸会话；
- 回收：playlist 轮询超时（客户端离开 → kill ffmpeg；ffmpeg 异常退出且无
  ENDLIST 时补写 ENDLIST 让播放端干净停住）→ 目录 TTL 后删除；
- 同视频新会话挤掉旧会话（播放端同一时刻只看一条流，快速 seek 热切换的旧
  ffmpeg 立即回收）；转码档复用 streamer 的并发信号量与幂等槽位。
"""
from __future__ import annotations

import logging
import re
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from app.services import streamer
from app.services.frame_sampler import locate_ffmpeg

log = logging.getLogger("sophos.hls")

# token 出现在 URL 路径里（拼会话目录名），白名单字符防路径穿越
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
# MPEG-TS 切片（R8 定稿）：fMP4 切片实测被 Chromium MSE 误拒（SourceBuffer
# error：init 带 edts/sidx 的 ffmpeg HLS fMP4 输出，hls.js 344 次 append 全部
# 失败；同内容连续 fMP4 渐进流却可正常解码）；TS 切片是 iOS 原生 HLS 的经典
# 形态、hls.js 最成熟的解封装路径，也是 Jellyfin 转码的默认切片格式。
SEGMENT_NAME_RE = re.compile(r"^seg_\d+\.ts$")

_HLS_TIME_SEC = 4          # 切片目标时长（event 心跳间隔 ≈ 1~3 倍此值）
PLAYLIST_WAIT_SEC = 20.0   # 起播预检：等待首个切片落盘（弱 CPU 转码首片 ~10s）
POLL_KILL_SEC = 120.0      # playlist 心跳超时 → kill ffmpeg（客户端已离开/弃用）
DIR_TTL_SEC = 900.0        # 心跳停止后目录保留时长（断播回看），到点删除
MAX_SESSIONS = 4           # 全局会话上限（新会话挤掉最旧）
JANITOR_INTERVAL_SEC = 15.0

_sessions: dict[str, "HlsSession"] = {}
_registry_lock = threading.Lock()
_janitor_started = False


@dataclass
class HlsSession:
    """一个 HLS 会话 = 一个 ffmpeg 切片进程 + 会话目录。

    槽位（转码档）语义与 streamer.iter_ffmpeg_pipe 一致：全程持有、
    _StreamSlot 幂等释放（生成器收尾/看门狗 kill 双路只放一次）。
    """

    video_id: int
    token: str
    mode: str            # remux | transcode（HLS 无 direct 档）
    start_sec: float
    src: str             # 源文件绝对路径（ffmpeg cwd=会话目录，须绝对）
    dir: Path
    proc: subprocess.Popen | None = None
    errf: tempfile.TemporaryFile | None = None
    slot: streamer._StreamSlot | None = None
    created: float = field(default_factory=time.monotonic)
    last_poll: float = field(default_factory=time.monotonic)
    endlist_written: bool = False  # 看门狗为异常退出补写 ENDLIST 的去重旗标
    lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def key(self) -> str:
        return f"{self.video_id}:{self.token}"

    @property
    def playlist_path(self) -> Path:
        return self.dir / "index.m3u8"


def session_dir(settings, video_id: int, token: str) -> Path:
    return Path(settings.data_dir) / "tmp" / "hls" / f"{video_id}.{token}"


def build_hls_cmd(mode: str, src: str, preset: str = "veryfast", crf: int = 23,
                  start_sec: float | None = None) -> list[str]:
    """构造 HLS 切片命令。与 streamer.build_ffmpeg_cmd 的分段/seek 约定一致，
    输出为 MPEG-TS 切片（兼容性定稿见 SEGMENT_NAME_RE 注释）。注意 ffmpeg 须
    以会话目录为 CWD 启动且用裸文件名（切片/playlist 落盘于会话目录）。"""
    exe = locate_ffmpeg()
    if exe is None:
        raise RuntimeError(
            "ffmpeg not found (set SOPHOS_FFMPEG_EXE, install to PATH, "
            "or put ffmpeg under tools/ffmpeg-*/bin)")
    cmd = [str(exe), "-hide_banner", "-nostdin", "-loglevel", "error"]
    if start_sec is not None and start_sec > 0:
        cmd += ["-ss", f"{float(start_sec):.3f}"]
    cmd += ["-i", str(src), "-map", "0:v:0", "-map", "0:a:0?"]
    if mode == "remux":
        # TS→TS 纯 copy（AAC 本就是 ADTS，无需任何 bsf）
        cmd += ["-c", "copy"]
    else:  # transcode
        # -pix_fmt yuv420p：同 streamer——10bit 源默认转出 High10，浏览器不解；
        # -ac 2：HLS 档下混立体声——5.1 AAC 经 hls.js 重封装进 MSE 会被
        # SourceBuffer 拒绝（实测 audio SourceBuffer error 循环，Chromium）；
        # 网页/移动客户端按惯例给立体声（同 Jellyfin 转码默认），渐进档不变。
        cmd += ["-c:v", "libx264", "-preset", preset, "-crf", str(crf),
                "-pix_fmt", "yuv420p", "-ac", "2", "-c:a", "aac"]
    cmd += ["-f", "hls", "-hls_time", str(_HLS_TIME_SEC),
            "-hls_playlist_type", "event", "-hls_list_size", "0",
            "-hls_segment_type", "mpegts",
            "-hls_segment_filename", "seg_%05d.ts", "index.m3u8"]
    return cmd


def get_or_start(video_id: int, token: str, src: str, mode: str,
                 start_sec: float, preset: str = "veryfast",
                 crf: int = 23) -> HlsSession:
    """取现有会话或起一条新 ffmpeg。同视频旧会话与超限最旧会话被挤掉。"""
    from app.config import settings

    global _janitor_started
    with _registry_lock:
        key = f"{video_id}:{token}"
        sess = _sessions.get(key)
        if sess is not None:
            sess.last_poll = time.monotonic()
            return sess

        # 新会话：同视频旧会话必被替换（播放端唯一消费者已换流）
        for other in [s for s in _sessions.values() if s.video_id == video_id]:
            _kill_locked(other, "superseded by new token")
        while len(_sessions) >= MAX_SESSIONS:
            oldest = min(_sessions.values(), key=lambda s: s.last_poll)
            _kill_locked(oldest, "session cap reached")

        outdir = session_dir(settings, video_id, token)
        shutil.rmtree(outdir, ignore_errors=True)
        outdir.mkdir(parents=True, exist_ok=True)

        sem = streamer._concurrency_semaphore() if mode == "transcode" else None
        slot = None
        if sem is not None:
            if not sem.acquire(timeout=streamer.STREAM_SEM_WAIT_SEC):
                shutil.rmtree(outdir, ignore_errors=True)
                raise streamer.StreamBusy(
                    f"transcode slot busy (waited {streamer.STREAM_SEM_WAIT_SEC:.0f}s; "
                    f"another stream is using the concurrency slot)")
            slot = streamer._StreamSlot(sem)

        try:
            cmd = build_hls_cmd(mode, src, preset=preset, crf=crf,
                                start_sec=start_sec or None)
        except RuntimeError:
            if slot is not None:
                slot.release()
            shutil.rmtree(outdir, ignore_errors=True)
            raise

        sess = HlsSession(video_id=video_id, token=token, mode=mode,
                          start_sec=start_sec, src=src, dir=outdir, slot=slot)
        sess.errf = tempfile.TemporaryFile()
        # cwd=会话目录：init/seg/playlist 全部落盘于此，EXT-X-MAP 引用裸文件名
        sess.proc = subprocess.Popen(cmd, cwd=str(outdir), stdout=subprocess.DEVNULL,
                                     stderr=sess.errf)
        _sessions[key] = sess
        if not _janitor_started:
            _janitor_started = True
            threading.Thread(target=_janitor_loop, name="hls-janitor",
                             daemon=True).start()
        log.info("hls session start: video_id=%s token=%s mode=%s ss=%.2f",
                 video_id, token, mode, start_sec)
        return sess


def wait_playlist(sess: HlsSession, timeout: float = PLAYLIST_WAIT_SEC) -> tuple[bool, str]:
    """起播预检：等待 playlist 落盘（ffmpeg 完成首个切片）。ffmpeg 先死后返回
    stderr 尾部供诊断；超时说明首片过慢（弱 CPU），客户端可稍后重试。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if sess.playlist_path.is_file() and sess.playlist_path.stat().st_size > 0:
                return True, ""
        except OSError:
            pass
        ret = sess.proc.poll() if sess.proc is not None else None
        if ret is not None:
            return False, _stderr_tail(sess, prefix=f"hls {sess.mode} exited (code {ret})")
        time.sleep(0.1)
    return False, f"hls playlist not ready after {timeout:.0f}s (first segment too slow)"


def read_playlist(sess: HlsSession) -> bytes:
    """读取 playlist 当前内容（event 模式下 ffmpeg 周期重写整文件；
    读与写并发可能撞上瞬间的截断态，调用方对空/残缺结果自行重试）。"""
    return sess.playlist_path.read_bytes()


def _stderr_tail(sess: HlsSession, prefix: str = "") -> str:
    tail = ""
    try:
        if sess.errf is not None:
            sess.errf.seek(0)
            tail = sess.errf.read()[-500:].decode("utf-8", errors="replace")
    except (OSError, ValueError):
        pass
    body = f"{prefix}: {tail.strip()}" if prefix else tail.strip()
    return body or prefix or "unknown hls error"


def _kill_locked(sess: HlsSession, reason: str) -> None:
    """registry_lock 内调用：杀进程、释放槽位、移出登记。目录留给 TTL 清理。"""
    proc = sess.proc
    if proc is not None and proc.poll() is None:
        proc.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.SubprocessError:
            pass
    if sess.slot is not None:
        sess.slot.release()
        sess.slot = None
    _sessions.pop(sess.key, None)
    log.info("hls session killed: video_id=%s token=%s reason=%s",
             sess.video_id, sess.token, reason)


def _append_endlist(sess: HlsSession) -> None:
    """ffmpeg 异常退出且 playlist 无 ENDLIST 时补写，播放端不再无限等待。"""
    with sess.lock:
        if sess.endlist_written or not sess.playlist_path.is_file():
            return
        try:
            content = sess.playlist_path.read_text(encoding="utf-8",
                                                   errors="replace")
            if "#EXT-X-ENDLIST" not in content:
                with sess.playlist_path.open("a", encoding="utf-8") as f:
                    f.write("#EXT-X-ENDLIST\n")
            sess.endlist_written = True
        except OSError:
            pass


def _janitor_loop() -> None:
    while True:
        time.sleep(JANITOR_INTERVAL_SEC)
        try:
            _janitor_pass()
        except Exception:  # noqa: BLE001
            log.exception("hls janitor pass failed")


def _janitor_pass() -> None:
    from app.config import settings

    now = time.monotonic()
    with _registry_lock:
        for sess in list(_sessions.values()):
            idle = now - sess.last_poll
            ret = sess.proc.poll() if sess.proc is not None else None
            if ret is not None and ret != 0:
                # 异常退出：补 ENDLIST 让播放端干净停住；无 playlist（无任何
                # 可续读内容）立即回收，有存量切片则按 TTL 窗口保留
                _append_endlist(sess)
                if not sess.playlist_path.is_file():
                    _kill_locked(sess, "dead proc without playlist")
                elif idle > DIR_TTL_SEC:
                    _kill_locked(sess, f"dead proc, dir ttl ({idle:.0f}s idle)")
                continue
            if idle > POLL_KILL_SEC:
                # 心跳停止：客户端已离开（event 播放端周期重拉 playlist）。
                # proc 已自然结束则只做登记清理；仍在跑则 kill。
                _kill_locked(sess, f"playlist poll timeout ({idle:.0f}s idle)")
    # 目录兜底清理：登记已消失（上次会话 kill 后）但目录仍在的。
    # 注意 st_mtime 是 wall-clock，这里必须用 time.time() 比较。
    base = Path(settings.data_dir) / "tmp" / "hls"
    if base.is_dir():
        cutoff = time.time() - DIR_TTL_SEC
        for d in base.iterdir():
            try:
                if d.is_dir() and d.stat().st_mtime < cutoff:
                    shutil.rmtree(d, ignore_errors=True)
            except OSError:
                pass


def cleanup_stale_dirs() -> None:
    """启动时清理上次进程遗留的会话目录（进程重启后无人引用）。"""
    from app.config import settings

    base = Path(settings.data_dir) / "tmp" / "hls"
    if base.is_dir():
        shutil.rmtree(base, ignore_errors=True)
    base.mkdir(parents=True, exist_ok=True)


def shutdown_all() -> None:
    """进程退出：杀掉全部 ffmpeg。"""
    with _registry_lock:
        for sess in list(_sessions.values()):
            _kill_locked(sess, "shutdown")


def touch(sess: HlsSession) -> None:
    sess.last_poll = time.monotonic()


def get_session(video_id: int, token: str) -> HlsSession | None:
    return _sessions.get(f"{video_id}:{token}")
