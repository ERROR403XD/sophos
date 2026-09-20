"""人脸引擎：ONNX 推理统一封装（M3 实现；R1 调整面容准入，ADR-014）。

流水线（docs/ARCHITECTURE.md §1、ADR-004/005/014）：
  检测(SCRFD det_500m/det_10g)
  → 样本级门控：尺寸(min_face) / 截断(出画>20%边长剔除) / 质量(拉普拉斯×尺寸≥min_quality)
    / 姿态(5 点启发式 yaw/pitch，只剔极端角度)
  → 5 点对齐 112×112（ArcFace 标准参考点）
  → genderage 记录 female_prob（**不再做样本级过滤**——P1 根因；
    女性裁决改为聚类后 identity 级投票，见 pipeline._gender_filter）
  → 遮挡提示 occlusion_score（下半区低纹理启发式，仅记录不剔除，P2）
  → MobileFaceNet(w600k_mbf) 512 维 embedding（L2 归一化）

约定：
- 模型文件位于 data/models/（清单见 data/models/README.md）
- 引擎进程内单例（pipeline.get_face_engine），接口与具体模型解耦；
  单元测试可用"假引擎"（固定输出）隔离 ONNX 依赖
- SCRFD 解码：9 输出（3 stride × [score, bbox, kps]），anchor center ± dist×stride，
  NMS IoU 0.4 —— 与 insightface 官方 scrfd 后处理一致
- 姿态为启发式（±10° 量级精度），阈值放宽只剔极端；
  定标工具 scripts/probe_pose.py（真实片源人工核对）
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import cv2
import numpy as np
from pathlib import Path

# ArcFace 112×112 标准 5 点（le, re, nose, mouth_l, mouth_r）
ARCFACE_SRC = np.array(
    [[38.2946, 51.6963], [73.5318, 51.5014], [56.0252, 71.7366],
     [41.5493, 92.3655], [70.7299, 92.2041]], dtype=np.float32)


def _dim(v, default: int = 640) -> int:
    """onnxruntime 的动态维度可能是字符串，这里统一收敛为 int。"""
    return int(v) if isinstance(v, int) and v > 0 else default


@dataclass
class Detection:
    bbox: tuple[float, float, float, float]  # x1,y1,x2,y2（原图坐标）
    det_score: float
    kps: np.ndarray  # (5,2)


@dataclass
class FaceSample:
    """通过样本级门控（尺寸/截断/质量/姿态）后的候选样本。

    R1(ADR-014)：性别不再做样本级过滤，female_prob 仅为记录值
    （identity 级裁决在聚类后进行）；occlusion_score 仅为提示值。
    R2：clip_female_prob / clip_profile_prob 为 CLIP 第二意见记录值
    （性别与 genderage 一致同意才入库；侧脸概率超阈值样本剔除）。
    """
    bbox: tuple[float, float, float, float]
    det_score: float
    quality_score: float
    female_prob: float
    timestamp_sec: float
    aligned: np.ndarray  # 112×112 BGR
    embedding: np.ndarray  # (512,) float32, L2 norm
    pose_yaw: float | None = None    # 度（右偏为正，仅审计/偏好排序用）
    pose_pitch: float | None = None  # 度（低头为正）
    pose_class: str | None = None    # frontal | near | side
    occlusion_score: float | None = None  # 0-1，≥0.6 视为疑似遮挡
    clip_female_prob: float | None = None   # R2：CLIP 女性 0-1（缺模型为 None）
    clip_profile_prob: float | None = None  # R2：CLIP 侧脸 0-1（缺模型为 None）
    base_score: float | None = None  # M4：流水线注入的颜值分（0-100）


def _nms(boxes: np.ndarray, scores: np.ndarray, iou_thresh: float = 0.4) -> list[int]:
    order = scores.argsort()[::-1]
    keep: list[int] = []
    while order.size > 0:
        i = order[0]
        keep.append(int(i))
        if order.size == 1:
            break
        rest = order[1:]
        xx1 = np.maximum(boxes[i, 0], boxes[rest, 0])
        yy1 = np.maximum(boxes[i, 1], boxes[rest, 1])
        xx2 = np.minimum(boxes[i, 2], boxes[rest, 2])
        yy2 = np.minimum(boxes[i, 3], boxes[rest, 3])
        w = np.maximum(0.0, xx2 - xx1)
        h = np.maximum(0.0, yy2 - yy1)
        inter = w * h
        area_i = (boxes[i, 2] - boxes[i, 0]) * (boxes[i, 3] - boxes[i, 1])
        area_r = (boxes[rest, 2] - boxes[rest, 0]) * (boxes[rest, 3] - boxes[rest, 1])
        iou = inter / np.maximum(1e-9, area_i + area_r - inter)
        order = rest[iou <= iou_thresh]
    return keep


def _letterbox(img: np.ndarray, size: tuple[int, int]) -> tuple[np.ndarray, float, int, int]:
    """等比缩放并 pad 到 size，返回 (blob 源图, scale, pad_x, pad_y)。"""
    h, w = img.shape[:2]
    tw, th = size
    scale = min(tw / w, th / h)
    nw, nh = int(round(w * scale)), int(round(h * scale))
    resized = cv2.resize(img, (nw, nh)) if (nw, nh) != (w, h) else img.copy()
    padded = np.zeros((th, tw, 3), dtype=np.uint8)
    pad_x, pad_y = (tw - nw) // 2, (th - nh) // 2
    padded[pad_y:pad_y + nh, pad_x:pad_x + nw] = resized
    return padded, scale, pad_x, pad_y


class SCRFD:
    """SCRFD 检测器（det_500m / det_10g，带 5 点关键点）。"""

    STRIDES = (8, 16, 32)

    def __init__(self, model_path: str | Path, providers=None):
        import onnxruntime as ort

        self.session = ort.InferenceSession(str(model_path), providers=providers)
        inp = self.session.get_inputs()[0]
        self.input_name = inp.name
        self.input_size = (_dim(inp.shape[2]), _dim(inp.shape[3]))  # (h, w)
        self.num_anchors = 2  # det_500m / det_10g 均为 2 anchor 版本

    def detect(self, img_bgr: np.ndarray, det_thresh: float = 0.5) -> list[Detection]:
        h0, w0 = img_bgr.shape[:2]
        padded, scale, pad_x, pad_y = _letterbox(img_bgr, self.input_size)
        blob = cv2.dnn.blobFromImage(padded, 1.0 / 128.0, swapRB=True)  # SCRFD: /128, RGB, no mean
        outs = self.session.run(None, {self.input_name: blob})
        fmc = 3  # score/bbox/kps × 3 strides

        ih, iw = self.input_size
        all_boxes, all_scores, all_kpss = [], [], []
        for idx, stride in enumerate(self.STRIDES):
            scores = outs[idx]
            bbox_preds = outs[idx + fmc] * stride
            kps_preds = outs[idx + fmc * 2] * stride
            fh, fw = ih // stride, iw // stride
            K = fh * fw
            A = self.num_anchors
            # (fh,fw,2) 先展平成 (K,2) 再复制 anchor —— 顺序必须与 ONNX 展平一致
            centers = np.stack(np.mgrid[:fh, :fw][::-1], axis=-1).reshape(-1, 2) * stride
            centers = np.stack([centers] * A, axis=1).reshape(-1, 2)  # (K*A,2) cell-major

            s = scores.reshape(K * A, 1)
            b = bbox_preds.reshape(K * A, 4)
            p = kps_preds.reshape(K * A, 5, 2)
            mask = (s[:, 0] > det_thresh)
            if not mask.any():
                continue
            c, bb, pp, ss = centers[mask], b[mask], p[mask], s[mask]
            boxes = np.stack([c[:, 0] - bb[:, 0], c[:, 1] - bb[:, 1],
                              c[:, 0] + bb[:, 2], c[:, 1] + bb[:, 3]], axis=1)
            kpss = c[:, None, :] + pp
            all_boxes.append(boxes)
            all_scores.append(ss)
            all_kpss.append(kpss)

        results: list[Detection] = []
        if not all_boxes:
            return results
        boxes = np.concatenate(all_boxes)
        scores = np.concatenate(all_scores)[:, 0]
        kpss = np.concatenate(all_kpss)
        keep = _nms(boxes, scores)
        for i in keep:
            x1, y1, x2, y2 = boxes[i]
            # 映射回原图坐标（letterbox 逆变换）
            bx = ((x1 - pad_x) / scale, (y1 - pad_y) / scale,
                  (x2 - pad_x) / scale, (y2 - pad_y) / scale)
            bk = (kpss[i] - np.array([pad_x, pad_y])) / scale
            results.append(Detection(bbox=bx, det_score=float(scores[i]), kps=bk.astype(np.float32)))
        return results


def align_face(img_bgr: np.ndarray, kps: np.ndarray, size: int = 112) -> np.ndarray:
    """按 5 点做相似变换对齐到 ArcFace 标准位置。"""
    src = ARCFACE_SRC if size == 112 else ARCFACE_SRC * (size / 112.0)
    m, _ = cv2.estimateAffinePartial2D(kps.reshape(5, 2).astype(np.float32), src)
    if m is None:
        raise ValueError("alignment failed (bad keypoints)")
    return cv2.warpAffine(img_bgr, m, (size, size))


def quality_score(img_bgr: np.ndarray, bbox: tuple[float, float, float, float]) -> float:
    """启发式质量分 0-1（R3 v3）：清晰度 × 尺寸 × 曝光 × 对比度。

    R3 治"暗脸/糊脸"（宁缺毋滥），四因子须同时达标：
    - sharp：拉普拉斯方差 / 80（**重新定标**：正常视频人脸 lap≈32-57 → 0.4-0.7，
      模糊 <20 → <0.25；旧 /200 归一把视频脸全压在 0.15-0.3 无法区分）；
    - size：短边/128 封顶；
    - exposure：平均亮度 <30 线性衰减到 0（暗场治理），≥55 满分；
    - contrast：灰度标准差 <12 衰减到 0（灰蒙蒙治理），≥32 满分。
    阈值 SOPHOS_MIN_QUALITY 默认 0.30（R3 由 0.15 上调；E01 已认可脸实测 0.30-0.71）。
    """
    x1, y1, x2, y2 = [int(round(v)) for v in bbox]
    h, w = img_bgr.shape[:2]
    x1, y1 = max(0, x1), max(0, y1)
    x2, y2 = min(w, x2), min(h, y2)
    if x2 - x1 < 4 or y2 - y1 < 4:
        return 0.0
    crop = cv2.cvtColor(img_bgr[y1:y2, x1:x2], cv2.COLOR_BGR2GRAY)
    blur = cv2.Laplacian(crop, cv2.CV_64F).var()
    sharp = min(1.0, blur / 80.0)
    size_factor = min(1.0, min(x2 - x1, y2 - y1) / 128.0)
    lum = float(crop.mean())
    exposure = float(np.clip((lum - 30.0) / 25.0, 0.0, 1.0))
    contrast = float(np.clip((float(crop.std()) - 12.0) / 20.0, 0.0, 1.0))
    return float(round(sharp * size_factor * exposure * contrast, 4))


# ---------------- R1(P2)：截断 / 姿态 / 遮挡启发式 ----------------

def is_truncated(bbox: tuple[float, float, float, float],
                 frame_w: int, frame_h: int, ratio: float = 0.2) -> bool:
    """bbox 任一边出画超过自身边长 ratio（默认 20%）→ 不完整人脸，剔除。"""
    x1, y1, x2, y2 = bbox
    w_box, h_box = x2 - x1, y2 - y1
    over_l = max(0.0, -x1)
    over_r = max(0.0, x2 - frame_w)
    over_t = max(0.0, -y1)
    over_b = max(0.0, y2 - frame_h)
    return (over_l > ratio * w_box or over_r > ratio * w_box
            or over_t > ratio * h_box or over_b > ratio * h_box)


def kps_outside(kps: np.ndarray, frame_w: int, frame_h: int, tol: float = 2.0) -> bool:
    """任一关键点出画（±tol 容差）→ 脸明显不完整（R2：治"明显看不全"）。"""
    xs, ys = kps[:, 0], kps[:, 1]
    return bool((xs < -tol).any() or (ys < -tol).any()
                or (xs > frame_w + tol).any() or (ys > frame_h + tol).any())


def square_crop(img_bgr: np.ndarray, bbox: tuple[float, float, float, float],
                margin: float = 0.15, size: int = 224) -> np.ndarray:
    """bbox 外扩为方形裁剪（出画部分补边）→ resize size×size（CLIP 输入，保整脸）。"""
    h, w = img_bgr.shape[:2]
    x1, y1, x2, y2 = bbox
    cx, cy = (x1 + x2) / 2.0, (y1 + y2) / 2.0
    side = max(x2 - x1, y2 - y1) * (1.0 + 2.0 * margin)
    half = side / 2.0
    # 方形区域（原图坐标），越界部分用边缘补
    sx1, sy1 = int(round(cx - half)), int(round(cy - half))
    sx2, sy2 = int(round(cx + half)), int(round(cy + half))
    chan = img_bgr.shape[2] if img_bgr.ndim == 3 else 1
    canvas = np.zeros((max(sy2 - sy1, 1), max(sx2 - sx1, 1), chan), dtype=img_bgr.dtype)
    # 源区域与目标区域对齐拷贝
    ix1, iy1 = max(0, sx1), max(0, sy1)
    ix2, iy2 = min(w, sx2), min(h, sy2)
    if ix2 > ix1 and iy2 > iy1:
        canvas[iy1 - sy1:iy2 - sy1, ix1 - sx1:ix2 - sx1] = img_bgr[iy1:iy2, ix1:ix2]
    return cv2.resize(canvas, (size, size))


# R3：缩略图人像取景比例（相对 bbox 自身边长）——头顶 +20%、两侧各 +20%、向下 +80%
PORTRAIT_TOP, PORTRAIT_SIDE, PORTRAIT_BOTTOM = 0.20, 0.20, 0.80
PORTRAIT_HEIGHT = 320  # 缩略图输出高度（评分卡片显示 280px，留余量）


def portrait_crop(img_bgr: np.ndarray, bbox: tuple[float, float, float, float],
                  height: int = PORTRAIT_HEIGHT) -> np.ndarray:
    """人像取景裁剪（R3）：bbox 按头顶+20%/两侧各+20%/向下+80% 外扩（越界截断于画幅），
    按比例缩放到目标高度。用于缩略图展示（审美观感），不用于特征/打分输入。
    """
    h, w = img_bgr.shape[:2]
    x1, y1, x2, y2 = bbox
    bw, bh = x2 - x1, y2 - y1
    ex1 = max(0, int(round(x1 - PORTRAIT_SIDE * bw)))
    ex2 = min(w, int(round(x2 + PORTRAIT_SIDE * bw)))
    ey1 = max(0, int(round(y1 - PORTRAIT_TOP * bh)))
    ey2 = min(h, int(round(y2 + PORTRAIT_BOTTOM * bh)))
    crop = img_bgr[ey1:ey2, ex1:ex2]
    if crop.size == 0:
        crop = img_bgr
    scale = height / crop.shape[0]
    out = cv2.resize(crop, (max(1, int(round(crop.shape[1] * scale))), height))
    return out


# yaw 经验放大系数：asym=(dL-dR)/(dL+dR) → yaw=arcsin(asym)×AMP。
# 依据：正脸 asym≈±0.1、3/4 侧≈0.5、近全侧≈0.85；×1.5 后分别 ≈9°/45°/87°，
# 与肉眼角度量级吻合（scripts/probe_pose.py 真实片源定标可调）。
YAW_AMP = 1.5

# 简化 3D 面部模型反解 pitch 的形状参数（相对 eye→nose 垂直距 h1）：
# 鼻尖前凸 a·h1、嘴相对鼻的深度差 b·h1；r(φ)=(C·cosφ+b·sinφ)/(cosφ−a·sinφ) 单调增。
_PITCH_A, _PITCH_B = 0.6, 0.3
# ArcFace 模板标准比例 (nose→mouth 垂直距)/(eye_center→nose 垂直距) ≈ 1.02
PITCH_STD_RATIO = (((92.2041 + 92.3655) / 2) - 71.7366) / (71.7366 - ((51.6963 + 51.5014) / 2))


def estimate_pose(kps: np.ndarray) -> tuple[float, float, str]:
    """5 点启发式姿态估计（原图坐标）：返回 (yaw_deg, pitch_deg, pose_class)。

    yaw：dL=|nose.x−le.x|、dR=|re.x−nose.x|，asym=(dL−dR)/(dL+dR)，arcsin×经验放大。
    pitch：r=(nose→mouth 垂直距)/(eye_center→nose 垂直距) 与 ArcFace 标准比例的偏差，
    按简化 3D 模型二分反解。**低头为正**：低头使下颌远离镜头、投影鼻→嘴距被压缩
    （r < 标准比例）→ 反解角取负号后为正（真实片源 probe_pose.py 目检定标）。
    pose_class：frontal |yaw|≤15、near ≤45、side 其余。
    精度 ±10° 量级，只用于剔除极端角度（阈值放宽，见 SOPHOS_MAX_YAW/PITCH_DEG）。
    """
    le, re, nose, ml, mr = (np.asarray(p, dtype=np.float64) for p in kps[:5])
    # ---- yaw ----
    dL = abs(nose[0] - le[0])
    dR = abs(re[0] - nose[0])
    asym = (dL - dR) / max(1e-6, dL + dR)
    yaw = math.degrees(math.asin(np.clip(asym, -1.0, 1.0))) * YAW_AMP
    # ---- pitch ----
    eye_y = (le[1] + re[1]) / 2.0
    mouth_y = (ml[1] + mr[1]) / 2.0
    den = nose[1] - eye_y
    pitch = 0.0
    if abs(den) > 1e-3 and mouth_y > nose[1] > eye_y:
        r = (mouth_y - nose[1]) / den
        lo, hi = -55.0, 55.0  # 模型分母在 ±59° 外变号，够覆盖 40° 剔除阈值

        def _r(phi_deg: float) -> float:
            phi = math.radians(phi_deg)
            c, s = math.cos(phi), math.sin(phi)
            return (PITCH_STD_RATIO * c + _PITCH_B * s) / max(1e-6, c - _PITCH_A * s)

        if r <= _r(lo):
            pitch = lo
        elif r >= _r(hi):
            pitch = hi
        else:
            for _ in range(40):  # 二分：r(φ) 单调增
                mid = (lo + hi) / 2.0
                if _r(mid) < r:
                    lo = mid
                else:
                    hi = mid
            pitch = (lo + hi) / 2.0
    a = abs(yaw)
    pose_class = "frontal" if a <= 15 else ("near" if a <= 45 else "side")
    return round(yaw, 2), round(-pitch, 2), pose_class  # 取负：低头（r 小）为正


def occlusion_score(aligned: np.ndarray) -> float:
    """遮挡启发式 0-1：对齐 112 图下半区（嘴/下巴）相对眼部带的纹理缺失程度。

    口罩/面罩等会使 y∈[64,112] 的灰度方差与边缘密度显著低于 y∈[36,64]（眼部带，
    天然含睫毛/眉毛高纹理）。比值 r≥0.55 视为正常（0 分），≤0.15 视为强遮挡（1 分），
    线性过渡。仅作提示：不剔除任何样本（库内保留语义，ADR-014 遮挡三规则）。
    """
    gray = cv2.cvtColor(aligned, cv2.COLOR_BGR2GRAY).astype(np.float32)
    upper = gray[36:64]
    lower = gray[64:112]
    var_u, var_l = float(upper.var()), float(lower.var())
    lap_u = float(np.abs(cv2.Laplacian(upper, cv2.CV_32F)).mean())
    lap_l = float(np.abs(cv2.Laplacian(lower, cv2.CV_32F)).mean())

    def _deficit(ratio: float) -> float:
        return float(np.clip((0.55 - ratio) / 0.40, 0.0, 1.0))

    s = 0.5 * _deficit(var_l / max(1e-6, var_u)) + 0.5 * _deficit(lap_l / max(1e-6, lap_u))
    return round(s, 4)


class FaceEngine:
    """多模型组合引擎：检测 → 对齐 → 性别 → 特征（+ R2 CLIP 第二意见，可选）。"""

    def __init__(self, models_dir: str | Path,
                 det_name: str = "det_500m.onnx",
                 emb_name: str = "w600k_mbf.onnx",
                 gender_name: str = "genderage.onnx",
                 clip_name: str = "clip_vitb32_face.onnx",
                 clip_prompts_name: str = "clip_vitb32_prompts.npz",
                 headpose_name: str = "headpose_resnet18.onnx",
                 providers=None):
        models_dir = Path(models_dir)
        for name in (det_name, emb_name, gender_name):
            if not (models_dir / name).is_file():
                raise FileNotFoundError(
                    f"model not found: {models_dir / name} "
                    f"(run scripts/download_models.py, see data/models/README.md)")
        self.detector = SCRFD(models_dir / det_name, providers=providers)
        import onnxruntime as ort

        providers = providers or ["CPUExecutionProvider"]
        self.emb_session = ort.InferenceSession(str(models_dir / emb_name), providers=providers)
        self.gender_session = ort.InferenceSession(str(models_dir / gender_name), providers=providers)

        # R2：头部姿态模型（可选；6DRepNet 系 ResNet-18，见 data/models/README.md）。
        # 文件缺失时姿态回退到 5 点启发式 estimate_pose。
        self.headpose_session = None
        hp_path = models_dir / headpose_name
        if hp_path.is_file():
            self.headpose_session = ort.InferenceSession(str(hp_path), providers=providers)
            self.headpose_input = self.headpose_session.get_inputs()[0].name
            shape = self.headpose_session.get_inputs()[0].shape
            self.headpose_size = (int(shape[2]) if isinstance(shape[2], int) and shape[2] > 0
                                  else 224)

        # R2：CLIP 第二意见（可选——文件缺失时置 None，调用方降级为仅 genderage）
        self.clip_session = None
        self.clip_prompts = None
        clip_path = models_dir / clip_name
        prompts_path = models_dir / clip_prompts_name
        if clip_path.is_file() and prompts_path.is_file():
            self.clip_session = ort.InferenceSession(str(clip_path), providers=providers)
            z = np.load(prompts_path, allow_pickle=False)
            # 类别矩阵：gender=[female,male]，pose=[frontal,profile]，
            # occlusion=[clear_face,occluded_face]（文本向量已在共享空间）
            self.clip_prompts = {
                "gender": np.stack([z["female"], z["male"]]).astype(np.float32),
                "pose": np.stack([z["frontal"], z["profile"]]).astype(np.float32),
                "occlusion": np.stack([z["clear_face"], z["occluded_face"]]).astype(np.float32),
                "proj": z["visual_projection"].astype(np.float32),  # [512,768]
                "logit_scale": float(z["logit_scale"]),
                "mean": z["mean"].astype(np.float32),
                "std": z["std"].astype(np.float32),
            }

    # ---- 单模型接口 ----
    def detect(self, img_bgr: np.ndarray, det_thresh: float = 0.5) -> list[Detection]:
        return self.detector.detect(img_bgr, det_thresh=det_thresh)

    def align(self, img_bgr: np.ndarray, kps: np.ndarray) -> np.ndarray:
        return align_face(img_bgr, kps)

    def female_prob(self, aligned: np.ndarray) -> float:
        """genderage：输入 96×96、raw 0-255、RGB（探针实测：RGB 置信度 0.999，
        BGR 退化为 0.46 均匀输出；注意 aligned 为 BGR，需翻转）。
        输出 fc1=[g0, g1, age/100]，gender 0=女 1=男（lena 实测 index0=女 0.999）。"""
        x = cv2.resize(aligned, (96, 96)).astype(np.float32)
        blob = x[:, :, ::-1].transpose(2, 0, 1)[None].copy()  # BGR→RGB, NCHW
        g_in = self.gender_session.get_inputs()[0].name
        out = np.asarray(self.gender_session.run(None, {g_in: blob})[0]).reshape(-1)
        logits = out[:2]
        probs = softmax(logits)
        return float(probs[0])

    def embed(self, aligned: np.ndarray) -> np.ndarray:
        """MobileFaceNet：insightface ArcFace 标准预处理 (x-127.5)/127.5、RGB、112×112。"""
        x = (aligned[:, :, ::-1].astype(np.float32) - 127.5) / 127.5
        blob = x.transpose(2, 0, 1)[None].copy()
        e_in = self.emb_session.get_inputs()[0].name
        out = self.emb_session.run(None, {e_in: blob})[0]
        vec = np.asarray(out).reshape(-1).astype(np.float32)
        return vec / max(1e-9, float(np.linalg.norm(vec)))

    # ---- R2：CLIP 第二意见（性别 + 侧脸，零样本双任务）----

    @property
    def clip_ready(self) -> bool:
        return self.clip_session is not None

    # ---- R2：头部姿态模型（6DRepNet 系；yaw/pitch 度，低头为正）----

    @property
    def headpose_ready(self) -> bool:
        return self.headpose_session is not None

    def headpose(self, crop_bgr: np.ndarray) -> tuple[float, float]:
        """头部姿态模型 → (yaw_deg, pitch_deg)。输入 square_crop 产物（自动缩放到模型尺寸）。

        预处理：BGR→RGB、resize、/255、ImageNet mean/std（与上游 onnx_inference.py 一致）；
        输出 3×3 旋转矩阵 → 欧拉角（yaw 绕 y，pitch 绕 x，低头为正——实测标定）。
        """
        if not self.headpose_ready:
            raise RuntimeError("headpose model not available (headpose_resnet18.onnx)")
        size = self.headpose_size
        x = cv2.cvtColor(crop_bgr, cv2.COLOR_BGR2RGB)
        x = cv2.resize(x, (size, size)).astype(np.float32) / 255.0
        x = (x - np.array([0.485, 0.456, 0.406], np.float32)) \
            / np.array([0.229, 0.224, 0.225], np.float32)
        blob = x.transpose(2, 0, 1)[None]
        r = np.asarray(self.headpose_session.run(None, {self.headpose_input: blob})[0]) \
            .reshape(3, 3)
        sy = math.sqrt(r[0, 0] ** 2 + r[1, 0] ** 2)
        if sy < 1e-6:
            pitch = math.degrees(math.atan2(-r[1, 2], r[1, 1]))
            yaw = math.degrees(math.atan2(-r[2, 0], sy))
        else:
            pitch = math.degrees(math.atan2(r[2, 1], r[2, 2]))
            yaw = math.degrees(math.atan2(-r[2, 0], sy))
        return round(yaw, 2), round(pitch, 2)

    def clip_probs(self, crop_bgr: np.ndarray) -> tuple[float, float, float]:
        """CLIP 零样本 → (female_prob, profile_prob, occluded_prob)。

        预处理：RGB、/255、CLIP 官方 mean/std、224×224（输入须为 square_crop 产物）。
        ONNX 输出 pooler_output[768] → visual_projection[512,768] 投影至共享空间；
        文本向量已在导出期固化（scripts/export_clip_gender.py），运行时只跑视觉塔。
        实测（E01 + lena）：gender 分离完美；profile 对紧裁剪区分度有限（仅拦近背面，
        见 SOPHOS_CLIP_PROFILE_MAX）；occluded 误报高**不作判定依据**，仅记录审计。
        """
        if not self.clip_ready:
            raise RuntimeError("CLIP not available (missing clip_vitb32_face.onnx/prompts)")
        p = self.clip_prompts
        x = crop_bgr[:, :, ::-1].astype(np.float32) / 255.0
        x = (x - p["mean"]) / p["std"]
        blob = x.transpose(2, 0, 1)[None].copy()
        c_in = self.clip_session.get_inputs()[0].name
        pooled = np.asarray(self.clip_session.run(None, {c_in: blob})[0]).reshape(-1)
        img = p["proj"] @ pooled  # → 共享空间 512
        img = img / max(1e-9, float(np.linalg.norm(img)))

        def _pair(matrix: np.ndarray) -> float:
            sims = p["logit_scale"] * (matrix @ img)  # [2]
            e = np.exp(sims - sims.max())
            probs = e / e.sum()
            return float(probs[0])  # 第 0 类 = female / frontal / clear

        return (_pair(p["gender"]), _pair(p["pose"]),
                round(1.0 - _pair(p["occlusion"]), 4))  # 第三值 = 遮挡概率

    # ---- 组合流水线：单帧 → 候选样本（性别裁决上移至 identity 级，R1/ADR-014）----
    def process_frame(self, img_bgr: np.ndarray, timestamp_sec: float = 0.0,
                      det_thresh: float = 0.5, min_face: int = 80,
                      min_quality: float = 0.15,
                      max_yaw_deg: float = 80.0, max_pitch_deg: float = 40.0,
                      clip_profile_max: float = 0.95
                      ) -> list[FaceSample]:
        """单帧 → 通过样本级门控的候选样本（男女都保留，供聚类后 identity 级裁决）。

        门控顺序（先廉价后昂贵）：尺寸 → 截断(bbox+kps) → 质量 → 姿态（R2 模型优先，
        缺文件回退 5 点启发式）→ 对齐+推理。
        female_prob / occlusion_score / clip_* 只记录不裁决性别（P1/P2，R2）。
        """
        frame_h, frame_w = img_bgr.shape[:2]
        samples: list[FaceSample] = []
        for det in self.detect(img_bgr, det_thresh=det_thresh):
            x1, y1, x2, y2 = det.bbox
            if min(x2 - x1, y2 - y1) < min_face:
                continue
            if is_truncated(det.bbox, frame_w, frame_h) or kps_outside(det.kps, frame_w, frame_h):
                continue
            q = quality_score(img_bgr, det.bbox)
            if q < min_quality:
                continue
            if self.headpose_ready:
                yaw, pitch = self.headpose(square_crop(img_bgr, det.bbox))
            else:
                yaw, pitch, _ = estimate_pose(det.kps)
            pose_class = ("frontal" if abs(yaw) <= 15
                          else "near" if abs(yaw) <= 45 else "side")
            if abs(yaw) > max_yaw_deg or abs(pitch) > max_pitch_deg:
                continue
            aligned = align_face(img_bgr, det.kps)
            clip_fp = clip_pp = None
            occ = occlusion_score(aligned)
            if self.clip_ready:
                crop = square_crop(img_bgr, det.bbox)
                clip_fp, clip_pp, _clip_occ = self.clip_probs(crop)
                # 注：CLIP occluded 在紧裁剪上误报高（lena 实测 0.795），不作遮挡依据，
                # 仅记录返回值供审计；遮挡判定维持下半区纹理启发式（R2 结论）。
                if clip_pp > clip_profile_max:
                    continue  # R2：CLIP 判近背面剪影 → 剔除

            samples.append(FaceSample(
                bbox=det.bbox, det_score=det.det_score, quality_score=q,
                female_prob=self.female_prob(aligned),
                timestamp_sec=timestamp_sec,
                aligned=aligned, embedding=self.embed(aligned),
                pose_yaw=yaw, pose_pitch=pitch, pose_class=pose_class,
                occlusion_score=occ,
                clip_female_prob=clip_fp, clip_profile_prob=clip_pp))
        return samples


def softmax(x: np.ndarray) -> np.ndarray:
    e = np.exp(x - np.max(x))
    return e / e.sum()
