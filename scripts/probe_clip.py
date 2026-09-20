"""R2 探针：CLIP 双任务（性别/侧脸）在 E01 rep 裁剪上的表现 vs 目检标签。

目检标签（2026-09-16 审计）：男 = id03/06/13/17，其余女。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from app.services.face_engine import FaceEngine  # noqa: E402

MALE = {3, 6, 13, 17}
REVIEW = ROOT / "data" / "tmp" / "review_e01"

engine = FaceEngine(models_dir=ROOT / "data" / "models")
assert engine.clip_ready, "CLIP not exported"

def square_pad(img: np.ndarray) -> np.ndarray:
    h, w = img.shape[:2]
    side = max(h, w)
    canvas = np.zeros((side, side, 3), dtype=img.dtype)
    canvas[(side - h) // 2:(side - h) // 2 + h, (side - w) // 2:(side - w) // 2 + w] = img
    return canvas

print(f"{'id':>4s} {'truth':>6s} {'clip_f':>7s} {'clip_prof':>9s} {'clip_occ':>9s}  file")
errors = []
for p in sorted(REVIEW.glob("rep_*.jpg")):
    rid = int(p.stem.split("_")[1])
    data = np.fromfile(str(p), dtype=np.uint8)
    img = cv2.imdecode(data, cv2.IMREAD_COLOR)
    fp, pp, occ = engine.clip_probs(square_pad(cv2.resize(img, (224, 224))))
    truth = "male" if rid in MALE else "female"
    mark = ""
    if truth == "male" and fp >= 0.5:
        mark = "  <-- MISS(male as female)"
        errors.append((rid, "gender"))
    if truth == "female" and pp > 0.6:
        mark += "  (side female would be cut)"
    print(f"{rid:4d} {truth:>6s} {fp:7.3f} {pp:9.3f} {occ:9.3f}  {p.stem[:36]}{mark}")

print(f"\nerrors: {len(errors)}")
