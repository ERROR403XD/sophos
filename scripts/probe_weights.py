"""打印 checkpoint 各层张量形状，用于反推网络结构（M4 调试）。"""
import sys
from pathlib import Path

import torch

w = Path(sys.argv[1] if len(sys.argv) > 1 else r"tools\resnet18_py3.pth")
raw = torch.load(str(w), map_location="cpu", weights_only=False)
state = raw.get("state_dict", raw)
print("outer keys:", list(raw.keys()) if isinstance(raw, dict) else type(raw))
for k, v in state.items():
    print(f"{k:44s} {tuple(v.shape)}  {v.dtype}")
