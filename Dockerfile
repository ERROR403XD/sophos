# Sophos 多阶段构建（M7）
# 构建：docker build -t sophos .
# 说明：ONNX 模型不打入镜像（体积+非商业许可），通过 ./data 卷挂载提供；
#       首次部署用 docker compose run --rm sophos python scripts/download_models.py 拉取。

# ---- 阶段 1：前端构建 ----
FROM node:22-alpine AS frontend
WORKDIR /app
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-fund --no-audit
COPY frontend/ ./
RUN npm run build

# ---- 阶段 2：运行时 ----
FROM python:3.12-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY backend/requirements.txt ./backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

COPY backend/ ./backend/
COPY scripts/ ./scripts/
COPY --from=frontend /app/dist ./frontend/dist

ENV SOPHOS_DATA_DIR=/app/data \
    PYTHONUNBUFFERED=1
EXPOSE 8000

WORKDIR /app/backend
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
