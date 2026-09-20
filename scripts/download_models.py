"""下载 ONNX 模型到 data/models/（M3；R2 补头部姿态模型）。

来源：insightface 官方模型包（github release，非商业研究许可）。
- buffalo_s（默认，~30MB）：det_500m.onnx（检测）+ w600k_mbf.onnx（MobileFaceNet 特征）
                           + genderage.onnx（性别/年龄）
- buffalo_l（--pkg buffalo_l，~280MB）：det_10g.onnx（更准的检测）+ w600k_r50.onnx + genderage.onnx
- headpose_resnet18.onnx（~43MB，R2）：6DRepNet 系头部姿态（GitHub yakhyo/head-pose-estimation，MIT），
  文件缺失时姿态自动回退 5 点启发式

**无法下载、需从开发机复制或本地生成的文件**（见 data/models/README.md）：
- beauty_scut.onnx：convert_beauty_model.py 导出（缺→颜值分跳过）
- clip_vitb32_face.onnx + clip_vitb32_prompts.npz：export_clip_gender.py 导出（缺→性别门控降级为仅 genderage，有警告）

用法（在项目根运行）：
    python scripts/download_models.py                 # 下载 buffalo_s + headpose
    python scripts/download_models.py --pkg buffalo_l # 下载 buffalo_l + headpose
    python scripts/download_models.py --list          # 仅列出已装模型

国内网络慢时：脚本尊重 HTTPS_PROXY 环境变量；也可手动下载 zip 后
用 --zip <path> 指定本地包解压。
"""
from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
URL_TMPL = "https://github.com/deepinsight/insightface/releases/download/v0.7/{pkg}.zip"
HEADPOSE_URL = ("https://github.com/yakhyo/head-pose-estimation/releases/download/"
                "weights/resnet18.onnx")
HEADPOSE_NAME = "headpose_resnet18.onnx"

KEEP = {
    "buffalo_s": {"det_500m.onnx", "w600k_mbf.onnx", "genderage.onnx"},
    "buffalo_l": {"det_10g.onnx", "w600k_r50.onnx", "genderage.onnx"},
}

# 可下载的直链模型（缺失即拉取；失败仅警告——引擎有降级路径）
EXTRA_DIRECT: dict[str, str] = {HEADPOSE_NAME: HEADPOSE_URL}


def download(url: str, dest: Path) -> None:
    print(f"downloading {url}")
    # 优先 curl（自带 CA 包；部分 Windows 环境 python 的 SSL 根证书不全）
    import subprocess
    rc = subprocess.run(["curl", "-L", "-sS", "-o", str(dest), url]).returncode
    if rc == 0 and dest.exists() and dest.stat().st_size > 1_000_000:
        print(f"  curl ok: {dest.stat().st_size / 1e6:.1f} MB")
        return
    print("  curl unavailable/failed, falling back to urllib")
    req = urllib.request.Request(url, headers={"User-Agent": "sophos/0.2"})
    with urllib.request.urlopen(req, timeout=60) as resp, open(dest, "wb") as f:
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            if total:
                print(f"\r  {done / 1e6:.1f} / {total / 1e6:.1f} MB", end="", flush=True)
        print()


def extract(zip_path: Path, pkg: str, models_dir: Path) -> list[str]:
    models_dir.mkdir(parents=True, exist_ok=True)
    kept = []
    with zipfile.ZipFile(zip_path) as z:
        for info in z.infolist():
            name = Path(info.filename).name
            if name in KEEP[pkg]:
                target = models_dir / name
                with z.open(info) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                kept.append(f"{name} ({target.stat().st_size / 1e6:.1f} MB)")
    return kept


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pkg", choices=sorted(KEEP), default="buffalo_s")
    ap.add_argument("--models-dir", type=Path, default=PROJECT_ROOT / "data" / "models")
    ap.add_argument("--zip", type=Path, default=None, help="使用本地已下载的 zip 包")
    ap.add_argument("--list", action="store_true", help="仅列出已安装的模型")
    args = ap.parse_args()

    if args.list:
        for f in sorted(args.models_dir.glob("*.onnx")):
            print(f"{f.name}  {f.stat().st_size / 1e6:.1f} MB")
        return 0

    missing = KEEP[args.pkg] - {f.name for f in args.models_dir.glob("*.onnx")}
    if not missing:
        print("all models already present:", ", ".join(sorted(KEEP[args.pkg])))
    else:
        if args.zip:
            zip_path = args.zip
        else:
            zip_path = Path(tempfile.gettempdir()) / f"{args.pkg}.zip"
            if not zip_path.exists() or zip_path.stat().st_size < 1_000_000:
                download(URL_TMPL.format(pkg=args.pkg), zip_path)

        kept = extract(zip_path, args.pkg, args.models_dir)
        print("installed:")
        for k in kept:
            print(" ", k)
        still = KEEP[args.pkg] - {f.name for f in args.models_dir.glob("*.onnx")}
        if still:
            print("MISSING (zip 内未找到):", ", ".join(sorted(still)))
            return 1

    # ---- R2：直链补充模型（headpose 等；失败仅警告，引擎自动降级）----
    args.models_dir.mkdir(parents=True, exist_ok=True)
    for name, url in EXTRA_DIRECT.items():
        target = args.models_dir / name
        if target.is_file() and target.stat().st_size > 1_000_000:
            print(f"already present: {name} ({target.stat().st_size / 1e6:.1f} MB)")
            continue
        try:
            download(url, target)
        except Exception as exc:  # noqa: BLE001 —— 可选模型，失败不阻塞
            print(f"WARN: {name} download failed ({exc}); "
                  f"head pose falls back to 5-point heuristic")
            continue
        if target.is_file() and target.stat().st_size > 1_000_000:
            print(f"  ok: {name} ({target.stat().st_size / 1e6:.1f} MB)")
        else:
            print(f"WARN: {name} missing after download; "
                  f"head pose falls back to 5-point heuristic")

    generated = ["beauty_scut.onnx", "clip_vitb32_face.onnx", "clip_vitb32_prompts.npz"]
    absent = [g for g in generated if not (args.models_dir / g).is_file()]
    if absent:
        print("\nNOTE（可选增强，需从开发机复制或本地生成，见 data/models/README.md）:")
        for g in absent:
            print(f"  - {g}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
