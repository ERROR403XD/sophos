"""视频在线播放（M2 直出；R1 重构为三档，ADR-015）。

按**编码能力**分档，而不是按扩展名一刀切（P4：mkv 播放失败根因）：

| 档位 | 条件 | 实现 | 体验 |
|---|---|---|---|
| direct | 容器兼容(mp4/m4v/webm/mov) 且编码兼容（探测失败时乐观直出，保持 M2 行为） | FileResponse（真 Range，可拖动） | 最佳 |
| remux  | 容器不兼容(mkv/avi/ts…) 但 vcodec=h264 且 acodec=aac/无音轨 | ffmpeg -c copy → fMP4 管道 | 秒开，CPU≈0 |
| transcode | 编码不兼容（hevc/mpeg4/…）或探测明确不支持 | ffmpeg libx264+aac → fMP4 管道 | 启动延迟数秒，CPU 高 |

通用约定：
- remux/transcode 走 StreamingResponse 管道（fMP4，无 HTTP Range，浏览器渐进 seek）；
  **客户端断开时终止 ffmpeg 子进程**（生成器 close → finally kill，防僵尸进程）。
- 转码并发限流 SOPHOS_TRANSCODE_MAX_CONCURRENCY（默认 1，Semaphore）。
- 响应头 X-Sophos-Stream-Mode: direct|remux|transcode 供前端提示。
- SOPHOS_TRANSCODE_ENABLED=false 时 remux/transcode 退回 415。
- ffprobe 探测失败且扩展名不在白名单 → 415（诊断信息注明探测失败）。
"""
from __future__ import annotations

import json
import subprocess
import tempfile
import threading
from pathlib import Path

from app.services.frame_sampler import locate_ffmpeg, locate_ffprobe

MIME_BY_EXT = {
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".webm": "video/webm",
    ".mov": "video/quicktime",
}

# 直出（浏览器原生容器+编码）；探测失败时白名单容器仍乐观直出（M2 兼容）
DIRECT_CONTAINER_EXTS = set(MIME_BY_EXT)
DIRECT_VCODEC = {"h264", "vp9"}
DIRECT_ACODEC = {"aac", "opus"}

# 重封装：编码本就浏览器兼容，只是容器不认（P4 mkv 主场景）
REMUX_VCODEC = {"h264"}
REMUX_ACODEC = {"aac", ""}  # 无音轨同样可 copy

_STREAM_MODE_HEADER = "X-Sophos-Stream-Mode"


def media_type_for(path: str) -> str | None:
    return MIME_BY_EXT.get(Path(path).suffix.lower())


