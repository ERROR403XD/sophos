"""R8（ADR-029）：HLS 会话端点测试——移动端播放修复。

核心回归点：iOS Safari 起播前发 `Range: bytes=0-1` 探测，服务器必须回 206。
fMP4 管道流（/stream）做不到（200 + chunked），HLS 切片经 FileResponse 下发
必须做到——test_segment_supports_range 锁定该语义。
"""
import re
import subprocess
import time

import pytest

from app.db.models import Video
from app.services import hls
from app.services.frame_sampler import locate_ffmpeg

FFMPEG = locate_ffmpeg() is not None


def _add_video(db, path, filename, size, vcodec=None, acodec=None):
    v = Video(path=str(path), filename=filename, dir_path=str(path.parent),
              size_bytes=size, mtime=path.stat().st_mtime, status="done",
              vcodec=vcodec, acodec=acodec)
    db.add(v)
    db.commit()
    db.refresh(v)
    return v


def _synth(path, args):
    cmd = [str(locate_ffmpeg()), "-hide_banner", "-loglevel", "error", "-y",
           "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10:duration=3"]
    cmd += args + [str(path)]
    subprocess.run(cmd, check=True, capture_output=True)


@pytest.fixture()
def ts_video(client, db, tmp_path):
    """h264+aac 的 TS 源（用户报障的典型形态）→ HLS remux 会话。"""
    ts = tmp_path / "clip.ts"
    _synth(ts, ["-f", "lavfi", "-i", "sine=frequency=440:duration=3",
                "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-f", "mpegts"])
    v = _add_video(db, ts, "clip.ts", ts.stat().st_size, "h264", "aac")
    return v


def test_hls_url_in_list_and_detail(client, db, tmp_path):
    """列表/详情下发 hls_url（R8 契约，与 stream_url 同源）。"""
    f = tmp_path / "movie.mp4"
    f.write_bytes(b"x" * 16)
    v = _add_video(db, f, "movie.mp4", 16)
    detail = client.get(f"/api/videos/{v.id}").json()
    assert detail["hls_url"] == f"/api/videos/{v.id}/hls"
    items = client.get("/api/videos").json()["items"]
    assert all(i["hls_url"] == f"/api/videos/{i['id']}/hls" for i in items)


def test_hls_rejects_bad_token(client, db, tmp_path):
    f = tmp_path / "movie.mp4"
    f.write_bytes(b"x" * 16)
    v = _add_video(db, f, "movie.mp4", 16)
    assert client.get(f"/api/videos/{v.id}/hls/..%2Fevil/index.m3u8").status_code in (400, 404)
    r = client.get(f"/api/videos/{v.id}/hls/bad token/index.m3u8")
    assert r.status_code == 400
    assert r.json()["code"] == "BAD_TOKEN"
    r2 = client.get(f"/api/videos/{v.id}/hls/abc/seg_0000X.m4s")  # 非法切片名 → 400
    assert r2.status_code == 400
    r3 = client.get(f"/api/videos/{v.id}/hls/abc/seg_00000.ts")  # 形状合法但无会话 → 404
    assert r3.status_code == 404


