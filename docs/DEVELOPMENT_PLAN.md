# 开发计划与进度表（DEVELOPMENT_PLAN）

> 本项目分多个批次完成。**每个批次结束时必须**：勾选本表 → 在 `HANDOFF.md` 顶部新增交接记录 → 更新 `README.md` 批次状态表。
> 图例：⬜ 未开始 · 🚧 进行中 · ✅ 完成 · ⏸️ 阻塞/搁置

## 进度总览

| 批次 | 主题 | 状态 | 完成日期 |
|---|---|---|---|
| M1 | 文档、目录骨架、README、进度表 | ✅ | 2026-09-16 |
| M2 | 后端核心骨架：配置 / DB / 扫描 / 视频表 / 在线播放 | ✅ | 2026-09-16 |
| M3 | 人脸引擎：抽帧 / 检测 / 性别 / 特征 / 聚类 / 面容库 | ✅ | 2026-09-16 |
| M4 | 基础打分 + 视频综合分（滚动更新） | ✅ | 2026-09-16 |
| M5 | WebUI 第一版 | ✅ | 2026-09-16 |
| M6 | 用户偏好接续训练 | ✅ | 2026-09-16 |
| M7 | Docker 部署与端到端收尾 | 🚧 产物就绪，容器实测待宿主 Docker | 2026-09-16 |
| R1 | v1.0.1 修复批次：性别裁决 / 质量姿态遮挡 / 同人合并 / 播放三档 / 按库筛选 | ✅ 完成（2026-09-16，tag v1.0.1）；真实片源人工抽检为后续用户验收项 | 2026-09-16 |
| R2 | v1.0.2 强化批次：CLIP 性别第二意见 + 头部姿态模型 + 侧脸/小脸治理 | ✅ 完成（2026-09-17，tag v1.0.2）；待用户复检 | 2026-09-17 |
| R3 | v1.0.3 批次：缩略图人像取景（头顶+20%/两侧+20%/向下+80%）+ 质量门 v3（曝光/对比度/重定标） | ✅ 完成（2026-09-17，tag v1.0.3）；待用户复检 | 2026-09-17 |
| R4 | v1.0.4 批次：视频内双层 merge（同帧共现否决）+ diverse 人物优先对比 + 播放 404 根因修复（stream_url 契约/错误分型） | ✅ 完成（2026-09-17，tag v1.0.4）；待用户复检（重处理 + 对比页/播放页） | 2026-09-17 |
| R15 | v1.9.0 批次：任务列表活动优先排序 + 视频库整体面容评价 + 提取面容逐个评分抽屉 | ✅ 完成（源码就绪、未部署；待用户浏览器验收与数据落库确认） | 2026-10-08 |
| R16 | v1.9.1 批次：快速拖动/转码取消治理 + 真实时长 + 查询复合索引 + 面容相位补采样 | ✅ 已部署 + 技术压测完成（待用户浏览器验收） | 2026-10-08 |
| R17 | v1.9.2 批次：移动端返回层级 + 评分/对比页视频面容入口 + 指定重提面容 + 抽帧低优先级 | ✅ 源码完成（待部署） | 2026-10-08 |
| R18 | v1.10.0 批次：独立设置页 + 三档性别策略 + 全库分批重建面容 | ✅ 源码完成（待部署） | 2026-10-08 |

版本号约定：M1→v0.1.x，M2→v0.2.x … M7→v1.0.0；修复批次 R1→v1.0.1。

---

## M1 — 文档与工程骨架 ✅（2026-09-16）

- [x] README、架构、数据模型、API 设计、本计划、HANDOFF、DECISIONS 七份文档
- [x] 目录骨架 + 后端 stub（可 import、`/api/health` 可用）+ `.gitignore`
- [x] 风险清单与决策记录初版

出口条件：文档齐备、结构可续建、HANDOFF 指明 M2 入口。✅

## M2 — 后端核心骨架 ✅（2026-09-16）

**目标**：不依赖任何视觉模型，先把"目录 → 视频表 → 在线播放"跑通。

