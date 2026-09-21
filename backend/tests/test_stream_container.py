"""R9（ADR-030）：容器嗅探与播放分档回归。

用户报障形态：一批扩展名为 .mp4 的文件实为 **MPEG-TS**（下载工具按来源站点
后缀改名）。原 decide_mode 只看扩展名 → 判 direct → FileResponse 把 TS 字节流
当 mp4 直出 → 浏览器 MediaError → 前端降级转码（1080p/4K h264 → libx264）
把 CPU 打满 → 除播放失败外还表现为"页面不定期失去响应"。

本文件锁定：
- sniff_container 的魔术字节判定（含 spoofed 扩展名）；
- decide_mode 优先采信实测容器（容器未知才回退扩展名，旧行为）；
- 真实 TS 冒名 .mp4 走 stream 端点必须 remux（而非 direct）；
- 转码档分辨率封顶（4K 源 → 1080p 输出，治"单路打满 CPU"）。
"""
import re
import subprocess

import pytest

from app.db.models import Video
from app.services import streamer
from app.services.frame_sampler import locate_ffmpeg

FFMPEG = locate_ffmpeg() is not None


def _add_video(db, path, filename, size, vcodec=None, acodec=None, container=None):
    v = Video(path=str(path), filename=filename, dir_path=str(path.parent),
              size_bytes=size, mtime=path.stat().st_mtime, status="done",
              vcodec=vcodec, acodec=acodec, container=container)
    db.add(v)
    db.commit()
    db.refresh(v)
    return v


def _synth(path, args, size="160x120", rate=10, duration=2):
    cmd = [str(locate_ffmpeg()), "-hide_banner", "-loglevel", "error", "-y",
           "-f", "lavfi", "-i", f"testsrc=size={size}:rate={rate}:duration={duration}"]
    cmd += args + [str(path)]
    subprocess.run(cmd, check=True, capture_output=True)


# ---------------- sniff_container：魔术字节判定 ----------------

def test_sniff_container_magic_bytes(tmp_path):
    """各家族魔术字节识别；识别不出返回 None（调用方回退扩展名）。"""
    cases = {
        "a.mp4": b"\x00\x00\x00\x20ftypisom" + b"\x00" * 32,        # ISO BMFF
        "b.webm": b"\x1a\x45\xdf\xa3" + b"\x00" * 20 + b"webm" + b"\x00" * 32,
        "c.mkv": b"\x1a\x45\xdf\xa3" + b"\x00" * 20 + b"matroska" + b"\x00" * 20,
        "d.avi": b"RIFF\x00\x00\x00\x00AVI " + b"\x00" * 16,
        "e.flv": b"FLV\x01\x05" + b"\x00" * 20,
        "f.ogg": b"OggS" + b"\x00" * 20,
        "g.mpg": b"\x00\x00\x01\xba" + b"\x00" * 20,
        "h.wmv": b"0&\xb2u" + b"\x00" * 20,
        "i.unknown": b"\x01\x02\x03\x04" + b"\x00" * 20,
    }
    expect = {"a.mp4": "mp4", "b.webm": "webm", "c.mkv": "matroska", "d.avi": "avi",
              "e.flv": "flv", "f.ogg": "ogg", "g.mpg": "mpeg", "h.wmv": "asf",
              "i.unknown": None}
    for name, blob in cases.items():
        p = tmp_path / name
        p.write_bytes(blob + b"\x00" * 600)
        assert streamer.sniff_container(p) == expect[name], name


def test_sniff_container_mpegts_spoofed(tmp_path):
    """0x47 同步字节 @0/188/376 → mpegts（**不看扩展名**：.mp4 也认）。"""
    ts = tmp_path / "spoofed.mp4"
    pkt = bytes([0x47]) + b"\x00" * 187
    ts.write_bytes(pkt * 4)
    assert streamer.sniff_container(ts) == "mpegts"

    # 192 字节包（M2TS）形态：0x47 落在 4/196/388
    m2ts = tmp_path / "m2ts.m2ts"
    m2ts.write_bytes((b"\x00\x00\x00\x00" + bytes([0x47]) + b"\x00" * 187) * 3)
    assert streamer.sniff_container(m2ts) == "mpegts"


def test_sniff_container_short_or_missing(tmp_path):
    """过短/不存在的文件不抛异常（返回 None 或按已有字节判定）。"""
    ftyp = tmp_path / "short.mp4"
    ftyp.write_bytes(b"\x00\x00\x00\x20ftyp")  # 8 字节：足够认出 ISO BMFF
    assert streamer.sniff_container(ftyp) == "mp4"

    tiny = tmp_path / "tiny.mp4"
    tiny.write_bytes(b"ab")
    assert streamer.sniff_container(tiny) is None

    empty = tmp_path / "empty.mp4"
    empty.write_bytes(b"")
    assert streamer.sniff_container(empty) is None

    assert streamer.sniff_container(tmp_path / "nope.mp4") is None


# ---------------- decide_mode：实测容器优先 ----------------

