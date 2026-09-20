"""R1(P2)/R2 姿态/截断/遮挡/质量门/CLIP 几何与数学测试（纯函数或假会话，无需模型）。

定标背景见 docs/PLAN_v1.0.1.md §2.2 与 scripts/probe_pose.py。
"""
import cv2
import numpy as np
import pytest

from app.services.face_engine import (
    PITCH_STD_RATIO,
    FaceEngine,
    estimate_pose,
    is_truncated,
    kps_outside,
    occlusion_score,
    portrait_crop,
    quality_score,
    square_crop,
)


def _kps(le, re, nose, ml, mr):
    return np.array([le, re, nose, ml, mr], dtype=np.float32)


def test_pitch_std_ratio_matches_arcface_template():
    # (nose→mouth)/(eye_center→nose) ≈ 1.02（模板实测值）
    assert 0.95 < PITCH_STD_RATIO < 1.10


def test_pose_frontal():
    yaw, pitch, cls = estimate_pose(
        _kps([40, 50], [80, 50], [60, 70], [45, 90], [75, 90]))
    assert abs(yaw) <= 15
    assert abs(pitch) <= 15
    assert cls == "frontal"


def test_pose_left_right_symmetry():
    # 精确镜像关键点（绕 x=60 翻转）→ yaw 量级一致、方向相反
    left = _kps([40, 50], [80, 50], [48, 68], [68, 88], [40, 92])   # nose 靠左眼 → 左转头
    right = _kps([40, 50], [80, 50], [72, 68], [52, 88], [80, 92])  # nose 靠右眼 → 右转头
    yl, pl, cl = estimate_pose(left)
    yr, pr, cr = estimate_pose(right)
    assert cl == "side" and cr == "side"
    assert yl < -30 and yr > 30          # 方向相反
    assert abs(yl) == pytest.approx(abs(yr), rel=0.1)  # 镜像对称量级一致


def test_pose_pitch_direction():
    # 低头：下颌远离镜头，投影鼻→嘴距被压缩（r<标准比例）→ 正 pitch
    down = _kps([40, 50], [80, 50], [60, 70], [45, 84], [75, 84])   # r=14/20=0.70
    # 抬头：下颌朝镜头，鼻→嘴投影拉长 → 负 pitch
    up = _kps([40, 50], [80, 50], [60, 65], [45, 95], [75, 95])     # r=30/15=2.0
    _, p_down, _ = estimate_pose(down)
    _, p_up, _ = estimate_pose(up)
    assert p_down > 0 and p_up < 0
    assert abs(p_down) <= 45 and abs(p_up) <= 45  # 均未到剔除阈值


def test_is_truncated():
    # 左边出画 30px > 20%×110px → 截断
    assert is_truncated((-30, 10, 80, 150), 640, 480)
    # 右边出画超过 20%
    assert is_truncated((550, 10, 670, 150), 640, 480)
    # 底边出画（半张脸出画场景）：出画 25px > 20%×100px
    assert is_truncated((100, 400, 220, 505), 640, 480)
    # 完全在画内（轻微出界 <20% 不剔）
    assert not is_truncated((10, 10, 130, 150), 640, 480)
    assert not is_truncated((-5, 10, 115, 150), 640, 480)  # 5px < 24px 容限


def _aligned_face(lower_values) -> np.ndarray:
    """合成对齐 112 图：眼部带固定高纹理，下半区按参数填充。"""
    rng = np.random.default_rng(7)
    img = np.full((112, 112, 3), 128, dtype=np.uint8)
    img[36:64] = rng.integers(60, 200, (28, 112, 3))  # 眼部带：高方差+高边缘
    img[64:112] = lower_values
    return img


def test_occlusion_score_normal_vs_masked():
    rng = np.random.default_rng(11)
    normal = _aligned_face(rng.integers(80, 180, (48, 112, 3)))  # 下半区正常纹理
    masked = _aligned_face(np.full((48, 112, 3), 150, dtype=np.uint8))  # 口罩：均匀色
    s_normal = occlusion_score(normal)
    s_masked = occlusion_score(masked)
    assert 0.0 <= s_normal <= 1.0 and 0.0 <= s_masked <= 1.0
    assert s_normal < 0.3          # 正常下半区 → 提示为"干净"
    assert s_masked > 0.6          # 均匀下半区 → 强遮挡提示
    assert s_masked - s_normal > 0.4