- [x] 环境：venv 已建（宿主 Python 3.13）；M2 依赖已装
- [x] `app/config.py`：pydantic-settings，`SOPHOS_` 前缀环境变量 + `.env`
- [x] `app/db/`：SQLAlchemy engine/session + 九张表建齐（含新增 `pair_comparison`，ADR-013）
- [x] `app/services/scanner.py`：递归扫描 + 扩展名过滤 + 增量（新增/变更→pending、消失→missing、复现→恢复）
- [x] `app/services/jobs.py`：后台 worker 线程 + job 表 + 进度/结果上报 + 启动清理遗留任务
- [x] API：health / workdirs / scan-start / jobs / videos（分页/搜索/排序）/ videos/{id} / stream（Range 206）
- [x] `tests/`：扫描增量×2、workdirs、扫描任务端到端、409 防重、视频列表/详情、Range/415/404、health —— 9 项全绿
- [ ] （建议）宿主机安装 git 并完成首次提交（tag v0.2.0）—— git 未安装，遗留

**验收说明**：合成文件 + Range 测试已覆盖验收语义（宿主机暂无 ffmpeg 与真实样片）；真实编码视频的全链路播放验证并入 M3 入口。

## M3 — 人脸引擎与面容库 ✅（2026-09-16）

**目标**：视频 → 女性面容库（identity + 缩略图）全链路。

- [x] `scripts/download_models.py`：模型下载（curl 优先，支持 `--zip` 本地包）；已装 buffalo_s 三件套（det_500m / genderage / w600k_mbf）
- [x] `frame_sampler.py`：ffmpeg 按间隔抽帧（ffmpeg 绿色版置于 `tools/`，自动定位 PATH > tools/ffmpeg*/bin）
- [x] `face_engine.py`：SCRFD 解码（修复 anchor 堆叠轴序 bug，探针逐位比对定位）→ 5 点对齐 112×112 → genderage（**实测定稿：96×96、raw 0-255、RGB，gender 0=女**）→ MobileFaceNet（(x-127.5)/127.5 RGB）→ 质量过滤；`scripts/probe_models.py` 等 3 个探针脚本保留备用
- [x] `clustering.py`：Chinese Whispers 余弦阈值聚类 → face_identity + 代表帧缩略图
- [x] `process` 流水线接入 jobs：幂等（重跑先级联删；存在用户评分则拒绝重处理）、单视频失败不阻塞整批、抽帧目录即处理即清理
- [x] API：process-start / faces 列表与详情 / thumbs 静态托管（Unicode 安全读写）
- [x] `tests/`：抽帧×3、引擎 lena 实测×5、聚类×3、流水线端到端×3（含 lena 真人视频出库、无人脸视频、模型缺失明确报错）——合计 **23 passed**

**验收说明**：lena 真人视频全链路验证通过（检测 score 0.81、female_prob 0.999、产出 identity+缩略图）；多人视频聚类效果待用户真实片源入库后复核（M4 人工抽检一并做）。

## M4 — 基础打分与视频综合分 ✅（2026-09-16）

- [x] `scorer.py`：接入 SCUT-FBP5500 预训练 ResNet-18（**权重换源定稿**：HF `Gustrd/SCUT-FBP5500-PyTorch-Model` MIT 权重 → `scripts/convert_beauty_model.py` 键名映射（Torch7 转换遗留命名 group1/group2.fullyconnected）→ `beauty_scut.onnx` 单文件 44.7MB，torch/ort 数值一致）
- [x] 打分合理性实测（`scripts/probe_beauty.py`）：lena 人脸区域裁剪 raw≈3.4-3.6（→60-65 分），整图对照 2.19（构图敏感，行为正常）；预处理定稿：bbox 外扩 25% → 短边 256 → 中心裁 224 → ImageNet 归一化 RGB
- [x] face_score 落库（base_score + base_model_version）
- [x] `aggregator.py`：top-3 平均 + 路人过滤（n_samples≥2 或代表帧质量≥0.35）→ video_score 滚动更新（处理完成即算）
- [x] `GET /api/videos` 排序/筛选/搜索生效（final_score 排序；修复 _score_payload 键覆盖 video 面容数的 bug）
- [x] `tests/`：聚合公式/top-K/路人过滤/重算覆盖 ×3、打分链路端到端 ×1（ beauty 模型就位断言 0-100 分落库）
- [ ] 人工抽检真实片源 20 张（无真实样本，随用户首次入库进行）

**验收说明**：pytest **27 passed**。base_score/视频综合分已全链路落库（含 lena 真人视频）。

