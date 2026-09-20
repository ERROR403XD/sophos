"""将 SCUT-FBP5500 预训练 ResNet-18 权重转换为 ONNX（M4；仅转换期需要 torch，运行时用 onnxruntime）。

权重来源：https://huggingface.co/Gustrd/SCUT-FBP5500-PyTorch-Model（MIT，resnet18_py3.pth，
torchvision resnet18(num_classes=1) 回归，SCUT-FBP5500 1-5 分制）。

用法（项目根）：
    python scripts/convert_beauty_model.py --weights tools/resnet18_py3.pth
输出：data/models/beauty_scut.onnx（输入 1x3x224x224，已按 ImageNet 均值方差归一化的 NCHW RGB）
并做 torch vs onnxruntime 数值一致性校验。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_WEIGHTS = PROJECT_ROOT / "tools" / "resnet18_py3.pth"
DEFAULT_OUT = PROJECT_ROOT / "data" / "models" / "beauty_scut.onnx"


def main() -> int:
    import numpy as np
    import onnxruntime as ort
    import torch
    import torchvision

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--weights", type=Path, default=DEFAULT_WEIGHTS)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = ap.parse_args()

    model = torchvision.models.resnet18(num_classes=1)
    # 该 checkpoint 是完整训练存档 {optimizer, epoch, state_dict, best_prec1}；
    # 张量形状与 torchvision ResNet-18 完全一致，仅模块命名不同（Torch7 转换遗留）：
    #   group1.conv1/bn1 -> conv1/bn1        layer*.B.group1.convN/bnN -> layer*.B.convN/bnN
    #   group2.fullyconnected -> fc          downsample 命名已一致
    raw = torch.load(str(args.weights), map_location="cpu", weights_only=False)
    state_raw = raw.get("state_dict", raw)
    state = {}
    for k, v in state_raw.items():
        if k.startswith("group1."):
            k = k[len("group1."):]
        elif k.startswith("group2.fullyconnected."):
            k = "fc." + k[len("group2.fullyconnected."):]
        else:
            k = k.replace(".group1.", ".")
        state[k] = v
    model.load_state_dict(state, strict=True)
    print("checkpoint epoch:", raw.get("epoch"), "best_prec1:", raw.get("best_prec1"))
    model.eval()
    print("state dict loaded:", len(state), "tensors")

    sample = torch.randn(1, 3, 224, 224)
    with torch.no_grad():
        ref = model(sample)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    # dynamo=False：旧版导出器，权重内联单文件（新版导出器会拆 .data 外部文件，
    # 部署时要带两个文件，不如单文件省心）
    torch.onnx.export(model, sample, str(args.out), opset_version=17,
                      input_names=["input"], output_names=["score"],
                      dynamo=False)
    print("exported:", args.out, f"{args.out.stat().st_size / 1e6:.1f} MB")

    sess = ort.InferenceSession(str(args.out), providers=["CPUExecutionProvider"])
    out = sess.run(None, {"input": sample.numpy()})[0]
    diff = float(np.abs(np.asarray(ref) - np.asarray(out)).max())
    print(f"parity check: torch={float(np.asarray(ref).reshape(-1)[0]):.6f} "
          f"onnx={float(np.asarray(out).reshape(-1)[0]):.6f} maxdiff={diff:.2e}")
    return 0 if diff < 1e-4 else 1


if __name__ == "__main__":
    sys.exit(main())