def test_decide_mode_prefers_sniffed_container():
    """实测容器覆盖扩展名：TS 冒名 .mp4 不再误判 direct。"""
    # 报障形态：.mp4 + h264/aac，实测 mpegts → remux（copy 即可，CPU≈0）
    assert streamer.decide_mode("x.mp4", "h264", "aac", True,
                                container="mpegts") == "remux"
    # 真 mp4 → direct（回归：正常 mp4 不受影响）
    assert streamer.decide_mode("x.mp4", "h264", "aac", True,
                                container="mp4") == "direct"
    # mkv 换名 .mp4 → remux
    assert streamer.decide_mode("x.mp4", "h264", "aac", True,
                                container="matroska") == "remux"
    # 实测容器不可直出 + 探测失败（vcodec 空）→ 不赌直出：unsupported
    assert streamer.decide_mode("x.mp4", None, None, True,
                                container="mpegts") == "unsupported"
    # 容器未知（老库未回填）→ 回退扩展名白名单（旧行为）
    assert streamer.decide_mode("x.mp4", "h264", "aac", True,
                                container=None) == "direct"
    # 实测 webm + vp9 → direct
    assert streamer.decide_mode("x.webm", "vp9", "opus", True,
                                container="webm") == "direct"


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg not available")
def test_stream_spoofed_ts_named_mp4_goes_remux(client, db, tmp_path):
    """端到端（用户报障形态）：真实 TS 内容 + .mp4 扩展名 → stream 必须 remux
    档并产出可解码的 fMP4，而不是把 TS 字节流当 mp4 直出。"""
    spoof = tmp_path / "spoofed.mp4"
    _synth(spoof, ["-f", "lavfi", "-i", "sine=frequency=440:duration=2",
                   "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                   "-c:a", "aac", "-f", "mpegts"])
    assert streamer.sniff_container(spoof) == "mpegts"  # 内容确实是 TS
    v = _add_video(db, spoof, "spoofed.mp4", spoof.stat().st_size,
                   "h264", "aac", streamer.sniff_container(spoof))

    r = client.get(f"/api/videos/{v.id}/stream")
    assert r.status_code == 200
    assert r.headers["x-sophos-stream-mode"] == "remux"
    assert b"ftyp" in r.content[:32]

    # 播放期惰性补探测：container 留空的历史行也会被判回 remux
    db.expire_all()
    legacy = tmp_path / "legacy.mp4"
    legacy.write_bytes(spoof.read_bytes())
    v2 = _add_video(db, legacy, "legacy.mp4", legacy.stat().st_size, "h264", "aac", None)
    r2 = client.get(f"/api/videos/{v2.id}/stream")
    assert r2.headers["x-sophos-stream-mode"] == "remux"
    db.expire_all()
    assert db.get(Video, v2.id).container == "mpegts"


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg not available")
def test_list_backfills_container(client, db, tmp_path):
    """列表惰性回填：返回的 stream_mode 与实测容器一致（不再显示错误档位）。"""
    spoof = tmp_path / "spoof_list.mp4"
    pkt = bytes([0x47]) + b"\x00" * 187
    spoof.write_bytes(pkt * 4)
    v = _add_video(db, spoof, "spoof_list.mp4", spoof.stat().st_size,
                   "h264", "aac", None)

    items = client.get("/api/videos").json()["items"]
    row = next(i for i in items if i["id"] == v.id)
    assert row["container"] == "mpegts"
    assert row["stream_mode"] == "remux"  # 修复前：direct（白失败一轮）
    db.expire_all()
    assert db.get(Video, v.id).container == "mpegts"


# ---------------- 转码分辨率封顶（治 4K 单路打满 CPU） ----------------

def test_build_ffmpeg_cmd_scale_cap():
    """转码档注入 scale 封顶；remux 不受影响；max_height=0 关闭。"""
    cmd = streamer.build_ffmpeg_cmd("transcode", "x.mkv", max_height=1080)
    assert "-vf" in cmd
    vf = cmd[cmd.index("-vf") + 1]
    assert "min(ih,1080)" in vf and "min(iw,1920)" in vf
    assert "force_original_aspect_ratio=decrease" in vf

    assert "-vf" not in streamer.build_ffmpeg_cmd("transcode", "x.mkv", max_height=0)
    assert "-vf" not in streamer.build_ffmpeg_cmd("remux", "x.mkv", max_height=1080)


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg not available")
def test_transcode_caps_4k_output(client, db, tmp_path):
    """4K 源转码 → 1080p 封顶输出（修复前原样 4K 转码，单路打满 CPU）。"""
    from app.services.frame_sampler import locate_ffprobe
    from app.config import settings

    src = tmp_path / "uhd.mkv"
    _synth(src, ["-c:v", "mpeg4", "-q:v", "20"], size="640x360", rate=5, duration=1)
    # 用 640x360 的 mpeg4 源代表"高分辨率+需转码"（真 4K 合成太慢）；把封顶
    # 降到 200 高来验证 scale 生效，等价于生产里 1080 封顶对 4K 源的作用。
    v = _add_video(db, src, "uhd.mkv", src.stat().st_size, "mpeg4", None, "matroska")
    old = settings.transcode_max_height
    settings.transcode_max_height = 200
    try:
        r = client.get(f"/api/videos/{v.id}/stream")
        assert r.status_code == 200
        assert r.headers["x-sophos-stream-mode"] == "transcode"
        out = tmp_path / "out.mp4"
        out.write_bytes(r.content)
        probe = subprocess.run(
            [str(locate_ffprobe()), "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "csv=p=0", str(out)],
            capture_output=True, text=True)
        assert probe.stdout.strip() == "356,200"  # 16:9 保持 + force_divisible_by=2
    finally:
        settings.transcode_max_height = old