## M5 — WebUI 第一版 ✅（2026-09-16）

- [x] Vue3 + Vite + Element Plus 工程（手写工程文件，node 便携版 `tools/node-v22.23.2-win-x64/`）；dev 模式 proxy `/api`→8000；构建产物由 FastAPI StaticFiles 托管（dist 缺失时自动 API-only 模式）
- [x] 评分页：面容卡片流（缩略图/基础分/来源+时间点）+ 1-10 按钮组 + 👍👎 + 跳过 + 评分后自动下一张 + unrated 优先 + 批量进度
- [x] **对比模式（ADR-013 后端+前端）**：`GET /api/faces/pair`（similar/random 选对策略、排除已对比对、NO_PAIR 空状态）+ `POST /api/faces/pair/compare` + A/B 点选 UI；配套 `POST /api/faces/{id}/rating`、`GET /api/ratings/stats`（histogram/pair_count/unique）、faces 列表 `my_rating`/unrated 过滤/代表帧时间点
- [x] 视频库页：对照表（综合分标签/基础分/个性化分/面容数/状态，排序+搜索+筛选+分页）+ 行点击播放器弹窗（Range 流式，实测 readyState=4 正常播放）
- [x] 任务/设置页：工作目录增删、扫描/处理触发、任务表 2 秒轮询（进度/结果/错误）
- [x] 测试：**32 passed**（新增 pair 选对策略/排除/穷尽 + rating 校验/统计/unrated 过滤 5 项）
- [x] 真实浏览器验收（IAB 实测）：四个 tab 全部渲染正确；评分卡片点击 7 分→自动下一张→空状态闭环；视频行点击→播放器弹窗流式播放 3s 视频；任务表显示历史 job 进度与结果

**验收说明**：浏览器全流程走通——目录→扫描→处理→评分→视频表→在线播放 ✓（单人对比的成对交互由 API 测试覆盖，多人真实片源场景待用户入库复核）。

## M6 — 用户偏好接续训练 ✅（2026-09-16）

- [x] `personalizer.py`：**方案定稿偏离说明**——原计划 sklearn Ridge/MLP，因 pairwise 联合训练需自定义损失（共用打分头，ADR-008/013），改为 **numpy 全批梯度下降线性头** `score(x)=w·x+b`：损失 = w_abs·MSE(绝对) + w_pair·softplus(-y·Δs)（RankNet/Bradley-Terry 式）+ L2；特征 = [mean_embedding, base/100, rep_quality]；标签映射定稿：score→(v-1)/9·100，**up→100 / down→10**；同一 identity 取最新评分（跨 type）；零新增依赖
- [x] 版本化：`data/models/personalizer/v{n}.npz + meta.json`（n_abs/n_pair/mae/r2/pair_acc）；门槛：总样本 ≥20；启用是显式动作（activate）
- [x] 触发：手动按钮 + 每新增 30 条评分/对比自动提交训练 job（`_maybe_autotrain`，不自动启用）
- [x] 启用后：批量写 `face_score.personalized_score` + `aggregator.recompute_all` 滚动更新；deactivate 回退基础分
- [x] API：train-start（409 防重）/ status / versions / activate / deactivate
- [x] WebUI 训练页：训练按钮、启用状态、版本表（指标/时间/启用操作）、停用
- [x] `tests/`：样本不足拒绝、合成数据训练（pair_acc≥0.75、好差组拉开>20 分）、启用→视频分切换个性化、回退→回退基础分、API 全流程——**36 passed**
- [x] 实环境闭环：4 个视频 → 2 评分 + 18 对比 → 训练 v1 → 启用 → 视频表 personalized_final 生效（退化数据指标差属预期：同脸矛盾标签；合成信号数据已证指标正常）

**验收说明**：pytest 36 passed；真实 UI 训练页渲染与启用状态验证通过。**剩余项**：真实片源下"用户偏好评级排名上移"的主观验收，随用户数据积累观察。

## M7 — Docker 部署与收尾 🚧（产物就绪，容器实测待宿主 Docker）

