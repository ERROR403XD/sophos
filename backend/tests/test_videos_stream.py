"""视频列表/详情/Range 流式播放测试（M2）；R1 追加播放三档（ADR-015）。"""
import subprocess

import pytest

from app.db.models import Video
from app.services.frame_sampler import locate_ffmpeg

FFMPEG = locate_ffmpeg() is not None


@pytest.fixture()
def video_file(tmp_path):
    f = tmp_path / "movie.mp4"
    content = bytes(range(256)) * 8  # 2048 字节伪数据（Range 语义与内容无关）
    f.write_bytes(content)
    return f, content


def _add_video(db, path, filename, size):
    v = Video(path=str(path), filename=filename, dir_path=str(path.parent),
              size_bytes=size, mtime=path.stat().st_mtime, status="done")
    db.add(v)
    db.commit()
    db.refresh(v)
    return v


def test_list_filter_and_detail(client, db, tmp_path, video_file):
    f, content = video_file
    v = _add_video(db, f, "movie.mp4", len(content))

    body = client.get("/api/videos", params={"q": "movie"}).json()
    assert body["total"] == 1
    assert body["items"][0]["id"] == v.id

    assert client.get("/api/videos", params={"q": "none"}).json()["total"] == 0

    detail = client.get(f"/api/videos/{v.id}").json()
    assert detail["filename"] == "movie.mp4"
    assert detail["final_score"] is None

    assert client.get("/api/videos/9999").status_code == 404


def test_stream_range_and_full(client, db, video_file):
    f, content = video_file
    v = _add_video(db, f, "movie.mp4", len(content))

    r = client.get(f"/api/videos/{v.id}/stream", headers={"Range": "bytes=0-99"})
    assert r.status_code == 206
    assert r.content == content[:100]
    assert r.headers["content-range"] == f"bytes 0-99/{len(content)}"

    full = client.get(f"/api/videos/{v.id}/stream")
    assert full.status_code == 200
    assert full.content == content


def test_stream_unsupported_and_missing(client, db, tmp_path):
    mkv = tmp_path / "x.mkv"
    mkv.write_bytes(b"z" * 16)
    v = _add_video(db, mkv, "x.mkv", 16)
    r = client.get(f"/api/videos/{v.id}/stream")
    assert r.status_code == 415
    assert r.json()["code"] == "FORMAT_UNSUPPORTED"

    mp4 = tmp_path / "gone.mp4"
    mp4.write_bytes(b"y" * 16)
    v2 = _add_video(db, mp4, "gone.mp4", 16)
    mp4.unlink()
    assert client.get(f"/api/videos/{v2.id}/stream").status_code == 404


# ---------------- R1(P4)：播放三档（ADR-015） ----------------

