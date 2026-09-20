"""颜值模型打分探针（M4 调试）：lena 实测各预处理变体的 raw 分（合理区间 ~1-5）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
MODELS = ROOT / "data" / "models"
LENA = ROOT / "backend" / "tests" / "assets" / "lena.jpg"

from app.services.face_engine import SCRFD, align_face  # noqa: E402
from app.services.scorer import IMAGENET_MEAN, IMAGENET_STD, BaseScorer, preprocess_bgr_crop  # noqa: E402

img = cv2.imread(str(LENA))
d = SCRFD(MODELS / "det_500m.onnx").detect(img, 0.5)[0]
print("bbox:", [round(v, 1) for v in d.bbox])
sc = BaseScorer(MODELS / "beauty_scut.onnx")


def run(blob, tag):
    raw = float(np.asarray(sc.session.run(None, {sc.input_name: blob})[0]).reshape(-1)[0])
    print(f"  {tag:28s} raw={raw:.3f}  -> 0-100: {min(100, max(0, (raw - 1) / 4 * 100)):.1f}")


print("bbox 裁剪变体：")
for m in (0.0, 0.25, 0.5, 1.0):
    try:
        run(preprocess_bgr_crop(img, d.bbox, margin=m), f"bbox margin={m}")
    except ValueError as e:
        print(f"  margin={m}: {e}")

print("对齐 112 变体：")
al = align_face(img, d.kps)
for tag, x in (("aligned->224 bilinear", cv2.resize(al, (224, 224))),):
    v = x[:, :, ::-1].astype(np.float32) / 255.0
    v = (v - IMAGENET_MEAN) / IMAGENET_STD
    run(v.transpose(2, 0, 1)[None], tag)

print("整图变体（对照）：")
run(preprocess_bgr_crop(img, (0, 0, img.shape[1], img.shape[0]), margin=0.0), "full image")