- [x] `Dockerfile`（根目录）：多阶段（node:22-alpine 构建前端 → python:3.12-slim + apt ffmpeg）；模型不进镜像（体积+许可），卷挂载提供
- [x] `docker-compose.yml` + `.env.example` + `.dockerignore`：`/videos:ro` 与 `./data` 卷；`SOPHOS_WORK_DIRS=/videos` 容器路径约定（Windows 宿主路径映射说明写入 docker/README）
- [x] `scripts/backup_db.py`：VACUUM INTO 在线备份（本机实测通过）
- [x] `docker/README.md` 部署指南定稿；README 快速开始改为 Docker 优先
- [ ] **容器端到端验收**：本机无 Docker（安装属系统级变更，待用户）；在有 Docker 的机器上 `docker compose up -d --build` + 全流程 = M7 完结（tag v1.0.0）
- [ ] （可选）mkv/avi 转码播放评估（需容器内实测后决定）

**验收说明**：代码与文档产物全部就绪并通过静态审查；唯一未闭环项为容器实机验收（被宿主环境阻塞，非代码问题）。

## R1 — v1.0.1 修复批次 🔧（2026-09-16 开发完成，待用户验收）

**目标**：落地用户真实片源试用反馈的 5 项问题。**详细方案（根因取证、设计、任务拆解 T1-T9、测试与验收标准、风险表）见 [PLAN_v1.0.1.md](PLAN_v1.0.1.md)**，本节仅作索引。交接实录见 HANDOFF.md R1 节。

- [x] T1 DB 新列（video 编码 / face 姿态遮挡 / identity 性别置信）+ `db/migrations.py` 轻量迁移 + 8 个新配置项
- [x] T2 face_engine：质量门真实生效 + 截断剔除 + 5 点姿态估计（probe_pose.py 真实片源定标，修正 pitch 符号）+ occlusion_score；样本级性别硬过滤移除
- [x] T3 pipeline：先聚类后 identity 级性别裁决 → 合并 pass（同人碎 identity 合并）→ 样本修剪 → rep 按"干净/正面/清晰"优选
- [x] T4 personalizer：全遮挡 identity 剔出训练集（遮挡面容保留可评分但不污染训练）
- [x] T5 播放三档：ffprobe 探测入库 + direct/remux(-c copy)/transcode 自动分档 + 并发限流
- [x] T6 videos API 按库（dir_path/workdir）筛选（含 `GET /api/videos/libraries`）
- [x] T7 前端：视频库库下拉 + 播放模式提示；评分页遮挡标记按钮 + 性别置信度小字
- [x] T8 pytest 补齐（PLAN §10）+ 全量回归（36 → 67 passed）+ 真实 HTTP 冒烟（hevc mkv 转码档实测）
- [x] T9 文档回写（PLAN §12）+ 批次提交 + `git tag v1.0.1`（随批次完成，沿 M7 先例；PLAN §11 五条标准的人工抽检列为用户验收项，若需调阈值走 v1.0.2 修复批次）

出口条件：pytest 全绿（✅ 67 passed）+ 文档回写（✅）；人工抽检（PLAN §11）待用户。

## R2 — v1.0.2 强化批次 ✅（2026-09-17 开发完成，待用户复检）

**背景**：R1 验收反馈——清晰度达标，但男性误入仍多（部分女性置信 98%）、侧脸/超小脸/看不全仍漏。要求不受限于原技术栈，并以"模型视觉能力"（会话内目检）参与验收。**根因取证与迭代实录见 HANDOFF.md R2 节，决策依据见 DECISIONS ADR-016。**

- [x] 目检审计工具：`scripts/export_review.py`（处理→rep 裁剪→接触表）+ `probe_clip.py`（CLIP 对拍）；E01 基线审计定位 4 男性漏网（genderage 系统性高置信误判 0.83–1.00）与 2 强侧女
- [x] CLIP ViT-B/32 第二意见：`export_clip_gender.py` 导出视觉塔 ONNX + 固化 prompt 向量；identity 级与 genderage **一致同意**门控（`clip_gender_min=0.50`，缺模型自动降级）；`clip_female_mean/clip_profile_prob` 落库（migrations +3 列）
- [x] 头部姿态模型：`headpose_resnet18.onnx`（6DRepNet 系，GitHub release 直链）替代 5 点启发式做样本姿态门与 rep 侧脸拒绝（`allow_side_rep=false`）；缺文件回退启发式
- [x] 几何门收紧：`min_face_size` 64→80；`kps_outside` 关键点出画剔除
- [x] 实测排除项（ADR-016）：CLIP 零样本 profile/occluded 紧裁剪不可用（误杀 lena 曾回归失败，已回退 occlusion 融合并修正形参默认值）
- [x] E01 迭代验证：17 → 9 → **7 identity（全女性、全 frontal/near、无小脸/出画脸），逐格目检通过**
- [x] pytest 67 → **79 passed**（一致同意门 3 / 侧脸 rep 2 / kps+方裁 2 / CLIP 数学 2 / headpose 解码 3）
- [x] 前端评分页双票置信度小字 + rebuild；文档（HANDOFF/DEVELOPMENT_PLAN/DECISIONS/README/.env.example/models README）+ tag v1.0.2

