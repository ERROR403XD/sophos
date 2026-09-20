"""人脸引擎测试（M3）：真实模型 + lena.jpg 实测（检测→对齐→性别→特征）。

模型未下载或素材缺失时自动跳过（见 data/models/README.md）。
"""
from pathlib import Path

import cv2
import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[2]
MODELS = ROOT / "data" / "models"
LENA = Path(__file__).parent / "assets" / "lena.jpg"

pytestmark = pytest.mark.skipif(
    not (MODELS / "det_500m.onnx").is_file() or not LENA.is_file(),
    reason="models not downloaded or lena.jpg missing")


@pytest.fixture(scope="module")
def engine():
    from app.services.face_engine import FaceEngine

    return FaceEngine(models_dir=MODELS)


@pytest.fixture(scope="module")
def lena():
    img = cv2.imread(str(LENA))
    assert img is not None
    return img


def test_detect_lena(engine, lena):
    dets = engine.detect(lena, det_thresh=0.5)
    assert len(dets) >= 1
    d = dets[0]
    assert d.det_score >= 0.5
    x1, y1, x2, y2 = d.bbox
    h, w = lena.shape[:2]
    assert w > x2 > x1 >= -30 and h > y2 > y1 >= -30  # 框在合理范围（允许轻微出界）
    assert d.kps.shape == (5, 2)
    # 人脸框应接近图像中部（lena 人脸约在 (206,186)-(356,392)）
    assert x1 < 260 and x2 > 300 and y1 < 250 and y2 > 350


def test_align_112(engine, lena):
    d = engine.detect(lena)[0]
    aligned = engine.align(lena, d.kps)
    assert aligned.shape == (112, 112, 3)


def test_embed_l2_normalized(engine, lena):
    d = engine.detect(lena)[0]
    aligned = engine.align(lena, d.kps)
    emb = engine.embed(aligned)
    assert emb.shape == (512,)
    assert abs(float(np.linalg.norm(emb)) - 1.0) < 1e-3
    # 同一人两次 embedding 应几乎一致（确定性）
    emb2 = engine.embed(engine.align(lena, d.kps))
    assert float(emb @ emb2) > 0.99


def test_female_prob_lena(engine, lena):
    """lena 为女性：female_prob 应 > 0.5（实测 ~0.999）。"""
    d = engine.detect(lena)[0]
    aligned = engine.align(lena, d.kps)
    fp = engine.female_prob(aligned)
    assert 0.0 <= fp <= 1.0
    assert fp > 0.5


def test_process_frame_pipeline(engine, lena):
    samples = engine.process_frame(lena, timestamp_sec=0.0, det_thresh=0.5,
                                   min_face=64, min_quality=0.15)
    assert len(samples) >= 1
    s = samples[0]
    assert s.quality_score > 0
    assert s.embedding.shape == (512,)
    assert s.female_prob > 0.5
    assert s.aligned.shape == (112, 112, 3)
    # R1(P2)：姿态与遮挡提示字段随样本落库
    assert s.pose_class in ("frontal", "near", "side")
    assert s.pose_yaw is not None and s.pose_pitch is not None
    assert 0.0 <= s.occlusion_score <= 1.0