def test_hls_404_typed(client, db, tmp_path):
    r = client.get("/api/videos/9999/hls/abc123/index.m3u8")
    assert r.status_code == 404
    assert r.json()["code"] == "VIDEO_NOT_FOUND"

    ts = tmp_path / "gone.ts"
    ts.write_bytes(b"y" * 16)
    v = _add_video(db, ts, "gone.ts", 16, "h264", "aac")
    ts.unlink()
    r2 = client.get(f"/api/videos/{v.id}/hls/abc123/index.m3u8")
    assert r2.status_code == 404
    assert r2.json()["code"] == "SOURCE_NOT_FOUND"


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg not available")
def test_hls_playlist_remux_and_segments(client, db, ts_video):
    """TS/H264/AAC → HLS remux 会话：TS 切片齐备，模式头正确。"""
    v = ts_video
    url = f"/api/videos/{v.id}/hls/tok1/index.m3u8"
    r = client.get(url)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/vnd.apple.mpegurl")
    assert r.headers["x-sophos-stream-mode"] == "remux"
    assert r.headers["cache-control"] == "no-store"
    body = r.text
    assert body.startswith("#EXTM3U")
    assert "#EXT-X-ENDLIST" in body

    seg = re.search(r"seg_(\d+)\.ts", body)
    assert seg, "playlist 应引用 seg_*.ts 切片"

    seg_r = client.get(f"/api/videos/{v.id}/hls/tok1/{seg.group(0)}")
    assert seg_r.status_code == 200
    assert seg_r.headers["content-type"].startswith("video/mp2t")
    assert seg_r.content[0] == 0x47  # MPEG-TS 同步字节


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg not available")
def test_segment_supports_range(client, db, ts_video):
    """核心回归（ADR-029 根因）：切片必须支持 HTTP Range（206 + Content-Range），
    与 /stream 的 200+chunked 形成对照——iOS Safari 靠 206 才肯起播。"""
    v = ts_video
    assert client.get(f"/api/videos/{v.id}/hls/tok2/index.m3u8").status_code == 200
    seg = re.search(r"seg_\d+\.ts",
                    client.get(f"/api/videos/{v.id}/hls/tok2/index.m3u8").text)
    assert seg
    r = client.get(f"/api/videos/{v.id}/hls/tok2/{seg.group(0)}",
                   headers={"Range": "bytes=0-1"})
    assert r.status_code == 206
    assert r.headers["content-range"].startswith("bytes 0-1/")
    assert len(r.content) == 2


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg not available")
def test_hls_seek_and_fallback_params(client, db, ts_video):
    """?ss= 起播定位与 ?fallback=1 强制转码在 HLS 端点同样生效。"""
    v = ts_video
    r = client.get(f"/api/videos/{v.id}/hls/tok3/index.m3u8", params={"ss": 1.0})
    assert r.status_code == 200
    assert r.headers["x-sophos-stream-mode"] == "remux"

    # h264 源 fallback=1 → HLS 转码档
    r2 = client.get(f"/api/videos/{v.id}/hls/tok4/index.m3u8",
                    params={"fallback": 1})
    assert r2.status_code == 200
    assert r2.headers["x-sophos-stream-mode"] == "transcode"


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg not available")
def test_hls_session_reuse_and_supersede(client, db, ts_video):
    """同 token 幂等续用同一会话；新 token 挤掉同视频旧会话（登记口径）。"""
    v = ts_video
    assert client.get(f"/api/videos/{v.id}/hls/toka/index.m3u8").status_code == 200
    assert "7:toka" in hls._sessions or f"{v.id}:toka" in hls._sessions
    sess_before = hls.get_session(v.id, "toka")

    assert client.get(f"/api/videos/{v.id}/hls/toka/index.m3u8").status_code == 200
    assert hls.get_session(v.id, "toka") is sess_before  # 同 token 复用

    assert client.get(f"/api/videos/{v.id}/hls/tokb/index.m3u8").status_code == 200
    assert hls.get_session(v.id, "toka") is None          # 旧会话被挤掉
    assert hls.get_session(v.id, "tokb") is not None
    # 旧会话目录切片仍在 TTL 窗口内可续读（暂停回放语义）
    r = client.get(f"/api/videos/{v.id}/hls/toka/index.m3u8")
    assert r.status_code == 200 and r.text.startswith("#EXTM3U")

    hls.shutdown_all()


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg not available")
def test_hls_transcode_disabled_415(client, db, ts_video, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "transcode_enabled", False)
    v = ts_video
    r = client.get(f"/api/videos/{v.id}/hls/tokx/index.m3u8")
    assert r.status_code == 415
    assert r.json()["code"] == "FORMAT_UNSUPPORTED"


def test_janitor_kills_idle_session(client, db, ts_video):
    """心跳超时的会话被看门狗回收：杀 ffmpeg、移出登记、目录留给 TTL 清理。"""
    v = ts_video
    sess = hls.get_session(v.id, "tokz")
    assert sess is None
    r = client.get(f"/api/videos/{v.id}/hls/tokz/index.m3u8")
    assert r.status_code == 200
    sess = hls.get_session(v.id, "tokz")
    assert sess is not None

    sess.last_poll -= hls.POLL_KILL_SEC + 1
    hls._janitor_pass()
    assert hls.get_session(v.id, "tokz") is None
    # 目录保留（TTL 内），存量 playlist 仍可续读
    assert sess.playlist_path.is_file()
    hls.shutdown_all()


