# Sophos

本地视频库的**女性面容识别 + 个性化颜值评分**工具。全部推理在本机完成，视频与图片不出本机。

一句话流程：

> 扫描视频目录 → 抽帧检测人脸 → 聚类成面容库 → 基础模型打分 → 你在 WebUI 上评分 → 训练出带个人偏好的打分器 → 生成滚动更新的「视频—分数对照表」，并可在线观看。

## 功能

- **多工作目录扫描**：可配置多个视频目录，增量扫描；只记录路径，不复制、不移动源文件。
- **女性面容库**：人脸检测（SCRFD / MTCNN）+ 特征提取（MobileFaceNet）；样本级质量门（清晰度 / 尺寸 / 曝光 / 对比度）、出画与遮挡判定、头部姿态剔除（近全侧脸不入库）；视频内聚类 + 多层同人合并；性别用 genderage 与 CLIP 零样本双模型一致同意裁决。
- **基础打分**：SCUT-FBP5500 系颜值回归模型，ONNX Runtime CPU 推理。
- **三种评分方式**：1–10 打分、好评 / 差评、两两对比（A/B 选更好，默认优先跨视频不同人物）。
- **接续训练**：把全库的评分与对比作为监督信号，训练个性化打分器并重新预测全库分数；训练版本可启用、可回退。
- **视频—分数对照表**：SQLite 存储，WebUI 内可排序、筛选、搜索。
- **在线观看**：按编码自动选择直出 / 重封装 / 转码三档，支持拖动进度，播放失败自动降级。
- **任务控制**：扫描与处理任务可暂停、取消、恢复，自动分批执行，相关参数可在 WebUI 运行时调整。
- **外部视频一次性分析**：把文件放进投放目录即可分析打分，不写入视频库与面容库。
- **访问口令**：设置一个密码即可保护整个 WebUI。
- **移动端**：响应式布局（桌面顶栏 / 手机底部导航），可作为 PWA 安装到主屏幕。
- **Docker 部署**：单容器一键启动，前端构建产物、后端与 ffmpeg 打包在一起。

## 快速开始（Docker）

```bash
git clone <本仓库地址>
cd sophos
cp .env.example .env        # 编辑 .env，把 SOPHOS_VIDEOS_HOST 指向你的视频目录

docker compose up -d --build
docker compose run --rm sophos python scripts/download_models.py   # 首次下载模型，约 80MB

# 浏览器打开 http://localhost:8000
```

首次进入后，在「任务与设置」页确认工作目录为 `/videos`（容器内路径），然后触发扫描与处理。

说明：

- **模型不打进镜像**，由 `./data` 卷提供，镜像重建不丢数据。
- `download_models.py` 能自动下载 insightface 三件套与头部姿态模型；`beauty_scut.onnx`、`clip_vitb32_face.onnx`、`clip_vitb32_prompts.npz` 是本项目的转换产物，需自行生成或复制到 `data/models/`，缺失时对应功能自动降级（详见 `docker/README.md`）。
- 容器内路径即数据库路径：`video.path` 存 `/videos/...`，更换宿主目录只需改 `.env` 映射，不用动数据库。

## 本地开发

```bash
# 后端
cd backend
python -m venv .venv && . .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000

# 前端（另开终端）
cd frontend
npm install
npm run dev            # 开发模式，/api 代理到 127.0.0.1:8000
npm run build          # 构建到 dist/，由后端静态托管
```

测试：

```bash
cd backend && python -m pytest
```

## 技术栈

| 层 | 选型 |
|---|---|
| 后端 | Python 3.10+ · FastAPI · Uvicorn（单进程 + 后台 worker 线程，无消息队列） |
| 数据库 | SQLite + SQLAlchemy 2.0 |
| 推理 | ONNX Runtime（CPU 优先） |
| 视觉模型 | SCRFD / MTCNN · genderage · MobileFaceNet · SCUT-FBP5500 系 · CLIP ViT-B/32 · 6DRepNet 系头部姿态 |
| 视频处理 | ffmpeg |
| 前端 | Vue 3 + Vite + Element Plus + ArtPlayer |
| 个性化训练 | numpy 手写联合线性打分头（绝对评分 MSE + 对比 pairwise） |
| 部署 | Docker / docker compose |

## 目录结构

```
sophos/
├── backend/            FastAPI 后端（app/api 路由层 · app/db 模型 · app/services 业务核心）
├── frontend/           Vue 3 前端（src/views 六个页签）
├── scripts/            辅助脚本（模型下载 / 备份 / 模型转换 / 压测工装）
├── docker/             Docker 部署说明
├── docs/               设计文档
├── Dockerfile
├── docker-compose.yml
└── .env.example
```

## 文档

| 文件 | 内容 |
|---|---|
| `docs/ARCHITECTURE.md` | 架构与模块设计 |
| `docs/DATA_MODEL.md` | 数据库表结构与状态机 |
| `docs/API_DESIGN.md` | REST API 设计 |
| `docs/DECISIONS.md` | 技术决策记录（ADR） |
| `docs/DEVELOPMENT_PLAN.md` | 分批次开发计划与进度 |
| `docs/HANDOFF.md` | 开发交接记录 |
| `docs/STRESS_LOG.md` | 大规模压测记录 |

## 声明

- 本项目用于本地个人视频库的整理与检索，请仅处理你有权处理的视频，并遵守你所在地区的法律法规。
- insightface 系模型为非商业研究许可；其余模型许可见 `docs/DECISIONS.md` 与 `data/models/README.md`。
