"""SCRFD 输入预处理扫描探针（M3 调试）。

对 det_500m 尝试 输入尺寸(640/512) × 缩放(1/128, 1/255, 1.0) × 通道序(RGB/BGR)
组合，打印 stride32 最高分 cell 坐标与解码框 —— 框落在人脸处即为正确预处理。
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
name = sess.get_inputs()[0].name
img = cv2.imread(str(LENA))
assert img is not None

for size in (640, 512):
    x = cv2.resize(img, (size, size))
    for scale_name, scaled in (("1/128", x.astype(np.float32) / 128.0),
                               ("1/255", x.astype(np.float32) / 255.0),
                               ("raw", x.astype(np.float32))):
        for swap_name, arr in (("RGB", scaled[:, :, ::-1].copy()), ("BGR", scaled)):
            blob = arr.transpose(2, 0, 1)[None].copy()
            outs = [np.asarray(o) for o in sess.run(None, {name: blob})]
            stride = 32
            fh = size // stride
            K, A = fh * fh, 2
            s = outs[2].reshape(-1)
            b = outs[5].reshape(-1, 4) * stride
            i = int(s.argmax())
            cell = i // A
            cx, cy = (cell % fh) * stride, (cell // fh) * stride
            d = b[i]
            box = (cx - d[0], cy - d[1], cx + d[2], cy + d[3])
            print(f"size={size} scale={scale_name} {swap_name}: "
                  f"top={s[i]:.3f} cell=({cx},{cy}) box=({box[0]:.0f},{box[1]:.0f},{box[2]:.0f},{box[3]:.0f})")