def test_cleanup_stale_dirs(client, db, tmp_path, monkeypatch):
    """启动清理：删除上次进程遗留的会话目录。"""
    from app.config import settings

    base = settings.data_dir / "tmp" / "hls"
    stale = base / "999.stale"
    stale.mkdir(parents=True, exist_ok=True)
    (stale / "seg_00000.m4s").write_bytes(b"junk")
    hls.cleanup_stale_dirs()
    assert not stale.exists()
    assert base.is_dir()


def test_append_endlist_on_dead_session(client, db, tmp_path):
    """ffmpeg 异常退出且无 ENDLIST → 看门狗补写 ENDLIST（播放端不再无限等待）。"""
    from app.config import settings

    d = hls.session_dir(settings, 1, "deadbeef")
    d.mkdir(parents=True, exist_ok=True)
    (d / "index.m3u8").write_text("#EXTM3U\n#EXT-X-TARGETDURATION:4\n", encoding="utf-8")
    sess = hls.HlsSession(video_id=1, token="deadbeef", mode="remux", start_sec=0,
                          src="x", dir=d)
    hls._append_endlist(sess)
    assert "#EXT-X-ENDLIST" in (d / "index.m3u8").read_text(encoding="utf-8")
    hls._append_endlist(sess)  # 幂等
    import shutil as _sh
    _sh.rmtree(d, ignore_errors=True)


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg not available")
def test_wait_playlist_reports_ffmpeg_failure(client, db, tmp_path):
    """起播预检：源不可解 → wait_playlist 返回 stderr 尾部而非挂起。"""
    from app.config import settings

    bad = tmp_path / "bad.mp4"
    bad.write_bytes(b"not a video" * 32)
    sess = hls.get_or_start(1, "badtok", str(bad), "remux", 0.0)
    try:
        ok, err = hls.wait_playlist(sess, timeout=15)
        assert not ok
        assert "exited" in err
        hls._janitor_pass()  # 异常退出会话清理出登记
        assert hls.get_session(1, "badtok") is None
    finally:
        hls.shutdown_all()


# ---------------- R9：会话快速回收 / 登记锁不阻塞（页面失去响应治理） ----------------

