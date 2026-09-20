"""R2：导出 CLIP 视觉塔 ONNX + 固化 prompt 文本向量（性别 + 侧脸双任务零样本）。

一次性转换脚本（依赖 torch/transformers，运行时只需 onnxruntime，同 M4 beauty 模型模式）：
  python scripts/export_clip_gender.py

产出（data/models/）：
  clip_vitb32_face.onnx    视觉塔：pixel_values[N,3,224,224] → pooler_output[N,768]
  clip_vitb32_prompts.npz  各类别平均文本向量（已投影至共享空间、L2 归一）+
                           visual_projection[512,768]（运行时 pooler @ W.T）+ logit_scale + mean/std

预处理（推理侧，须与 CLIPProcessor 一致）：RGB、/255、mean/std(CLIP 官方)、224×224。
人脸输入约定：bbox 外扩方形裁剪（不足处补边）→ 直接 resize 224（保整脸，不走中心裁剪）。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import numpy as np
import torch
from transformers import CLIPModel, CLIPTokenizerFast

MODELS = ROOT / "data" / "models"
HF_ID = "openai/clip-vit-base-patch32"

# prompt 组（组内取平均再做类别间 softmax；参照 CLIP 零样本分类 ensemble 惯例）
PROMPTS = {
    "female": ["a photo of a woman", "a photo of a female person",
               "a face of a woman", "a woman looking at the camera"],
    "male": ["a photo of a man", "a photo of a male person",
             "a face of a man", "a man looking at the camera"],
    "frontal": ["a front view photo of a person's face",
                "a face looking straight at the camera",
                "a person facing the camera"],
    "profile": ["a side profile view of a person's face",
                "a person looking away to the side",
                "a profile silhouette of a face"],
    "clear_face": ["a photo of a clearly visible face",
                   "a person's face with nothing covering it",
                   "a fully visible unobstructed face"],
    "occluded_face": ["a face partially covered by an object",
                      "a person's face blocked by something",
                      "a face hidden behind an object"],
}

# CLIPProcessor 官方归一化
MEAN = np.array([0.48145466, 0.4578275, 0.40821073], dtype=np.float32)
STD = np.array([0.26862954, 0.26130258, 0.27577711], dtype=np.float32)


def main() -> None:
    print(f"loading {HF_ID} ...")
    model = CLIPModel.from_pretrained(HF_ID)
    tokenizer = CLIPTokenizerFast.from_pretrained(HF_ID)
    model.eval()

    # ---- 1) 视觉塔 ONNX（trace vision_model 本体，输出 pooler_output[768]；
    #      投影矩阵单独固化——get_image_features 的 kwargs 转发在 trace 下不稳）----
    class VisionWrap(torch.nn.Module):
        def __init__(self, vm):
            super().__init__()
            self.vm = vm

        def forward(self, pixel_values):
            return self.vm(pixel_values).pooler_output

    dummy = torch.zeros(1, 3, 224, 224)
    out_path = MODELS / "clip_vitb32_face.onnx"
    print("exporting vision tower ->", out_path)
    with torch.no_grad():
        torch.onnx.export(
            VisionWrap(model.vision_model).eval(), dummy, str(out_path),
            input_names=["pixel_values"], output_names=["pooler_output"],
            dynamic_axes={"pixel_values": {0: "N"}, "pooler_output": {0: "N"}},
            opset_version=17, dynamo=False)  # dynamo=False：单文件导出（M4 教训）
    proj = model.visual_projection.weight.detach().numpy()  # [512,768]

    # ---- 2) 文本向量固化（transformers 5.x 的 get_text_features 返回类型有变，
    #      直接走 text_model → pooler → text_projection 手动链路）----
    embeds = {}
    for cls, prompts in PROMPTS.items():
        toks = tokenizer(prompts, padding=True, return_tensors="pt")
        with torch.no_grad():
            pooled = model.text_model(**toks).pooler_output
            feats = model.text_projection(pooled)  # [P,512] 共享空间
        feats = feats / feats.norm(dim=-1, keepdim=True)
        mean = feats.mean(dim=0)
        embeds[cls] = (mean / mean.norm()).numpy()
        print(f"{cls}: {len(prompts)} prompts -> {embeds[cls].shape}")

    logit_scale = float(model.logit_scale.exp().item())
    np.savez(MODELS / "clip_vitb32_prompts.npz",
             **embeds, visual_projection=proj.astype(np.float32),
             logit_scale=np.float32(logit_scale),
             mean=MEAN, std=STD,
             prompt_json=np.array(str(PROMPTS)))  # 便于审计
    print(f"logit_scale={logit_scale:.1f}")
    print("done:", out_path, MODELS / "clip_vitb32_prompts.npz")


if __name__ == "__main__":
    main()
