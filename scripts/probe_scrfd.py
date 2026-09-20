"""SCRFD 输出布局探针（M3 调试用）。

排查两件事：
1) 9 个输出是 [s8,s16,s32,b8,b16,b32,k8,k16,k32] 块状，还是每 stride 交错 [s8,b8,k8,...]
   —— 用 shape 即可判别（不同 stride 的 K 不同）。
2) anchor 展平顺序是 cell-major（[c0a0,c0a1,c1a0,c1a1...]，insightface 约定）
   还是 anchor-major（[全部 a0 的 cell, 全部 a1 的 cell]）
   —— 对 stride8 的最高分位分别按两种布局解码并画框，肉眼比对。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import cv2
import numpy as np
import onnxruntime as ort

ROOT = Path(__file__).resolve().parents[1]
MODELS = ROOT / "data" / "models"
LENA = ROOT / "backend" / "tests" / "assets" / "lena.jpg"

sess = ort.InferenceSession(str(MODELS / "det_500m.onnx"), providers=["CPUExecutionProvider"])
inp = sess.get_inputs()[0]
print("input:", inp.name, inp.shape)
img = cv2.imread(str(LENA))
padded = cv2.resize(img, (640, 640))
blob = cv2.dnn.blobFromImage(padded, 1.0 / 128.0, swapRB=True)
outs = [np.asarray(o) for o in sess.run(None, {inp.name: blob})]
for i, o in enumerate(outs):
    print(f"out[{i}] shape={o.shape} min={o.min():.4f} max={o.max():.4f}")

STRIDES = (8, 16, 32)


def decode_layout(scores_flat, bbox_flat, kps_flat, stride, layout):
    fh = fw = 640 // stride
    K, A = fh * fw, 2
    yy, xx = np.mgrid[:fh, :fw]
    if layout == "cell_major":
        cx = np.stack([xx.reshape(-1), yy.reshape(-1)], axis=1)  # (K,2) x快? -> (x,y)
        centers = np.stack([cx, cx], axis=1).reshape(-1, 2)
    else:  # anchor_major
        cx = np.stack([xx.reshape(-1), yy.reshape(-1)], axis=1)
        centers = np.concatenate([cx, cx], axis=0)
    b = bbox_flat.reshape(-1, 4) * stride
    boxes = np.stack([centers[:, 0] - b[:, 0], centers[:, 1] - b[:, 1],
                      centers[:, 0] + b[:, 2], centers[:, 1] + b[:, 3]], axis=1)
    k = kps_flat.reshape(-1, 5, 2) * stride + centers[:, None, :]
    s = scores_flat.reshape(-1)
    order = s.argsort()[::-1][:3]
    return s, boxes, k, order


for stride_idx, stride in enumerate(STRIDES):
    s8, b8, k8 = outs[stride_idx], outs[stride_idx + 3], outs[stride_idx + 6]
    vis = padded.copy()
    for layout in ("cell_major", "anchor_major"):
        s, boxes, kps, order = decode_layout(s8, b8, k8, stride, layout)
        i = int(order[0])
        x1, y1, x2, y2 = boxes[i] / 1.25
        cv2.rectangle(vis, (int(x1), int(y1)), (int(x2), int(y2)), (0, 0, 255), 2)
        for p in kps[i]:
            cv2.circle(vis, (int(p[0] / 1.25), int(p[1] / 1.25)), 3, (0, 255, 0), -1)
        print(f"stride{stride} {layout}: top score={s[i]:.3f} box_orig=({x1:.0f},{y1:.0f},{x2:.0f},{y2:.0f})")
    cv2.imwrite(str(ROOT / "data" / "models" / f"_probe_stride{stride}.jpg"), vis)
print("\nsaved _probe_stride8/16/32.jpg")