@pytest.mark.skipif(not FFMPEG, reason="ffmpeg not available")
def test_hls_stop_session_kills_immediately(client, db, tmp_path):
    """显式删除会话（前端关闭播放器/换视频）：立即移出登记并归还转码槽。

    修复前快速退出只能等 120s 心跳超时，转码 ffmpeg 继续占满 CPU、槽位也被
    继续持有——下一条视频起播要等 15s 甚至 503，用户观感即"退出/换片后页面卡住"。
    """
    import threading

    from app.services import streamer

    src = tmp_path / "stop.ts"
    _synth(src, ["-f", "lavfi", "-i", "sine=frequency=440:duration=3",
                 "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                 "-c:a", "aac", "-f", "mpegts"])
    v = _add_video(db, src, "stop.ts", src.stat().st_size, "h264", "aac")

    sem = threading.Semaphore(1)
    streamer._transcode_sem = sem
    try:
        sess = hls.get_or_start(v.id, "stopme", str(src), "transcode", 0.0)
        ok, _err = hls.wait_playlist(sess)
        assert ok
        assert sem._value == 0  # 转码槽被会话持有

        r = client.delete(f"/api/videos/{v.id}/hls/stopme")
        assert r.status_code == 204
        assert hls.get_session(v.id, "stopme") is None
        assert sess.proc.poll() is not None  # 进程已被 kill
        assert sem._value == 1               # 槽位立即归还，下一条流无需等待
        assert client.delete(f"/api/videos/{v.id}/hls/stopme").status_code == 204  # 幂等
    finally:
        hls.shutdown_all()
        streamer._transcode_sem = None

    bad = client.delete(f"/api/videos/{v.id}/hls/bad token")
    assert bad.status_code == 400
    assert bad.json()["code"] == "BAD_TOKEN"


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg not available")
def test_hls_slot_released_when_transcode_finishes(client, db, tmp_path):
    """转码进程自然结束 → 下一次 playlist 轮询即归还槽位（不等 120s 看门狗）。

    修复前"看完一条转码视频立刻点下一条"要等槽位释放（最长 120s），表现为
    重复播放时页面卡住/503。
    """
    import threading
    import time

    from app.services import streamer

    src = tmp_path / "done.ts"
    _synth(src, ["-f", "lavfi", "-i", "sine=frequency=440:duration=3",
                 "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                 "-c:a", "aac", "-f", "mpegts"])
    v = _add_video(db, src, "done.ts", src.stat().st_size, "h264", "aac")

    sem = threading.Semaphore(1)
    streamer._transcode_sem = sem
    try:
        sess = hls.get_or_start(v.id, "donetok", str(src), "transcode", 0.0)
        ok, _err = hls.wait_playlist(sess)
        assert ok
        assert sem._value == 0
        deadline = time.time() + 20
        while time.time() < deadline and sess.proc.poll() is None:
            time.sleep(0.2)
        assert sess.proc.poll() is not None, "短素材转码应在 20s 内结束"
        hls.touch(sess)  # 模拟播放端下一次 playlist 轮询
        assert sem._value == 1, "转码结束后槽位必须立即归还"
    finally:
        hls.shutdown_all()
        streamer._transcode_sem = None


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg not available")
def test_hls_superseded_session_returns_fast(client, db, ts_video):
    """会话已不属于当前 token（被换掉/顶替）：wait_playlist 必须立即返回，
    不能让请求线程空等到 20s 超时——否则连发请求会占满同步线程池（页面失去响应）。"""
    import time

    v = ts_video
    sess = hls.get_or_start(v.id, "oldtok", str(v.path), "remux", 0.0)
    # 模拟"登记已被顶替但进程尚未收尾"的窗口：直接摘除登记
    with hls._registry_lock:
        hls._sessions.pop(f"{v.id}:oldtok", None)

    t0 = time.monotonic()
    ok, err = hls.wait_playlist(sess, timeout=20)
    elapsed = time.monotonic() - t0
    assert not ok
    assert "superseded" in err
    assert elapsed < 5, f"被顶替会话应快速返回，实际 {elapsed:.1f}s"
    # 兜底收尾（正常路径由 _kill_locked 负责）
    if sess.proc is not None and sess.proc.poll() is None:
        sess.proc.kill()
    hls.shutdown_all()


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg not available")
def test_hls_transcode_wait_does_not_block_remux(client, db, tmp_path, monkeypatch):
    """转码槽被占时，remux 会话仍能立即建立（转码等待移到登记锁之外）。

    修复前 get_or_start 在 _registry_lock 内等最长 15s 信号量——期间**所有**
    视频/所有 token 的 HLS 请求（含秒开的 remux）全被串行阻塞，正是"反复
    播放/快速退出时页面不定期失去响应"的服务端侧机制。
    """
    import threading
    import time

    from app.services import streamer

    src = tmp_path / "clip.ts"
    _synth(src, ["-f", "lavfi", "-i", "sine=frequency=440:duration=3",
                 "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                 "-c:a", "aac", "-f", "mpegts"])
    v = _add_video(db, src, "clip.ts", src.stat().st_size, "h264", "aac")

    sem = threading.Semaphore(1)
    streamer._transcode_sem = sem
    assert sem.acquire(blocking=False)  # 模拟另一路转码长时间占住槽位
    try:
        # remux（不占转码槽）必须立即返回，而不是排在被占用的转码等待后面
        t0 = time.monotonic()
        sess = hls.get_or_start(v.id, "remuxtok", str(src), "remux", 0.0)
        elapsed = time.monotonic() - t0
        assert elapsed < 2, f"remux 起流被转码等待阻塞：{elapsed:.1f}s"
        ok, _err = hls.wait_playlist(sess)
        assert ok
    finally:
        sem.release()
        hls.shutdown_all()
        streamer._transcode_sem = None


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg not available")
def test_hls_transcode_scale_cap_applied(monkeypatch):
    """HLS 转码命令带分辨率封顶（4K 源实时转码单路打满 CPU → 整机失去响应）。"""
    from app.config import settings

    cmd = hls.build_hls_cmd("transcode", "x.mkv", max_height=1080)
    assert "-vf" in cmd
    vf = cmd[cmd.index("-vf") + 1]
    assert "min(ih,1080)" in vf and "force_original_aspect_ratio=decrease" in vf
    # remux 是 -c copy，绝不能带 -vf
    assert "-vf" not in hls.build_hls_cmd("remux", "x.mkv", max_height=1080)
