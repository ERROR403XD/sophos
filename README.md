# Sophos — 视频面容评分系统

基于**本地视频库**的女性面容识别与个性化颜值评分系统：

> 扫描工作目录中的视频 → 抽取女性人脸形成面容库 → 基础模型打分 → 用户在 WebUI 上评分（1-10 打分 / 好评·差评 / 两两对比）→ 接续训练出"带用户倾向"的打分器 → 生成滚动更新的**视频-分数对照表**，并支持在线观看视频。

> **当前状态**：M1-M7 + R1（v1.0.1）+ R2（v1.0.2）+ R3（v1.0.3）+ R4（v1.0.4）+ R5（**v1.1.0**）+ R6（**v1.2.0**，2026-09-18：访问口令 + 移动端响应式重构 + 播放自动降级 + 转码并发治理 + 面容合并第三层/性别门保底 + 训练语义澄清）完成。
> 接手开发前请务必先读 [docs/HANDOFF.md](docs/HANDOFF.md) 与 [docs/DEVELOPMENT_PLAN.md](docs/DEVELOPMENT_PLAN.md)。

## 功能特性

- 🔐 **访问口令**（R6）：`SOPHOS_ACCESS_PASSWORD` 一个密码保护整个 WebUI（容器建立时经 .env/compose 配置，留空 = 不启用）；Cookie 会话 30 天，改密码即全体旧会话失效
- 📱 **移动端响应式 UI**（R6）：≤768px 切换为顶栏 + 底部导航外壳；视频库/任务页自动变卡片列表；播放器弹窗全屏（ArtPlayer 手势/全屏/画中画）
- 📲 **PWA**（R6.1）：可安装到主屏幕（独立窗口、项目图标）、静态资源本地缓存（视频流/缩略图不经 SW，仍走服务端缓存头）；Service Worker 仅在 localhost/https 生效（平台限制）
- 📁 **多工作目录扫描**：支持配置一个或多个视频目录，增量扫描，只维护视频路径（不复制、不移动文件）；扫描期 ffprobe 探测视频编码入库
- 👩 **女性面容库**：先聚类、identity 级**双模型一致同意**裁决性别（genderage + CLIP 零样本第二意见，治单模型高置信误判）；样本级质量门 + 截断/关键点出画剔除 + **头部姿态模型**（6DRepNet 系，侧脸 rep 不入评分库）+ 遮挡提示；同视频内聚类 + **三层合并 pass**（高置信直接 / 中等相似谨慎 / **单样本同人**，R6/ADR-028）+ **同帧共现一票否决**（R4/ADR-018）防同人碎裂；**性别门保底**：有女性出镜的视频不再因门控边际误判颗粒无收（宁缺毋滥：只保 1 张最优，不凑数）
- 🎭 **遮挡面容三规则**（R1）：含遮挡（面具等）的面容保留可评分（偏好信号），特征只用干净样本计算，全遮挡面容不进个性化训练
- 🎯 **基础打分**：基于 SCUT-FBP5500 系颜值回归模型，ONNX 统一推理（检测用 SCRFD/MTCNN，特征用 MobileFaceNet）
- 🖱️ **用户评分**：WebUI 提供三种评分方式——1-10 打分、好评/差评、两两对比；评分页可显示性别置信度、标记遮挡
- ⚖️ **对比评分**：两个面容 A/B 并排，选更好的一张；R5 起默认 **diverse 层级（ADR-023，修订 ADR-019）**：跨视频不同人物 > 同视频不同人物（同帧共现）> 跨视频可能同人 > 同视频证据不足
- 🧠 **接续训练**：基于**全库用户的评分/对比**作为监督信号调整打分模型参数，再用新模型对全库**预测**个性化分数（不是对指定视频的面容分数直接改写/规范化）；R6 在训练页明确该语义
- 📊 **视频-分数对照表**：存于 SQLite，滚动更新，WebUI 中可排序/筛选/搜索/**按库筛选**（R1）查看
- ▶️ **在线观看**（R1 三档，ADR-015）：直出（真 Range 可拖动）/ 重封装（h264+mkv 秒开）/ 转码（hevc 等现场转 fMP4），按**文件头实测容器**自动选择（R9：扩展名会骗人——".mp4" 实为 TS 也走重封装，不再误判直出）；R5 起 remux/转码档支持**服务端 seek**（`?ss=`，ADR-021）；R6 起 direct/remux 播放失败**自动降级转码**（`?fallback=1`，ADR-027），错误按类型提示；R9 转码输出**分辨率封顶 1080p**（4K 源实时转码会打满 CPU，`SOPHOS_TRANSCODE_MAX_HEIGHT=0` 可关）
- ⏯️ **任务控制**（R5，ADR-022）：任务可暂停/取消/恢复（协作式中断，视频边界/逐帧检查点）；人脸处理**自动分批链**（每批大小可调，批间其他任务自然插队）；运行时设置（分批大小 / 每人面容上限 / 自动训练阈值 / 自动扫描·自动处理面容联动）WebUI 即改即生效
- 🔍 **外部视频一次性分析**（R6.3）：上传或拷贝到投放目录（data/inbox/）→ 面容处理+打分 → 综合分与面容卡，**不进自管视频库/面容库**；大文件建议走投放目录（Docker 即挂载卷）
- 👤 **同人面容上限**（R5，ADR-023）：同一视频同一人最多保留 5 张面容（可调，0=不限）——按"可能同人组件"独立封顶，共现否决 + 相似度下限护栏，宁超限不误并
- 📦 **大规模优化**（R5，ADR-024）：面向数万视频——缩略图分片存储 + immutable 缓存 + 评分页预取、复合索引、pair 选对抽样化、训练/打分应用分块化（R6：分块提交间歇让读，训练/应用模型不再挤占在线浏览）
- 🐳 **Docker 部署**：单容器一键启动（容器实测待宿主 Docker）

## 架构总览

```
┌──────────────────────────────────────────────────────────────────────┐
│                        工作目录（可配置多个）                          │
│            E:\videos    F:\clips    /media/videos ...                │
└──────────────────────────────┬───────────────────────────────────────┘
                               │ ① 增量扫描 scanner（只记路径）
                               ▼
                        video 表 (pending)
                               │ ② 抽帧 frame_sampler (ffmpeg)
                               ▼
   face_engine：③ 人脸检测(SCRFD/MTCNN) → 样本级门控(尺寸/截断/质量/姿态)
               → 特征 embedding(MobileFaceNet) + 记录性别/遮挡提示
                               │
                               ▼ ④ 视频内聚类 → identity 级性别裁决 → 合并 pass (R1)
                    face_identity（面容库，一视频多面容）
                               │
              ┌────────────────┼─────────────────────┐
              ▼                ▼                     ▼
        缩略图入库        ⑤ 基础打分 scorer        ⑦ 用户评分 user_rating
                       (SCUT-FBP5500)          (1-10 / 好评·差评, WebUI)
                               │                     │
                               │                     ▼
                               │            ⑧ 接续训练 personalizer
                               │            (个性化打分器, 版本化)
                               ▼                     │
                        ⑥ aggregator 聚合 ◄──────────┘
                       video_score（滚动更新）
                               │
                               ▼
        WebUI：视频-分数对照表 + 在线观看(Range 流式) + 评分页 + 任务页
```

## 技术栈

| 层 | 选型 | 说明 |
|---|---|---|
| 后端 | Python 3.10+ · FastAPI · Uvicorn | 单进程 + 后台 worker 线程，无需消息队列 |
| 数据库 | SQLite + SQLAlchemy 2.0 | 单机零运维，预留迁移 PostgreSQL 的可能 |
| 推理 | ONNX Runtime（CPU 优先，预留 GPU） | 所有视觉模型统一 ONNX 格式 |
| 视觉模型 | SCRFD（或 MTCNN 备选）· genderage · MobileFaceNet · SCUT-FBP5500 系 | 详见 `docs/DECISIONS.md` ADR-004~006 |
| 视频处理 | ffmpeg | 抽帧；容器镜像内置 |
| 前端 | Vue 3 + Vite + Element Plus | 构建产物由 FastAPI 静态托管（单容器） |
| 个性化训练 | numpy 手写联合线性打分头（ADR-008） | 绝对评分 MSE + 对比 pairwise 联合训练；后续可选 PyTorch 微调 |
| 部署 | Docker / docker compose | M7 落地 |

## 目录结构

```
<project-root>\
├── README.md                 ← 本文件
├── docs\
│   ├── ARCHITECTURE.md       ← 架构与模块设计（必读）
│   ├── DATA_MODEL.md         ← 数据库表结构与状态机（必读）
│   ├── API_DESIGN.md         ← REST API 设计
│   ├── DEVELOPMENT_PLAN.md   ← 分批次开发计划 + 进度表（必读）
│   ├── HANDOFF.md            ← 交接文档（每批次结束必须更新）
│   └── DECISIONS.md          ← 技术决策记录（ADR）
├── backend\                  ← FastAPI 后端（M2 起实现）
│   ├── app\main.py · config.py
│   ├── app\api\              ← 路由层
│   ├── app\db\               ← SQLAlchemy 模型与会话
│   ├── app\services\         ← 核心业务：scanner / face_engine / scorer / personalizer ...
│   └── tests\                ← pytest
├── frontend\                 ← Vue3 WebUI（M5 起实现）
├── docker\                   ← Dockerfile / compose（M7 落地）
├── scripts\                  ← 模型下载、备份等辅助脚本
└── data\                     ← 运行时数据（git 忽略）：db / thumbs / models / logs
    └── models\               ← ONNX 模型文件放置处（见其中 README）
```

## 开发批次与当前进度（速览）

| 批次 | 内容 | 状态 |
|---|---|---|
| M1 | 文档、目录骨架、README、进度表 | ✅ 完成（2026-09-16） |
| M2 | 后端核心骨架：配置/DB/视频扫描/视频表/在线播放 | ✅ 完成（2026-09-16） |
| M3 | 人脸引擎：抽帧/检测/性别/特征/聚类/面容库 | ✅ 完成（2026-09-16） |
| M4 | 基础打分 + 视频综合分（滚动更新） | ✅ 完成（2026-09-16） |
| M5 | WebUI 第一版（评分页/对比/视频表/播放/任务页） | ✅ 完成（2026-09-16） |
| M6 | 用户偏好接续训练 | ✅ 完成（2026-09-16） |
| M7 | Docker 部署与端到端收尾 | 🚧 产物就绪，容器实测待宿主 Docker |
| R1 | v1.0.1 修复批次（性别裁决/质量姿态遮挡/同人合并/播放三档/按库筛选） | ✅ 完成（2026-09-16，tag v1.0.1；[docs/PLAN_v1.0.1.md](docs/PLAN_v1.0.1.md)） |
| R2 | v1.0.2 强化批次（CLIP 性别第二意见/头部姿态模型/侧脸小脸治理） | ✅ 完成（2026-09-17，tag v1.0.2；见 HANDOFF R2 节与 ADR-016） |
| R3 | v1.0.3 批次（缩略图人像取景/质量门 v3） | ✅ 完成（2026-09-17，tag v1.0.3；见 HANDOFF R3 节与 ADR-017） |
| R4 | v1.0.4 批次（视频内双层 merge+同帧共现否决/diverse 人物优先对比/播放 404 修复） | ✅ 完成（2026-09-17，tag v1.0.4；[docs/PLAN_v1.0.4.md](docs/PLAN_v1.0.4.md) 与 ADR-018/019/020） |
| R5 | v1.1.0 批次（播放 seek/任务暂停取消/分批链/面容上限 5/对比层级重排/大规模优化） | ✅ 完成（2026-09-17；[docs/PLAN_v1.1.0.md](docs/PLAN_v1.1.0.md) 与 ADR-021/022/023/024） |
| R5.1/R5.2 | 压测 + 播放器统一（ArtPlayer/移动端全屏/部署形态适配/WAL 开关） | ✅ 完成（2026-09-18；STRESS_LOG 与 ADR-025） |
| R6 | v1.2.0 批次（访问口令/移动端响应式重构/播放自动降级/转码并发治理/面容合并第三层+性别门保底/训练语义澄清） | ✅ 完成（2026-09-18；[docs/PLAN_v1.2.0.md](docs/PLAN_v1.2.0.md) 与 ADR-026/027/028） |
| R7/R8 | 移动端播放（HLS 会话传输/转码兼容性定稿/播放器全屏·滚动·返回键） | ✅ 完成（2026-09-20；ADR-029） |
| R9 | 容器实测分档 + 播放链路 CPU/线程治理（TS 冒名 .mp4 修复/转码 1080p 封顶/HLS 会话即时回收） | ✅ 完成（2026-09-21；ADR-030） |

详细任务清单、验收标准与风险见 **[docs/DEVELOPMENT_PLAN.md](docs/DEVELOPMENT_PLAN.md)**。

## 快速开始

### Docker 一键部署（推荐，见 [docker/README.md](docker/README.md)）

```cmd
cd /d <project-root>
copy .env.example .env        :: 编辑 .env 设置宿主视频目录 SOPHOS_VIDEOS_HOST；建议同时设置访问口令 SOPHOS_ACCESS_PASSWORD
docker compose up -d --build
docker compose run --rm sophos python scripts/download_models.py   :: 首次拉模型
:: 打开 http://localhost:8000 （设置了访问口令时先输入密码登录）
```

### 开发模式

```cmd
cd /d <project-root>\backend
.venv\Scripts\python.exe -m uvicorn app.main:app --port 8000
:: 打开浏览器访问 http://127.0.0.1:8000/ 即是 WebUI
:: API 文档: http://127.0.0.1:8000/docs    测试: .venv\Scripts\python.exe -m pytest
```

> 数据目录约定：模型在**项目根** `data\models\`，而默认 `./data` 相对启动目录解析。
> `backend\.env` 已设 `SOPHOS_DATA_DIR=<project-root>/data` 指向项目根（勿删）；换机器部署时改为对应绝对路径。

WebUI 六个页签（桌面端顶部页签 / 移动端底部导航）：**评分**（1-10 + 👍👎，自动下一张）、**对比**（A/B 选更好一张）、**视频库**（分数表 + 在线播放）、**分析**（外部视频一次性分析打分，不入库）、**训练**（个性化打分器训练/启用/回退）、**任务与设置**（目录管理 + 扫描/处理 + 任务暂停/取消/恢复 + 分批/上限/自动训练阈值/自动扫描·处理设置）。设置 `SOPHOS_ACCESS_PASSWORD` 后首次访问会弹出登录层。前端开发模式：`cd frontend && set PATH=<project-root>\tools\node-v22.23.2-win-x64;%PATH% && npm run dev`（需先 `npm install`）。

本机开发已预置：ffmpeg（`tools/ffmpeg-*/bin`，自动定位）、模型四件套（`data/models/`）、node 便携版、MinGit（`tools/cmd/git.exe`）。

## 文档索引

| 文档 | 用途 |
|---|---|
| [ARCHITECTURE.md](docs/ARCHITECTURE.md) | 模块划分、数据流、关键设计（幂等/滚动更新/流式播放/训练闭环） |
| [DATA_MODEL.md](docs/DATA_MODEL.md) | 全部表结构、索引、状态机、保留与清理策略 |
| [API_DESIGN.md](docs/API_DESIGN.md) | API 约定与逐端点定义 |
| [DEVELOPMENT_PLAN.md](docs/DEVELOPMENT_PLAN.md) | 批次划分、任务清单、验收标准、风险表 |
| [PLAN_v1.0.1.md](docs/PLAN_v1.0.1.md) | R1 修复批次实施方案（用户反馈 5 项：性别/质量/去重/播放/库筛选） |
| [PLAN_v1.0.4.md](docs/PLAN_v1.0.4.md) | R4 实施方案（双层 merge / diverse 人物优先对比 / 播放 404 根因与 stream_url 契约） |
| [PLAN_v1.1.0.md](docs/PLAN_v1.1.0.md) | R5 实施方案（播放 seek / 任务控制与分批链 / 面容上限 / 大规模优化） |
| [PLAN_v1.2.0.md](docs/PLAN_v1.2.0.md) | R6 实施方案（访问口令 / 移动端响应式 / 播放自动降级 / 面容第三层合并与保底 / 训练语义澄清） |
| [STRESS_LOG.md](docs/STRESS_LOG.md) | 压测日志（30k 规模基准与修复记录；工装 `scripts/stress/`，测试库 `<stress-data>\`） |
| [HANDOFF.md](docs/HANDOFF.md) | **每批次交接必读必写**：当前状态、下一步入口 |
| [DECISIONS.md](docs/DECISIONS.md) | 已定/待定的技术决策及理由 |

## 合规与隐私

- 所有视频与人脸数据**仅在本地处理**，不向任何外部服务上传。
- 所用开源模型多为**研究用途许可**（如 InsightFace 系列为非商业许可），本项目定位为个人本地工具，请勿用于商业用途。
