"""基础颜值打分（M4 实现）。

设计（docs/ARCHITECTURE.md、ADR-006）：
- 模型：SCUT-FBP5500 预训练 ResNet-18 回归（beauty_scut.onnx，
  由 scripts/convert_beauty_model.py 从 HF Gustrd 权重导出，MIT 许可）
- 输入：原帧 bbox 裁剪（外扩 25% 近似训练集"整张正脸照"构图）
  → 短边 resize 256 → 中心裁 224 → /255 → ImageNet 均值方差归一化（RGB, NCHW）
- 输出：raw ∈ ~[1,5]，线性映射 0-100：`clamp((raw-1)/4*100, 0, 100)`
- 版本化：face_score.base_score + base_model_version；接口抽象可换模型

与 insightface 对齐 112 图的区别：本模型训练集是整张正脸照（含发型/背景边距），
因此不喂 arcface 对齐图，而是原帧带边距 bbox 裁剪。
"""
from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

IMAGENET_MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
IMAGENET_STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)
MODEL_VERSION = "scut_resnet18_v1"


def preprocess_bgr_crop(img_bgr: np.ndarray, bbox: tuple[float, float, float, float],
                        margin: float = 0.25, size: int = 224) -> np.ndarray:
    """bbox 外扩 margin 裁剪 → 短边 resize 256 → 中心裁 224 → 归一化 NCHW RGB。"""
    h, w = img_bgr.shape[:2]
    x1, y1, x2, y2 = bbox
    bw, bh = x2 - x1, y2 - y1
    mx, my = bw * margin / 2, bh * margin / 2
    x1, y1 = max(0, int(x1 - mx)), max(0, int(y1 - my))
    x2, y2 = min(w, int(x2 + mx)), min(h, int(y2 + my))
    if x2 - x1 < 8 or y2 - y1 < 8:
        raise ValueError("face crop too small")
    crop = img_bgr[y1:y2, x1:x2]

    # 短边 resize 到 256
    ch, cw = crop.shape[:2]
    scale = 256.0 / min(ch, cw)
    crop = cv2.resize(crop, (max(224, int(round(cw * scale))), max(224, int(round(ch * scale)))))
    # 中心裁 224
    ch, cw = crop.shape[:2]
    x0, y0 = (cw - 224) // 2, (ch - 224) // 2
    crop = crop[y0:y0 + 224, x0:x0 + 224]

    x = crop[:, :, ::-1].astype(np.float32) / 255.0  # BGR→RGB
    x = (x - IMAGENET_MEAN) / IMAGENET_STD
    return x.transpose(2, 0, 1)[None].copy()  # NCHW


class BaseScorer:
    """SCUT-FBP5500 基础颜值打分器（ONNX）。"""

    version = MODEL_VERSION

    def __init__(self, model_path: str | Path, providers=None):
        import onnxruntime as ort

        self.session = ort.InferenceSession(str(model_path), providers=providers)
        self.input_name = self.session.get_inputs()[0].name

    def score_raw(self, img_bgr: np.ndarray, bbox: tuple[float, float, float, float]) -> float:
        """原始模型输出（约 1-5 分制）。"""
        blob = preprocess_bgr_crop(img_bgr, bbox)
        out = np.asarray(self.session.run(None, {self.input_name: blob})[0]).reshape(-1)[0]
        return float(out)

    def score(self, img_bgr: np.ndarray, bbox: tuple[float, float, float, float]) -> float:
        """映射到 0-100。"""
        raw = self.score_raw(img_bgr, bbox)
        return float(round(min(100.0, max(0.0, (raw - 1.0) / 4.0 * 100.0)), 1))
