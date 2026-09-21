# Docker 部署（M7）

产物：根目录 `Dockerfile`（多阶段：node 构建前端 → python:3.12-slim + ffmpeg 运行时）、
`docker-compose.yml`、`.env.example`、`.dockerignore`。

## 一键部署

```cmd
:: 0) 前置：安装 Docker Desktop（含 WSL2），确认 docker --version 可用
cd /d <project-root>
copy .env.example .env
:: 编辑 .env：SOPHOS_VIDEOS_HOST 指向宿主视频目录（如 E:\videos）

docker compose up -d --build

:: 首次：下载模型（约 80MB：insightface 三件套 + R2 头部姿态模型，进 data/models 卷，只需一次）
docker compose run --rm sophos python scripts/download_models.py

:: 打开 http://localhost:8000
```

## 关键约定（Windows 宿主必读）

- **容器内路径即数据库路径**：`video.path` 存容器内路径（`/videos/...`）。添加工作目录时一律填 `/videos`；换宿主目录只改 `.env` 映射，不动数据库。
- **模型不打进镜像**：体积与非商业许可原因，`./data` 卷挂载提供；镜像重建不丢数据。
  - `download_models.py` 可自动下载：det_500m / genderage / w600k_mbf（insightface）+ headpose_resnet18（R2 姿态模型，缺它姿态回退启发式）。
  - **无法下载、需从开发机复制**（转换产物，缺失会降级）：`beauty_scut.onnx`（缺→颜值分跳过）、
    `clip_vitb32_face.onnx` + `clip_vitb32_prompts.npz`（缺→性别门控降级为仅 genderage，R2 双票失效）。
    把开发机 `data/models/` 下这三个文件拷到 Docker 宿主 compose 目录的 `data/models/` 即可。
  - 验证：`docker compose run --rm sophos ls /app/data/models`
- **视频只读挂载**：`/videos:ro`，系统只维护路径、不复制文件。
- 数据库备份：`docker compose exec sophos python scripts/backup_db.py`（或宿主机直接跑 `python scripts/backup_db.py`），输出到 `data/backups/`。

## 当前状态（2026-09-16）

- ✅ Dockerfile / compose / .env / .dockerignore / 备份脚本已写好并通过静态审查
- ⏸️ **容器实测待办**：本机未安装 Docker（安装需 WSL2 + 系统级变更，留待用户执行）。
  在有 Docker 的机器上执行"一键部署"步骤并走一遍 WebUI 全流程即为 M7 验收完成；
  如遇 ffmpeg 缺层/路径映射问题，按 README 排查或回写本文件。