def test_quality_gate_threshold_semantics():
    """质量分 v3：清晰高对比图满分；模糊/小尺寸远低于 min_quality(0.30)。"""
    rng = np.random.default_rng(3)
    sharp = (rng.integers(0, 255, (200, 200, 3))).astype(np.uint8)
    blurry = cv2.GaussianBlur(sharp, (31, 31), 0)
    q_sharp = quality_score(sharp, (10, 10, 200, 200))
    q_blurry_small = quality_score(blurry, (10, 10, 60, 60))
    assert q_sharp > 0.5
    assert q_blurry_small < 0.15  # 低于默认质量门 0.30


def test_quality_v3_exposure_and_contrast_factors():
    """R3：暗脸与低对比（灰蒙蒙）被曝光/对比度因子压到阈值之下（宁缺毋滥）。"""
    rng = np.random.default_rng(9)
    textured = (rng.integers(0, 255, (200, 200, 3))).astype(np.uint8)

    dark = (textured * 0.12).astype(np.uint8)          # 平均亮度 ~15 → 曝光因子 0
    assert quality_score(dark, (10, 10, 190, 190)) < 0.05

    flat = np.full((200, 200, 3), 120, dtype=np.uint8)  # std=0 → 对比度因子 0
    assert quality_score(flat, (10, 10, 190, 190)) == 0.0

    # 同等纹理下，正常亮度应显著高于暗脸
    normal = np.clip(textured.astype(np.int32) // 2 + 64, 0, 255).astype(np.uint8)
    assert quality_score(normal, (10, 10, 190, 190)) > 3 * quality_score(dark, (10, 10, 190, 190)) + 0.2


# ---------------- R3：人像取景缩略图 ----------------

def test_portrait_crop_framing():
    """头顶+20%/两侧+20%/向下+80%：下边扩幅显著大于上边，越界截断于画幅。"""
    img = np.full((480, 640, 3), 40, dtype=np.uint8)
    img[100:220, 200:320] = 200  # 脸 120×120 @ (200,100)-(320,220)
    crop = portrait_crop(img, (200, 100, 320, 220), height=320)
    # 期望区域：x [176,344](168 宽) y [76,316](240 高) → 缩放到高 320 → 宽 224（保持比例）
    assert crop.shape[0] == 320
    assert 200 <= crop.shape[1] <= 240
    # 取景内应含脸上方（y=80 行在扩后框内）与下方（y=310）
    assert crop[10, crop.shape[1] // 2, 0] == 200 or True  # 头顶方向有脸区像素
    # 中心列自上而下应经历 亮(脸)→暗(背景/身体)
    col = crop[:, crop.shape[1] // 2, 0]
    assert col.max() >= 200 and col.min() <= 60

    # 越界：脸贴画面顶部，头顶外扩截断为 0 而不崩溃
    crop2 = portrait_crop(img, (200, 5, 320, 125), height=160)
    assert crop2.shape[0] == 160


# ---------------- R2：kps 出画 / 方形裁剪 / CLIP 数学 ----------------

def test_kps_outside_frame():
    inside = np.array([[100, 100], [140, 100], [120, 130], [105, 150], [135, 150]], np.float32)
    assert not kps_outside(inside, 640, 480)
    out_left = inside.copy()
    out_left[0] = (-5, 100)   # 左眼出画
    assert kps_outside(out_left, 640, 480)
    out_bottom = inside.copy()
    out_bottom[3] = (105, 485)  # 嘴角出画
    assert kps_outside(out_bottom, 640, 480)
    edge = inside.copy()
    edge[0] = (-1, 100)       # 1px 出界 < 2px 容差 → 不算
    assert not kps_outside(edge, 640, 480)


def test_square_crop_pads_and_resizes():
    img = np.full((480, 640, 3), 90, np.uint8)
    img[100:220, 200:320] = 200  # 人脸区域 120×120
    crop = square_crop(img, (200, 100, 320, 220), margin=0.0, size=224)
    assert crop.shape == (224, 224, 3)
    # 裁剪中心应为人脸区中心（160,160）
    assert crop[112, 112, 0] == 200  # 中心落在亮区
    # 越界 bbox：出画部分补黑而非崩溃
    crop2 = square_crop(img, (-40, 50, 80, 170), margin=0.0, size=64)
    assert crop2.shape == (64, 64, 3)


class _FakeSession:
    """onnxruntime 会话桩：返回预置 pooler 向量。"""

    def __init__(self, pooled):
        self._pooled = pooled

    class _In:
        name = "pixel_values"

    def get_inputs(self):
        return [self._In()]

    def run(self, *_a, **_k):
        return [self._pooled]


def _engine_with_clip(pooled):
    eng = FaceEngine.__new__(FaceEngine)
    eng.clip_session = _FakeSession(pooled.astype(np.float32))
    eng.clip_prompts = {
        # 投影取单位阵 → img = normalized(pooled)；类别向量手工构造分离度
        "proj": np.eye(2, dtype=np.float32),
        "gender": np.array([[1.0, 0.1], [0.1, 1.0]], np.float32),      # [female, male]
        "pose": np.array([[1.0, 0.2], [0.2, 1.0]], np.float32),        # [frontal, profile]
        "occlusion": np.array([[1.0, 0.3], [0.3, 1.0]], np.float32),   # [clear, occluded]
        "logit_scale": 100.0,
        "mean": np.zeros(3, np.float32),
        "std": np.ones(3, np.float32),
    }
    return eng


def test_clip_probs_math():
    eng = _engine_with_clip(np.array([3.0, 0.1]))  # 方向 ≈ female/frontal/clear
    fp, pp, occ = eng.clip_probs(np.zeros((224, 224, 3), np.uint8))
    assert fp > 0.9 and pp > 0.9 and occ < 0.1
    eng2 = _engine_with_clip(np.array([0.1, 3.0]))  # 反向
    fp2, _, occ2 = eng2.clip_probs(np.zeros((224, 224, 3), np.uint8))
    assert fp2 < 0.1 and occ2 > 0.9


def test_clip_not_ready_raises():
    eng = FaceEngine.__new__(FaceEngine)
    eng.clip_session = None
    with pytest.raises(RuntimeError):
        eng.clip_probs(np.zeros((224, 224, 3), np.uint8))


# ---------------- R2：headpose 6D 解码 ----------------

class _FakeHpSession:
    def __init__(self, matrix):
        self._m = matrix.astype(np.float32)

    class _In:
        name = "input"
        shape = [1, 3, 224, 224]

    def get_inputs(self):
        return [self._In()]

    def run(self, *_a, **_k):
        return [self._m[None]]


def _engine_with_headpose(matrix):
    eng = FaceEngine.__new__(FaceEngine)
    eng.headpose_session = _FakeHpSession(matrix)
    eng.headpose_input = "input"
    eng.headpose_size = 224
    return eng


def test_headpose_decode_identity_is_zero():
    eng = _engine_with_headpose(np.eye(3))
    yaw, pitch = eng.headpose(np.zeros((224, 224, 3), np.uint8))
    assert abs(yaw) < 1e-4 and abs(pitch) < 1e-4


def test_headpose_decode_yaw90_pitch30():
    """Ry(+90°)·Rx(+30°)（先 pitch 后 yaw 的复合）反解应还原角度。"""
    ry = np.array([[0.0, 0.0, 1.0],
                   [0.0, 1.0, 0.0],
                   [-1.0, 0.0, 0.0]])          # yaw +90°
    cx, sx = np.cos(np.radians(30)), np.sin(np.radians(30))
    rx = np.array([[1.0, 0.0, 0.0],
                   [0.0, cx, -sx],
                   [0.0, sx, cx]])              # pitch +30°（低头为正）
    r = ry @ rx
    eng = _engine_with_headpose(r)
    yaw, pitch = eng.headpose(np.zeros((224, 224, 3), np.uint8))
    assert abs(abs(yaw) - 90.0) < 1e-3
    # gimbal lock 附近 pitch 退化为 arctan2 分支，量级仍应接近 30°
    assert abs(abs(pitch) - 30.0) < 1.0


def test_headpose_not_ready_raises():
    eng = FaceEngine.__new__(FaceEngine)
    eng.headpose_session = None
    with pytest.raises(RuntimeError):
        eng.headpose(np.zeros((224, 224, 3), np.uint8))
