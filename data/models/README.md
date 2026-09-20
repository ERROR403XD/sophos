# 模型文件目录

运行时 ONNX 模型统一放这里（git 忽略）。`scripts/download_models.py` 自动下载；
也可手动下载 insightface 发布包后用 `--zip <path>` 解压。文件名如下（face_engine 按名加载，
可在 `.env` 中用 `SOPHOS_MODEL_DET` 等覆盖）。

| 文件 | 用途 | 来源 | 批次状态 |
|---|---|---|---|
| `det_500m.onnx` | 人脸检测（SCRFD-500M，buffalo_s 默认） | insightface buffalo_s | ✅ 已装（2026-09-16） |
| `w600k_mbf.onnx` | 特征 embedding（MobileFaceNet，512d） | insightface buffalo_s | ✅ 已装 |
| `genderage.onnx` | 性别/年龄（女性过滤；gender 0=女 1=男） | insightface buffalo_s | ✅ 已装 |
| `det_10g.onnx` | 人脸检测（SCRFD-10GF，更准，`--pkg buffalo_l` 可选） | insightface buffalo_l | 可选未装 |
| `beauty_scut.onnx` | 基础颜值分（ResNet-18 回归，1-5→0-100） | HF `Gustrd/SCUT-FBP5500-PyTorch-Model`（MIT）经 `scripts/convert_beauty_model.py` 导出 | ✅ 已装（2026-09-16，实测 lena raw≈3.59→64.7 分） |
| `clip_vitb32_face.onnx` + `clip_vitb32_prompts.npz` | R2 性别第二意见（CLIP ViT-B/32 零样本，性别+侧脸双任务） | HF `openai/clip-vit-base-patch32`（MIT）经 `scripts/export_clip_gender.py` 导出（临时依赖 torch+transformers） | ✅ 已装（2026-09-16；E01 审计 4 男性 leak 全部 clip_female≤0.07，13 女性 ≥0.63，完美分离） |
| `headpose_resnet18.onnx` | R2 头部姿态（6DRepNet 系 ResNet-18，yaw/pitch/roll；治 5 点启发式对近全侧脸的低估） | GitHub `yakhyo/head-pose-estimation` release `weights/resnet18.onnx`（MIT，300W-LP 训练） | ✅ 已装（2026-09-16；E01 目检对拍：真侧脸 -58~71° 精准，启发式曾估 40°） |
| `personalizer/v{n}.joblib` + `meta.json` | 个性化打分器（版本化） | 本项目训练产出 | M6 |

注意：
- insightface 系模型为**非商业研究许可**。
- 下载脚本国内网络优先走 curl（支持 HTTPS_PROXY）；手动下载地址：
  `https://github.com/deepinsight/insightface/releases/download/v0.7/buffalo_s.zip`
