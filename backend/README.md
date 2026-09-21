# Sophos 后端

FastAPI 单进程应用：API + 后台 worker（扫描/抽帧/人脸/打分/训练）。设计文档见 `../docs/`。

## 运行（开发模式）

```cmd
cd /d <project-root>\backend
<windows-user>\AppData\Local\Programs\Python\Python313\python.exe -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

- 交互式 API 文档：http://127.0.0.1:8000/docs
- 测试：`python -m pytest`（在 `backend/` 目录下执行）

## 结构

```
app/
├── main.py        # 入口：装配路由/DB/静态托管/worker（M2 起充实）
├── config.py      # pydantic-settings 配置（SOPHOS_ 前缀环境变量）
├── api/           # 路由层：workdirs/scan/videos/faces/ratings/train/stream
├── db/            # SQLAlchemy 模型（8 张表，见 docs/DATA_MODEL.md）与会话
└── services/      # 业务核心（每模块职责见 docs/ARCHITECTURE.md §2.1）
    scanner.py / frame_sampler.py / face_engine.py / clustering.py
    scorer.py / aggregator.py / personalizer.py / streamer.py / jobs.py
```

当前状态：**M5 已完成**——WebUI 四页签可用（评分/对比/视频库+播放/任务管理），后端全链路（扫描→人脸→聚类→颜值分→视频综合分→用户评分→对比）32 项测试全绿；`personalizer` 为 M6 占位。

启动：`uvicorn app.main:app --port 8000` 后浏览器开 `http://127.0.0.1:8000/`（自动托管 `frontend/dist`；dist 不存在则为纯 API 模式）。前端开发见 `../frontend/README.md`。

模型与工具已预置：`data/models/`（det_500m/genderage/w600k_mbf/beauty_scut，见其中 README）、`tools/ffmpeg-*/bin`、`tools/node-v22.23.2-win-x64/`。torch 仅转换颜值模型时需要，运行时不依赖。