def probe_codec(video_path: str | Path) -> tuple[str | None, str | None]:
    """ffprobe 探测 (vcodec, acodec)；任何失败返回 (None, None)（调用方决定回退）。"""
    exe = locate_ffprobe()
    if exe is None:
        return None, None
    cmd = [str(exe), "-v", "error", "-show_entries", "stream=codec_type,codec_name",
           "-of", "json", str(video_path)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None, None
    if proc.returncode != 0:
        return None, None
    try:
        streams = json.loads(proc.stdout).get("streams", [])
    except ValueError:
        return None, None
    vcodec = acodec = None
    for st in streams:
        ct, cn = st.get("codec_type"), st.get("codec_name")
        if ct == "video" and vcodec is None:
            vcodec = cn
        elif ct == "audio" and acodec is None:
            acodec = cn
    return vcodec, acodec


def decide_mode(path: str, vcodec: str | None, acodec: str | None,
                transcode_enabled: bool = True) -> str:
    """播放分档决策 → direct | remux | transcode | unsupported。

    - 探测失败（vcodec 为空）：白名单容器乐观直出；否则 unsupported（415，注明探测失败）。
    - 容器+编码都兼容 → direct；编码兼容容器不兼容 → remux；其余 → transcode
      （transcode_enabled=false 时 remux/transcode 一律 unsupported，回退 M2 行为）。
    """
    ext = Path(path).suffix.lower()
    v, a = (vcodec or "").lower(), (acodec or "").lower()

    if ext in DIRECT_CONTAINER_EXTS:
        if not v:  # 探测失败：容器本身兼容，乐观直出（历史行为）
            return "direct"
        if v in DIRECT_VCODEC and (not a or a in DIRECT_ACODEC):
            return "direct"
    else:
        if not v:  # 探测失败且扩展名不在白名单
            return "unsupported"
        if v in REMUX_VCODEC and a in REMUX_ACODEC:
            if transcode_enabled:
                return "remux"
            return "unsupported"
    if not transcode_enabled:
        return "unsupported"
    return "transcode"


# ---------------- ffmpeg fMP4 管道（remux/transcode 共用） ----------------

_transcode_sem: threading.Semaphore | None = None
_sem_lock = threading.Lock()
_CHUNK = 64 * 1024


class StreamFailure(RuntimeError):
    """ffmpeg 管道失败（R4，ADR-020）：携带 return_code / stderr 尾部供 API 层
    转为 STREAM_FAILED / TRANSCODE_FAILED 错误响应，不再伪装成 404/静默截断。"""

    def __init__(self, message: str, return_code: int | None = None,
                 stderr_tail: str = ""):
        super().__init__(message)
        self.return_code = return_code
        self.stderr_tail = stderr_tail


class StreamBusy(StreamFailure):
    """转码并发信号量等待超时（R6，ADR-027）：另一路转码流占住通道。
    API 层转 503 STREAM_BUSY，前端提示而不是无限转圈。"""

    def __init__(self, message: str = "transcode slot busy"):
        super().__init__(message, return_code=None, stderr_tail=message)


STREAM_SEM_WAIT_SEC = 15.0


class _StreamSlot:
    """并发槽位（幂等释放，R6/ADR-027）。

    信号量全程持有后，释放路径有两条且都可能出现：
    - 生成器 finally（正常放完 / Starlette 关闭生成器 / 断连 kill 后管道 EOF）；
    - 断连看门狗（videos.py stream 端点）：实测（Starlette 1.6 spec≥2.4）客户端
      断连时 `stream_response` 以 OSError 结束、**同步生成器不会被关闭**——它
      永远挂在 yield 上，finally 不执行 → 信号量泄漏（次条流 503，受控复现）。
      看门狗 kill ffmpeg 后必须自己兜底释放。
    两条路径都可能都走到（kill → EOF → 生成器收尾），故用旗标保证只放一次。
    """

    def __init__(self, sem: threading.Semaphore):
        self._sem = sem
        self._lock = threading.Lock()
        self._released = False

    def release(self) -> None:
        with self._lock:
            if self._released:
                return
            self._released = True
        self._sem.release()


def _concurrency_semaphore() -> threading.Semaphore:
    from app.config import settings

    global _transcode_sem
    with _sem_lock:
        if _transcode_sem is None:
            _transcode_sem = threading.Semaphore(max(1, settings.transcode_max_concurrency))
        return _transcode_sem


def build_ffmpeg_cmd(mode: str, src: str | Path, ffmpeg_exe: str = "",
                     preset: str = "veryfast", crf: int = 23,
                     start_sec: float | None = None) -> list[str]:
    """构造 remux/transcode 命令：fMP4 输出到 stdout。

    -map 0:v:0 -map 0:a:0? 只取首视频流+可选首音频流——mkv 常见字幕/附件轨
    进 mp4 会 mux 失败；remux 全程 -c copy（零转码）。
    start_sec（R5）：服务端 seek —— fMP4 管道流无 HTTP Range，浏览器 seek 到
    未缓冲位置会触发注定失败的 Range 请求（表现为 MediaError 解码失败）；
    改由前端拦截 seek 后带 ?ss= 重新起流，此处作为输入选项置于 -i 之前
    （ffmpeg 输入 seek，转码档精确、copy 档对齐关键帧），输出时间戳从 0 重排。
    """
    exe = locate_ffmpeg(ffmpeg_exe)
    if exe is None:
        raise RuntimeError(
            "ffmpeg not found (set SOPHOS_FFMPEG_EXE, install to PATH, "
            "or put ffmpeg under tools/ffmpeg-*/bin)")
    cmd = [str(exe), "-hide_banner", "-nostdin", "-loglevel", "error"]
    if start_sec is not None and start_sec > 0:
        cmd += ["-ss", f"{float(start_sec):.3f}"]
    cmd += ["-i", str(src), "-map", "0:v:0", "-map", "0:a:0?"]
    if mode == "remux":
        # R7：aac_adtstoasc —— TS/MPEGPS 容器的 AAC 是 ADTS 裸流，直接 copy 进
        # MP4 会报 "Malformed AAC bitstream detected" 且 ffmpeg 立即中止
        #（实测整条流只剩 ftyp+moov 约 1.2KB，前端表现为永远转圈）；mkv 等容器
        # 的 AAC 本就是 ASC，该 bsf 透传无副作用（TS+mkv 双向实测）。
        cmd += ["-c", "copy", "-bsf:a", "aac_adtstoasc"]
    else:  # transcode
        # R8：强制 8bit 4:2:0 输出——10bit 源（如 x265 10bit mkv）默认转出
        # High10（pix_fmt 跟随输入），所有浏览器都无法解码（实测 MediaError
        # code 4，且降级重转同样 High10 永远失败）；浏览器本也不解 10bit。
        cmd += ["-c:v", "libx264", "-preset", preset, "-crf", str(crf),
                "-pix_fmt", "yuv420p", "-c:a", "aac"]
    cmd += ["-movflags", "frag_keyframe+empty_moov", "-f", "mp4", "pipe:1"]
    return cmd


def iter_ffmpeg_pipe(cmd: list[str], mode: str = "pipe",
                     proc_holder: dict | None = None):
    """执行 ffmpeg 并逐块产出 stdout；transcode 受并发信号量限流（流生命周期全程持有）。

    R6 语义修订（ADR-027）：R5.2 曾改为"首块交付即释放"以规避僵尸 ffmpeg
    永久占满信号量——但副作用是**起播后的存续期不限流**：快速拖动进度条时
    每 250ms 就能起一条新转码流（旧流靠看门狗回收），弱 CPU 上多条
    1080p x265→h264 并行打满整机，Web 服务一起失去响应（用户实测复现）。
    现恢复全程持有：R5.2 同时落地的**断连看门狗**（stream 端点轮询
    is_disconnected → 直接 kill ffmpeg → 管道 EOF → 生成器收尾）已把
    僵尸窗口压到亚秒级，泄漏风险有界，限流语义可以回归正确。

    R7：remux（-c copy，CPU≈0）**不再占用转码并发槽**——信号量保护的是
    转码 CPU；remux 全程占槽曾使"一路 remux 播放中 + 打开下一条流"等待
    15s（长转圈）甚至 503 STREAM_BUSY。remux 快速拖动的并发流成本可忽略，
    僵尸回收仍由断连看门狗负责。

    客户端断开 → 看门狗 kill / Starlette 关闭生成器 → GeneratorExit →
    finally 终止子进程并释放信号量（transcode）。
    stderr 落临时文件：失败时取出尾部用于诊断（随 StreamFailure 抛出）。
    """
    sem = _concurrency_semaphore() if mode == "transcode" else None
    if sem is not None and not sem.acquire(timeout=STREAM_SEM_WAIT_SEC):
        raise StreamBusy(
            f"transcode slot busy (waited {STREAM_SEM_WAIT_SEC:.0f}s; "
            f"another stream is using the concurrency slot)")
    slot = _StreamSlot(sem) if sem is not None else None
    if proc_holder is not None:
        # 看门狗断连兜底释放（幂等）；remux 无槽位时为 no-op，kill 仍生效
        proc_holder["release"] = slot.release if slot is not None else (lambda: None)
    proc: subprocess.Popen | None = None
    try:
        with tempfile.TemporaryFile() as errf:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=errf)
            if proc_holder is not None:
                proc_holder["proc"] = proc
            try:
                while True:
                    chunk = proc.stdout.read1(_CHUNK)
                    if not chunk:
                        break
                    yield chunk
                ret = proc.wait(timeout=30)
                if ret != 0:
                    errf.seek(0)
                    tail = errf.read()[-500:].decode("utf-8", errors="replace")
                    raise StreamFailure(f"{mode} failed (code {ret}): {tail}",
                                        return_code=ret, stderr_tail=tail)
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=5)
    finally:
        if slot is not None:
            slot.release()


def stream_mode_header(mode: str) -> dict[str, str]:
    return {_STREAM_MODE_HEADER: mode}