出口条件：E01 目检审计通过（✅）+ pytest 全绿（✅）；用户全库复检待做。

## R3 — v1.0.3 批次 ✅（2026-09-17 开发完成，待用户复检）

**背景**：R2 复检反馈——效果明显改善，但 ①面容截图范围太小（要求 头顶+20%/两侧各+20%/向下+80%）；②仍截取很多不清晰、很暗的面容，宁缺毋滥。决策依据见 DECISIONS ADR-017，实录见 HANDOFF R3 节。

- [x] 人像取景缩略图：`portrait_crop`（头顶+20%/两侧+20%/向下+80%，高 320 原生分辨率 q90）替代 112 对齐脸放大；抽帧保留至入库后；前端竖版自适应；识别/打分输入口径不变
- [x] 质量门 v3：sharp 归一重新定标（/200→/80，E01 成分探针定标）+ 曝光因子（暗场衰减）+ 对比度因子（灰蒙蒙衰减）；`min_quality` 0.15→0.30（三因子同时达标）
- [x] 教训沉淀：首版陡曲线+0.25 阈值误杀全部已认可脸 → 改归一化定标比抬阈值更本质（ADR-017）
- [x] E01 复审：9 identity 全女性、取景/清晰度达标、无暗糊脸；pytest 79 → **81 passed**

出口条件：E01 目检通过（✅）+ pytest 全绿（✅）；用户复检待做。

## R4 — v1.0.4 批次 ✅（2026-09-17 开发完成，待用户复检）

**背景**：用户反馈 ①两两对比应先追求"不同的人之间比较"；②同一视频内同人被拆成多个 identity；③视频在线播放报 `Request failed with status code 404`。**方案（T1-T8、验收标准、"明确不做"清单）见 [PLAN_v1.0.4.md](PLAN_v1.0.4.md)**，决策见 DECISIONS ADR-018/019/020，实录见 HANDOFF R4 节。

- [x] T1 基线统计：主库 E01-E16 共 321 identity，**~78% 单样本**（碎裂确认）；回归集 = HIMYM S01 全季（他/她同帧场景丰富）
- [x] T2 双层 merge（ADR-018）：高置信 ≥0.78 直接合并（沿 R1）+ 谨慎层 [0.72, 0.78)（双方 ≥2 干净样本 + 干净均值过线 + 无同帧共现）；**同帧共现一票否决**（优先级高于任何相似度）；阈值配置化（MERGE_REVIEW_THRESHOLD / MERGE_COOCCUR_TOLERANCE_SEC）
- [x] T3+T4 diverse 策略（ADR-019）：`GET /api/faces/pair?strategy=diverse`（**前端默认**），明确 Tier A/B/C 排序 = 明显不同人物 > 不同视频 > 其余未比较 pair；各层内再按分差小 + 随机抖动；`different_person_confidence` 不落库（共现 1.0 > 余弦 ≤0.40 > 未知 > ≥0.60），**未知置信度不压过视频差异**；`pair_comparison` 结构不变，跨视频同演员仅降级不禁止；similar 保留原语义 + 轻度人物偏好
- [x] T5 pair 单测：明显不同人物优先 / 同帧优先 / 证据不足跨视频优先 / **Tier B 跨视频压过同视频未知** / 跨视频同演员 fallback / 分差次级 / 已比较排除 / 耗尽 NO_PAIR（test_pairs_diverse.py 8 项）
- [x] T6 播放 404 根因：**前端 axios HEAD 探测 baseURL 双前缀**（`/api/api/...` → 404 误杀播放弹窗）——非 MKV/HEVC 编码问题
- [x] T7 stream_url 契约（ADR-020）：videos 列表/详情下发 `stream_url`；前端原生 `<video>` 播放 + 删 axios 探测 + video error 事件提示；404 分型 VIDEO_NOT_FOUND / SOURCE_NOT_FOUND；起播预检 → STREAM_FAILED / TRANSCODE_FAILED（500 + stderr）；FFMPEG_NOT_FOUND；中途失败完整上下文日志
- [x] T8 播放回归：MP4 h264/aac direct / mkv h264+aac remux / mpeg4/hevc transcode / VIDEO_NOT_FOUND / SOURCE_NOT_FOUND / API 路由不被 SPA fallback 捕获 / remux+transcode 失败分型（6 项新增）
- [x] 实机验证：真实库 uvicorn + HIMYM E11（hevc/eac3）转码档 200 + fMP4 + `X-Sophos-Stream-Mode: transcode`；diverse 连续 30 对（同帧共现同视频 12 + 跨视频 18，无重复）；404 分型 JSON 正确；**浏览器 IAB** 原生 video `readyState=4`、`currentTime=5.42s`、无 error；对比页默认 radio checked 且 A/B 渲染
- [x] pytest 81 → **100 passed**；frontend build 通过（仅既有 chunk size warning）；文档回写（本表/HANDOFF/API_DESIGN/ARCHITECTURE/DECISIONS/README/.env.example）+ tag v1.0.4

