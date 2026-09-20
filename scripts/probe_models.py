"""模型探针（第 2 版）：检测→对齐→genderage(96×96, fc1[1,3])→特征 预处理实测（M3 调试用）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
MODELS = ROOT / "data" / "models"
LENA = ROOT / "backend" / "tests" / "assets" / "lena.jpg"

from app.services.face_engine import SCRFD, align_face  # noqa: E402

det = SCRFD(MODELS / "det_500m.onnx")
img = cv2.imread(str(LENA))
dets = det.detect(img, 0.5)
print("detections:", len(dets))
for d in dets:
    print("  score=%.3f bbox=%s" % (d.det_score, [round(v, 1) for v in d.bbox]))
aligned = align_face(img, dets[0].kps)
cv2.imwrite(str(ROOT / "data" / "models" / "_probe_aligned.jpg"), aligned)
print("aligned saved (应为人脸正脸 112x112)")

import onnxruntime as ort  # noqa: E402

gs = ort.InferenceSession(str(MODELS / "genderage.onnx"), providers=["CPUExecutionProvider"])
gi = gs.get_inputs()[0].name
print("\ngenderage input:", gi, gs.get_inputs()[0].shape, "outputs:", [o.shape for o in gs.get_outputs()])
x112 = aligned.astype(np.float32)
g96 = cv2.resize(x112, (96, 96))
variants = {
    "raw(0-255)": g96,
    "div255": g96 / 255.0,
    "arc(-1..1)": (g96 - 127.5) / 127.5,
}
for name, v in variants.items():
    for order, tag in ((v, "RGB"), (v[:, :, ::-1].copy(), "BGR")):
        inp = order.transpose(2, 0, 1)[None].copy()
        out = np.asarray(gs.run(None, {gi: inp})[0]).reshape(-1)
        g_logits = out[:2]
        prob = np.exp(g_logits - g_logits.max())
        prob /= prob.sum()
        print(f"  {name:11s} {tag}: fc1={np.round(out, 3).tolist()} "
              f"gender_p={np.round(prob, 3).tolist()} age~{out[2]:.1f}")

es = ort.InferenceSession(str(MODELS / "w600k_mbf.onnx"), providers=["CPUExecutionProvider"])
ei = es.get_inputs()[0].name
print("\nemb input:", ei, es.get_inputs()[0].shape, "outputs:", [o.shape for o in es.get_outputs()])
for name, v in (("arc", (x112 - 127.5) / 127.5), ("raw", x112), ("div255", x112 / 255.0)):
    for order, tag in ((v, "RGB"), (v[:, :, ::-1].copy(), "BGR")):
        inp = order.transpose(2, 0, 1)[None].copy()
        o = np.asarray(es.run(None, {ei: inp})[0]).reshape(-1)
        print(f"  {name:6s} {tag}: dim={o.shape[0]} norm={float(np.linalg.norm(o)):.3f} head={np.round(o[:3], 3).tolist()}")
