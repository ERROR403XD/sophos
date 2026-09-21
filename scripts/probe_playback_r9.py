"""R9 播放链路端到端验证（只验容器/编码/传输，不涉及视频内容识别）。

用法：
  python scripts/probe_playback_r9.py [样本目录]     # 默认 <sample-video-dir>\\fail_sample

流程：临时 DB + 临时 data_dir → 扫描样本目录（只记路径与容器/编码元数据）→
走 HTTP 播放：渐进 stream（remux）与 HLS 会话 → 校验输出为可解析的 fMP4 /
TS 切片；对高分辨率样本验证转码分辨率封顶。不做任何面容处理，不落缩略图。
"""
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

SAMPLE_DIR = Path(sys.argv[1] if len(sys.argv) > 1 else r"Z:\<sample-video-dir>\fail_sample")
FFPROBE = Path(r"<project-root>\tools\ffmpeg-9.0.1-essentials_build\bin\ffprobe.exe")


def main():
    tmp = Path(tempfile.mkdtemp(prefix="sophos_r9_"))
    from app.config import settings
    settings.data_dir = tmp / "data"
    settings.db_path = tmp / "data" / "probe.db"
    settings.work_dirs = str(SAMPLE_DIR)
    settings.models_dir = Path(r"<project-root>\data\models")

    from app.db.session import init_engine
    init_engine(settings)

    import uvicorn
    from app.main import app

    with __import__("socket").socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
                                           log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()

    import httpx
    base = f"http://127.0.0.1:{port}"
    for _ in range(60):
        try:
            httpx.get(f"{base}/api/health", timeout=1)
            break
        except Exception:
            time.sleep(0.3)

    with httpx.Client(timeout=60) as http:
        # 1) 扫描（只记路径 + 嗅探容器 + ffprobe 编码元数据）
        r = http.post(f"{base}/api/scan/start")
        assert r.status_code in (200, 202), r.text
        for _ in range(120):
            jobs = http.get(f"{base}/api/jobs").json()["items"]
            if jobs and jobs[0]["status"] in ("done", "failed", "cancelled"):
                break
            time.sleep(0.5)
        print("scan job:", jobs[0]["status"], jobs[0].get("result"))

        items = http.get(f"{base}/api/videos", params={"page_size": 50}).json()["items"]
        print(f"\n== 列表（{len(items)} 条）==")
        for it in items:
            print(f"  #{it['id']} {it['filename']:<12} container={it['container']:<8} "
                  f"mode={it['stream_mode']}")

        # 2) 渐进 stream：remux 档 → 读前 3MB 校验 fMP4 头
        vid = items[0]["id"]
        with http.stream("GET", f"{base}/api/videos/{vid}/stream") as resp:
            assert resp.status_code == 200, resp.status_code
            mode = resp.headers["x-sophos-stream-mode"]
            head = b""
            for chunk in resp.iter_bytes():
                head += chunk
                if len(head) > 3 * 1024 * 1024:
                    break
        print(f"\n== 渐进 stream（#{vid}）== mode={mode} 读到 {len(head)} 字节")
        assert mode == "remux", mode
        assert b"ftyp" in head[:64], "缺 fMP4 头"
        assert len(head) > 1024 * 1024, "流数据过少（疑似死流）"
        out = tmp / "progressive.mp4"
        out.write_bytes(head)
        pr = subprocess.run([str(FFPROBE), "-v", "error", "-show_entries",
                             "stream=codec_name,width,height", "-of", "csv=p=0", str(out)],
                            capture_output=True, text=True)
        print("  ffprobe 输出片段:", pr.stdout.strip().replace("\n", " | ") or pr.stderr.strip())

        # 3) HLS 会话：playlist + 切片
        tok = "probetok"
        r = http.get(f"{base}/api/videos/{vid}/hls/{tok}/index.m3u8")
        print(f"\n== HLS playlist（#{vid}）== {r.status_code} "
              f"mode={r.headers.get('x-sophos-stream-mode')}")
        assert r.status_code == 200, r.text
        assert "#EXTM3U" in r.text
        import re
        seg = re.search(r"seg_\d+\.ts", r.text)
        assert seg, r.text[:300]
        rs = http.get(f"{base}/api/videos/{vid}/hls/{tok}/{seg.group(0)}")
        print(f"  切片 {seg.group(0)}: {rs.status_code} {len(rs.content)} 字节 "
              f"sync=0x{rs.content[0]:02X}")
        assert rs.status_code == 200 and rs.content[0] == 0x47
        rr = http.get(f"{base}/api/videos/{vid}/hls/{tok}/{seg.group(0)}",
                      headers={"Range": "bytes=0-1"})
        print(f"  切片 Range 探测: {rr.status_code} {rr.headers.get('content-range')}")
        assert rr.status_code == 206  # iOS Safari 起播硬要求

        # 4) 显式停止会话（快速退出路径）
        rd = http.delete(f"{base}/api/videos/{vid}/hls/{tok}")
        print(f"\n== DELETE 会话 == {rd.status_code}")
        assert rd.status_code == 204

        # 5) 最大体积样本（通常是 4K 源）：转码档 + 分辨率封顶
        uhd = max(items, key=lambda i: i["size_bytes"] or 0, default=None)
        if uhd:
            with http.stream("GET", f"{base}/api/videos/{uhd['id']}/stream",
                             params={"fallback": 1}) as resp:
                assert resp.status_code == 200
                m = resp.headers["x-sophos-stream-mode"]
                buf = b""
                for chunk in resp.iter_bytes():
                    buf += chunk
                    if len(buf) > 6 * 1024 * 1024:
                        break
            out2 = tmp / "uhd_capped.mp4"
            out2.write_bytes(buf)
            pr2 = subprocess.run([str(FFPROBE), "-v", "error", "-select_streams", "v:0",
                                  "-show_entries", "stream=codec_name,profile,width,height",
                                  "-of", "csv=p=0", str(out2)],
                                 capture_output=True, text=True)
            print(f"\n== 大样本转码（#{uhd['id']} {uhd['filename']}）=="
                  f" mode={m} 输出={pr2.stdout.strip()}")
            assert m == "transcode"
            h = int(pr2.stdout.strip().split(",")[-1])
            assert h <= 1080, f"分辨率未封顶: {pr2.stdout.strip()}"

    server.should_exit = True
    time.sleep(0.5)
    print("\n全部通过 ✅  临时目录:", tmp)


if __name__ == "__main__":
    main()