出口条件：见 PLAN §29 清单——合并/对比/播放三项语义均落地（✅ pytest + HTTP/IAB 实机）；“同人碎片显著减少 + 无 Robin/Lily 误合并 + 多集重处理后的 identity 变化”待用户复检（R4 代码/契约已闭环）。

## R16 — v1.9.1 播放与数据性能批次 ✅（2026-10-08 已部署 + 技术压测完成）

**背景**：重封装/转码视频时间轴不完整、快速拖动导致转圈并连带卡死；同时需持续修复质量更好、更正面的面容未被采样的问题。

- [x] 首块前断连取消 ffmpeg 并释放转码槽位；等待槽位期间也可取消
- [x] direct 播放停滞不误降级；换源前断开旧 video 请求，降低快速拖动残留请求
- [x] ffprobe 技术时长 API + 扫描回写 + 播放器异步钉住真实时长（不解析标题/内容语义）
- [x] 评分最新值、已对比过滤、轻量抽样三类查询复合索引及幂等迁移
- [x] 弱代表（单样本/遮挡/非正脸/低质量）触发半间隔相位补采样，带最大帧数成本护栏
- [x] 后端 222 项回归与前端构建通过
- [x] 部署后技术性 Range/seek/取消/索引压测
- [ ] 用户浏览器快速拖动与时间轴验收

出口条件：自动化回归与构建通过；部署容器健康、快速 seek 不失控、索引生效且自动化设置按原状恢复。

## R18 — v1.10.0 设置页与面容库重建批次 ✅（2026-10-08 源码完成，待部署）

**背景**：用户要求拆分“设置项”和“任务列表”；面容性别扩展为仅女性/仅男性/全部；提供彻底重建面容库入口，且不改变已训练模型文件。

- [x] 独立 Settings 页；任务页保留目录、扫描/处理和任务控制
- [x] `gender_selection=female|male|all` 运行时设置，三档 gender/CLIP 裁决
- [x] `POST /api/process/rebuild-faces`：全部可处理视频置 pending，按现有批大小分批链式重跑，可暂停/取消
- [x] `force_refresh` 跳过旧 identity 人工数据保护；模型文件与 active 版本不动
- [x] 重建只重跑视频面容派生数据，不能凭原帧缓存“轻量”生成新 identity
- [x] 完整回归 229 passed；停机数据库备份 `quick_check=ok`
- [ ] 部署后先小批量验收性别策略与重建进度，再允许全库长任务运行

## R17 — v1.9.2 面容评分交互与重提批次 ✅（2026-10-08 源码完成，待部署）

**背景**：移动端从播放/面容评分返回时会退出上一级；评分/对比页缺少当前视频全部面容入口；需汇报并验证面容优选落实情况。

