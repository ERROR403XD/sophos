"""对照探针（M3 调试）：face_engine.SCRFD.detect vs probe2 式解码，同进程逐位比对。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import cv2
import numpy as np
import onnxruntime as ort

ROOT = Path(__file__).resolve().parents[1]
MODELS = ROOT / "data" / "models"
LENA = ROOT / "backend" / "tests" / "assets" / "lena.jpg"

from app.services.face_engine import SCRFD, _letterbox  # noqa: E402

img = cv2.imread(str(LENA))

print("=== A. face_engine.SCRFD.detect ===")
det = SCRFD(MODELS / "det_500m.onnx")
for d in det.detect(img, 0.5):
    print(f"  score={d.det_score:.3f} bbox=({d.bbox[0]:.0f},{d.bbox[1]:.0f},{d.bbox[2]:.0f},{d.bbox[3]:.0f})")
    print(f"  kps={np.round(d.kps, 1).tolist()}")

print("=== B. 手动解码（probe2 式）===")
sess = det.session
blob = cv2.dnn.blobFromImage(img, 1.0 / 128.0, (640, 640), swapRB=True)  # 显式 size
outs = [np.asarray(o) for o in sess.run(None, {det.input_name: blob})]
print("  n_outputs:", len(outs))
stride = 32
fh = 640 // stride
s = outs[2].reshape(-1)
b = outs[5].reshape(-1, 4) * stride
i = int(s.argmax())
cell = i // 2
cx, cy = (cell % fh) * stride, (cell // fh) * stride
d = b[i]
print(f"  top={s[i]:.3f} box=({cx-d[0]:.0f},{cy-d[1]:.0f},{cx+d[2]:.0f},{cy+d[3]:.0f})/1.25")

print("=== C. blob 一致性检查 ===")
padded, scale, pad_x, pad_y = _letterbox(img, det.input_size)
blob_a = cv2.dnn.blobFromImage(padded, 1.0 / 128.0, swapRB=True)          # face_engine 用法
blob_b = (padded[:, :, ::-1].astype(np.float32) / 128.0).transpose(2, 0, 1)[None].copy()
print("  blob shape:", blob_a.shape, blob_b.shape, "max|diff|=", float(np.abs(blob_a - blob_b).max()))