def _synth(path, args):
    cmd = [str(locate_ffmpeg()), "-hide_banner", "-loglevel", "error", "-y",
           "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10:duration=1"]
    cmd += args + [str(path)]
    subprocess.run(cmd, check=True, capture_output=True)


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg not available")
def test_stream_remux_h264_aac_mkv(client, db, tmp_path):
    """h264+aac mkv：编码兼容容器不兼容 → remux 档，fMP4 输出 + 模式头。"""
    mkv = tmp_path / "h264.mkv"
    _synth(mkv, ["-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                 "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac"])
    v = _add_video(db, mkv, "h264.mkv", mkv.stat().st_size)
    r = client.get(f"/api/videos/{v.id}/stream")
    assert r.status_code == 200
    assert r.headers["x-sophos-stream-mode"] == "remux"
    assert r.headers["content-type"].startswith("video/mp4")
    assert b"ftyp" in r.content[:32]  # fMP4 魔数


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg not available")
def test_stream_transcode_mpeg4_mkv(client, db, tmp_path):
    """mpeg4 mkv：编码不兼容 → transcode 档（低分辨率控 CPU）。"""
    mkv = tmp_path / "mpeg4.mkv"
    _synth(mkv, ["-c:v", "mpeg4", "-q:v", "20"])
    v = _add_video(db, mkv, "mpeg4.mkv", mkv.stat().st_size)
    r = client.get(f"/api/videos/{v.id}/stream")
    assert r.status_code == 200
    assert r.headers["x-sophos-stream-mode"] == "transcode"
    assert b"ftyp" in r.content[:32]
    # 探测结果应回写 DB（播放期再试口径）
    db.expire_all()
    assert db.get(Video, v.id).vcodec == "mpeg4"


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg not available")
def test_stream_transcode_disabled_falls_back_415(client, db, tmp_path, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "transcode_enabled", False)
    mkv = tmp_path / "h264.mkv"
    _synth(mkv, ["-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                 "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac"])
    v = _add_video(db, mkv, "h264.mkv", mkv.stat().st_size)
    r = client.get(f"/api/videos/{v.id}/stream")
    assert r.status_code == 415
    assert r.json()["code"] == "FORMAT_UNSUPPORTED"
    # 白名单容器不受开关影响（M2 行为保留）
    mp4 = tmp_path / "ok.mp4"
    _synth(mp4, ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"])
    v2 = _add_video(db, mp4, "ok.mp4", mp4.stat().st_size)
    r2 = client.get(f"/api/videos/{v2.id}/stream")
    assert r2.status_code == 200
    assert r2.headers["x-sophos-stream-mode"] == "direct"


def test_stream_mode_field_in_list(client, db, tmp_path):
    """列表项含 stream_mode 与 dir_path（前端库下拉/播放提示用）。"""
    mkv = tmp_path / "x.mkv"
    mkv.write_bytes(b"z" * 16)
    v = _add_video(db, mkv, "x.mkv", 16)
    items = client.get("/api/videos").json()["items"]
    row = next(i for i in items if i["id"] == v.id)
    assert row["stream_mode"] == "unsupported"  # 假文件：探测失败且容器不在白名单
    assert row["dir_path"] == str(mkv.parent)


# ---------------- R4(P3)：stream_url 契约 + 错误分型 + 路由优先级（ADR-020） ----------------

def test_stream_url_in_list_and_detail(client, db, video_file):
    """列表/详情下发 stream_url（后端是 stream endpoint 唯一真源，PLAN §22）。"""
    f, content = video_file
    v = _add_video(db, f, "movie.mp4", len(content))
    detail = client.get(f"/api/videos/{v.id}").json()
    assert detail["stream_url"] == f"/api/videos/{v.id}/stream"
    items = client.get("/api/videos").json()["items"]
    assert all(i["stream_url"] == f"/api/videos/{i['id']}/stream" for i in items)


def test_stream_404_typed_codes(client, db, tmp_path):
    """404 分型：库中无 id → VIDEO_NOT_FOUND；源文件丢失 → SOURCE_NOT_FOUND。"""
    r = client.get("/api/videos/9999/stream")
    assert r.status_code == 404
    assert r.json()["code"] == "VIDEO_NOT_FOUND"

    mp4 = tmp_path / "gone.mp4"
    mp4.write_bytes(b"y" * 16)
    v = _add_video(db, mp4, "gone.mp4", 16)
    mp4.unlink()
    r = client.get(f"/api/videos/{v.id}/stream")
    assert r.status_code == 404
    assert r.json()["code"] == "SOURCE_NOT_FOUND"


def test_stream_api_route_beats_spa_fallback(client, db):
    """/api/* 优先于 StaticFiles(html=True)：stream 404 必须是后端 JSON 分型，
    不能被 SPA fallback 当作不存在的静态资源返回 HTML（PLAN §24 回归）。"""
    r = client.get("/api/videos/9999/stream")
    assert r.headers["content-type"].startswith("application/json")
    assert r.json()["code"] == "VIDEO_NOT_FOUND"
    assert "text/html" not in r.headers["content-type"]


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg not available")
def test_stream_direct_mp4_h264_aac(client, db, tmp_path):
    """MP4 h264/aac → direct 档（真 Range 文件直出）。"""
    mp4 = tmp_path / "h264.mp4"
    _synth(mp4, ["-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                 "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                 "-c:a", "aac"])
    v = _add_video(db, mp4, "h264.mp4", mp4.stat().st_size)
    r = client.get(f"/api/videos/{v.id}/stream")
    assert r.status_code == 200
    assert r.headers["x-sophos-stream-mode"] == "direct"
    assert r.headers["content-type"].startswith("video/mp4")


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg not available")
def test_stream_remux_failure_is_stream_failed(client, db, tmp_path):
    """remux 起播即失败（损坏文件）→ 500 STREAM_FAILED（带 stderr），不再伪装。"""
    mkv = tmp_path / "bad.mkv"
    mkv.write_bytes(b"not a video at all" * 10)
    v = _add_video(db, mkv, "bad.mkv", mkv.stat().st_size)
    v.vcodec, v.acodec = "h264", "aac"  # 伪造探测结果 → decide_mode=remux
    db.commit()
    r = client.get(f"/api/videos/{v.id}/stream")
    assert r.status_code == 500
    body = r.json()
    assert body["code"] == "STREAM_FAILED"
    assert body["message"]  # stderr 尾部可读


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg not available")
def test_stream_transcode_failure_is_transcode_failed(client, db, tmp_path):
    """transcode 起播即失败 → 500 TRANSCODE_FAILED。"""
    mkv = tmp_path / "bad2.mkv"
    mkv.write_bytes(b"junk data" * 100)
    v = _add_video(db, mkv, "bad2.mkv", mkv.stat().st_size)
    v.vcodec, v.acodec = "hevc", "ac3"  # → transcode 档
    db.commit()
    r = client.get(f"/api/videos/{v.id}/stream")
    assert r.status_code == 500
    assert r.json()["code"] == "TRANSCODE_FAILED"


# ---------------- R5：服务端 seek（?ss=，转码/重封装管道流无 Range） ----------------

def test_build_ffmpeg_cmd_with_start_sec():
    """-ss 置于 -i 之前（输入 seek）；None/0 不注入。"""
    from app.services.streamer import build_ffmpeg_cmd

    cmd = build_ffmpeg_cmd("transcode", "x.mkv", start_sec=12.5)
    i_ss, i_input = cmd.index("-ss"), cmd.index("-i")
    assert cmd[i_ss + 1] == "12.500"
    assert i_ss < i_input
    assert "-ss" not in build_ffmpeg_cmd("transcode", "x.mkv")
    assert "-ss" not in build_ffmpeg_cmd("remux", "x.mkv", start_sec=0)
    assert "-ss" in build_ffmpeg_cmd("remux", "x.mkv", start_sec=3)


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg not available")
def test_stream_transcode_with_seek(client, db, tmp_path):
    """转码档 ?ss=：200 + fMP4，输出从目标位置起播（时间戳重排）。"""
    mkv = tmp_path / "seek.mkv"
    _synth(mkv, ["-c:v", "mpeg4", "-q:v", "20"])  # 1s mpeg4 → transcode 档
    v = _add_video(db, mkv, "seek.mkv", mkv.stat().st_size)
    r = client.get(f"/api/videos/{v.id}/stream", params={"ss": 0.5})
    assert r.status_code == 200
    assert r.headers["x-sophos-stream-mode"] == "transcode"
    assert b"ftyp" in r.content[:32]


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg not available")
def test_stream_remux_with_seek(client, db, tmp_path):
    """重封装档 ?ss=：copy 模式下输入 seek 正常产出 fMP4。"""
    mkv = tmp_path / "h264s.mkv"
    _synth(mkv, ["-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                 "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac"])
    v = _add_video(db, mkv, "h264s.mkv", mkv.stat().st_size)
    r = client.get(f"/api/videos/{v.id}/stream", params={"ss": 0.2})
    assert r.status_code == 200
    assert r.headers["x-sophos-stream-mode"] == "remux"
    assert b"ftyp" in r.content[:32]


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg not available")
def test_stream_remux_ts_adts_aac(client, db, tmp_path):
    """R7 回归（生产缺陷）：TS 容器 AAC 为 ADTS 裸流，remux 到 fMP4 必须加
    aac_adtstoasc——缺失时 ffmpeg 报 Malformed AAC 立即中止，整条流只剩
    fMP4 头（前端永远转圈）。修复后应产出含媒体数据的完整 fMP4。"""
    ts = tmp_path / "adts.ts"
    _synth(ts, ["-f", "lavfi", "-i", "sine=frequency=440:duration=2",
                "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-f", "mpegts"])
    v = _add_video(db, ts, "adts.ts", ts.stat().st_size)
    r = client.get(f"/api/videos/{v.id}/stream")
    assert r.status_code == 200
    assert r.headers["x-sophos-stream-mode"] == "remux"
    assert b"ftyp" in r.content[:32]
    # 完整 fMP4：长度远超裸头（死流实测 ~1.2KB）且以 mfra/mfro 收尾
    assert len(r.content) > 16 * 1024
    assert b"mfro" in r.content[-64:]


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg not available")
def test_stream_direct_ignores_seek_param(client, db, tmp_path):
    """direct 档有真 Range：?ss= 被忽略（FileResponse 语义不变）。"""
    mp4 = tmp_path / "ok2.mp4"
    _synth(mp4, ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"])
    v = _add_video(db, mp4, "ok2.mp4", mp4.stat().st_size)
    r = client.get(f"/api/videos/{v.id}/stream", params={"ss": 0.5})
    assert r.status_code == 200
    assert r.headers["x-sophos-stream-mode"] == "direct"


# ---------------- R6：?fallback=1 自动降级转码（ADR-027） ----------------

@pytest.mark.skipif(not FFMPEG, reason="ffmpeg not available")
def test_stream_fallback_forces_transcode(client, db, tmp_path):
    """direct 档 ?fallback=1 → 强制 transcode：浏览器解不了的 h264 变体
    （High10 等"探测兼容但解码失败"）由前端自动降级兜底。"""
    mp4 = tmp_path / "h264f.mp4"
    _synth(mp4, ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"])
    v = _add_video(db, mp4, "h264f.mp4", mp4.stat().st_size)
    r = client.get(f"/api/videos/{v.id}/stream", params={"fallback": 1})
    assert r.status_code == 200
    assert r.headers["x-sophos-stream-mode"] == "transcode"
    assert b"ftyp" in r.content[:32]


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg not available")
def test_stream_fallback_respects_transcode_disabled(client, db, tmp_path, monkeypatch):
    """转码总开关关闭时 fallback 不生效：direct 保持 direct。"""
    from app.config import settings

    monkeypatch.setattr(settings, "transcode_enabled", False)
    mp4 = tmp_path / "h264g.mp4"
    _synth(mp4, ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"])
    v = _add_video(db, mp4, "h264g.mp4", mp4.stat().st_size)
    r = client.get(f"/api/videos/{v.id}/stream", params={"fallback": 1})
    assert r.status_code == 200
    assert r.headers["x-sophos-stream-mode"] == "direct"


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg not available")
def test_stream_busy_503_when_slot_held(client, db, tmp_path, monkeypatch):
    """转码信号量被占且等待超时 → 503 STREAM_BUSY（而非无限挂起）。"""
    from app.config import settings
    from app.services import streamer

    monkeypatch.setattr(settings, "transcode_max_concurrency", 1)
    monkeypatch.setattr(streamer, "_transcode_sem", None)  # 重置单例以套用并发=1
    monkeypatch.setattr(streamer, "STREAM_SEM_WAIT_SEC", 0.5)  # 测试不等 15s
    sem = streamer._concurrency_semaphore()
    assert sem.acquire(blocking=False)  # 模拟另一路转码占住通道
    try:
        mkv = tmp_path / "busy.mkv"
        _synth(mkv, ["-c:v", "mpeg4", "-q:v", "20"])
        v = _add_video(db, mkv, "busy.mkv", mkv.stat().st_size)
        r = client.get(f"/api/videos/{v.id}/stream", params={"fallback": 1})
        assert r.status_code == 503
        assert r.json()["code"] == "STREAM_BUSY"
    finally:
        sem.release()
        streamer._transcode_sem = None


# ---------------- R6：断连后并发槽位必须归还（ADR-027 受控复现的生产缺陷） ----------------

def test_stream_slot_idempotent_release():
    """_StreamSlot：生成器 finally 与看门狗兜底双路释放只放行一次。"""
    import threading

    from app.services.streamer import _StreamSlot

    sem = threading.Semaphore(1)
    assert sem.acquire(blocking=False)  # 占用槽位（value 1→0）
    slot = _StreamSlot(sem)
    slot.release()
    slot.release()
    assert sem._value == 1  # 双重 release 只归一次槽位（不超发）


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg not available")
def test_stream_generator_close_releases_slot(tmp_path):
    """生成器未消费完即 close（断连后 Starlette 不再推进的路径）→ finally 释放槽位。"""
    import threading

    from app.services import streamer

    clip = tmp_path / "slot_test.mp4"
    _synth(clip, ["-c:v", "mpeg4", "-q:v", "20"])
    cmd = streamer.build_ffmpeg_cmd("transcode", str(clip))
    sem = threading.Semaphore(1)
    streamer._transcode_sem = sem
    try:
        gen = streamer.iter_ffmpeg_pipe(cmd, "transcode", {})
        assert next(gen)  # 起播预检：占用槽位
        gen.close()       # 断连后生成器被关闭的路径
        assert sem._value == 1
    finally:
        streamer._transcode_sem = None


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg not available")
def test_stream_client_abort_releases_slot_for_next_stream(client, db, tmp_path):
    """端到端回归（受控复现的生产缺陷）：客户端中途断流后，断连看门狗
    kill ffmpeg 并兜底释放槽位——下一条流必须 200 而非 503 STREAM_BUSY。

    R7 起仅 transcode 占用并发槽（remux 为 -c copy 不限流），故用例改用
    mpeg4 源走转码档以保持回归语义。

    TestClient 无法模拟中途断连，这里在进程内起真实 uvicorn + httpx 早停。
    """
    import threading
    import time
    import socket

    import httpx
    import uvicorn

    from app.config import settings
    from app.services import streamer

    mkv = tmp_path / "long.mkv"
    _synth(mkv, ["-f", "lavfi", "-i", "testsrc=size=160x120:rate=10:duration=30",
                 "-c:v", "mpeg4", "-q:v", "20"])
    v = _add_video(db, mkv, "long.mkv", mkv.stat().st_size)

    sem = threading.Semaphore(1)
    streamer._transcode_sem = sem
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    config = uvicorn.Config(client.app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    try:
        thread.start()
        base = f"http://127.0.0.1:{port}"
        for _ in range(50):
            try:
                httpx.get(f"{base}/api/health", timeout=1)
                break
            except Exception:  # noqa: BLE001
                time.sleep(0.2)
        with httpx.Client() as http:
            with http.stream("GET", f"{base}/api/videos/{v.id}/stream",
                             timeout=30) as resp:
                n = 0
                for chunk in resp.iter_bytes():
                    n += len(chunk)
                    if n > 100_000:
                        break
                assert n > 0
            # with 退出 = 客户端断连；给看门狗（0.35s 轮询）时间回收
            deadline = time.time() + 10
            while time.time() < deadline and sem._value == 0:
                time.sleep(0.2)
            r2 = http.get(f"{base}/api/videos/{v.id}/stream", timeout=30)
            assert r2.status_code == 200, f"slot leaked: {r2.status_code} {r2.text[:120]}"
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        streamer._transcode_sem = None