- [x] `FaceRatingDrawer` 复用组件：视频库、评分页、对比页共用全部面容分页、逐个评分与整体好评/差评
- [x] 抽屉打开时占用 history 条目；系统返回/侧滑只关闭抽屉，保留上一级页面
- [x] `POST /api/process/reprocess`：指定 done/failed 视频重提面容，保留“有评分/对比禁止删除”安全门
- [x] 抽帧 ffmpeg 低优先级，降低 4K/HEVC 重提时对在线服务的影响
- [x] 面容优选回归：干净正脸小 yaw 样本优先于高质量遮挡样本；弱代表触发半间隔相位补采样
- [ ] 部署后对无人工信号候选视频执行一次真实重提，记录 `phase_samples / samples / identities`

## R15 — v1.9.0 交互增强批次 ✅（2026-10-08 源码就绪，待用户验收）

**背景**：用户要求任务列表把进行中的任务排在已完成任务之前，组内保持原有顺序；视频库支持对单个视频内出现的面容整体好评/差评，并可调出提取面容逐个评分。详细交接见 [HANDOFF.md](HANDOFF.md) R15 节。

- [x] `GET /api/jobs` 状态组排序：running → queued → paused → 终态；每组内继续按 id 倒序
- [x] `POST /api/faces/rate-video`：单视频全部 `FaceIdentity` 原子写入同一 thumbs 评分，保留视频不存在 / 无面容 / 非法 verdict 的分型错误，并沿用自动训练触发
- [x] 视频库桌面与移动端 UI：整体好评/差评确认、提取面容抽屉、缩略图与分数展示、逐个 1-10 评分或好评/差评
- [x] 新增任务排序与批量评价 API 回归；前端构建通过
- [ ] 用户浏览器验收：真实任务排序、单视频整体评价、面容抽屉逐个评分与训练样本落库

出口条件：自动化回归与前端构建通过（✅）；真实浏览器操作和生产数据落库待用户验收。

---

## 环境要求与备注

| 依赖 | 现状（2026-09-16） | 备注 |
|---|---|---|
| Python 3.13.15 | ✅ `C:\Users\Wang\AppData\Local\Programs\Python\Python313\python.exe`（不在 PATH） | 本项目 target ≥3.10 |
| git | ❌ 未安装 | M2 建议安装（版本管理利于跨批次交接） |
| ffmpeg | ❌ 宿主机未装 | M3 前需装（winget/choco）或依赖 M7 容器；`frame_sampler` 需运行时调用 |
| Docker | 待确认 | M7 使用 |
| pip 源 | — | 国内网络建议 `pip install -i https://pypi.tuna.tsinghua.edu.cn/simple` |

## 风险与应对

| 风险 | 影响 | 应对 |
|---|---|---|
| SCUT-FBP5500 开源 ONNX 权重不可得 | M4 延误 | **已解决（M4）**：HF `Gustrd/SCUT-FBP5500-PyTorch-Model`（MIT）权重经键名映射转 ONNX，实测可用；etrain 源的 Google Drive 文件已失效 |
| InsightFace 系模型为非商业许可 | 合规 | 项目定位个人本地工具；已在 README 声明 |
| mkv/avi 等格式浏览器不能直接播 | M5 播放体验 | MVP 明确标记不支持；M7 评估 ffmpeg 转码 |
| 视频量大导致处理慢 | 体验 | jobs 断点续跑 + 串行低优先级处理 + 抽帧间隔可调 |
| Windows 路径/中文文件名 | 稳定性 | 全程 `pathlib` + UTF-8；容器内路径映射需与 DB 一致（M7 重点测试） |
| numpy/onnxruntime 版本兼容 | 安装失败 | **已解决（M3）**：Python 3.13 需 numpy≥2.1 + onnxruntime≥1.20（requirements 已定稿并安装验证） |
| 聚类阈值不当（过合/过散） | 面容库质量 | 阈值可配（默认 0.68）；M3 用真实样本回归测试 |
| 跨批次上下文丢失 | 交接 | **每批次结束强制更新 HANDOFF.md 与本表**（见文首纪律） |

## 批次间交接纪律（强制）

1. 勾选本文件对应任务与总览表；
2. `HANDOFF.md` 顶部新增交接记录（用其内置模板）；
3. 更新 `README.md` 的"开发批次与当前进度"表；
4. 完成一次提交（若 git 可用）并打 tag；
5. 跑通全部 pytest 后再宣布批次完成。
