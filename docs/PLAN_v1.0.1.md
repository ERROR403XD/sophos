# Sophos v1.0.1 实施方案（面容质量与播放体验修复）

> 状态：**已实施完成（2026-09-16，T1-T9 全部落地，pytest 67 绿）**；剩余：用户真实片源抽检（§11）→ tag `v1.0.1`。
> 依据：用户 2026-09-16 反馈的 5 个问题。代码引用基于 v0.7.0（`backend/app/...`）。
> 阅读顺序：本文 → 相关源码 → ARCHITECTURE / DATA_MODEL / API_DESIGN 对应章节（实施后回写）。
> 交接实录见 [HANDOFF.md](HANDOFF.md) R1 节。

## 0. 背景与范围

用户在真实片源入库试用后反馈 5 个问题，按流水线顺序编号 P1-P5：

| # | 问题 | 一句话方案 |
|---|---|---|
| P1 | 性别筛选不准，不能很好地只选出女性面容 | 样本级过滤改为**先聚类、identity 级投票裁决**，阈值上调可配 |
| P2 | 模糊/角度不佳面容应剔除；面具等遮挡物视为面容一部分，但不得污染训练 | 质量门**真实生效** + 5 点启发式姿态估计 + identity 级遮挡标记与"干净样本 embedding" |
| P3 | 相同人物保留太多 | 聚类后**合并 pass**（组间余弦二次合并）+ identity 内样本修剪 + 代表帧按质量优选 |
| P4 | mkv 在线播放失败 | 播放分三档：**直出 / ffmpeg 重封装（-c copy）/ 转码**，扫描期 ffprobe 探测编码入库 |
| P5 | 视频库缺按库筛选 | `GET /api/videos` 增加 `library` 参数 + 前端库下拉 |

**目标**：P1-P5 全部落地，pytest 全绿，真实片源人工抽检通过。
**非目标（本期不做，防蔓延）**：跨视频人物合并（ADR-007 既有扩展项）、转码结果缓存、性别/遮挡二次确认模型（换 det_10g 等）、人脸对遮挡的自动抠图修复。

## 1. P1 性别筛选 —— 根因与方案

### 1.1 根因（代码取证）

- `face_engine.py:230-249`（`process_frame`）：**逐样本**做 `female_prob >= 0.5` 硬过滤，过滤发生在**聚类之前**。genderage 模型在运动模糊、侧脸、低分辨率帧上输出波动大：
  - 男性样本偶发 fp>0.5 → 单帧即成 identity，混入面容库（假阳性）；
  - 女性边缘帧被丢 → 同一人物样本骤减，`mean_embedding` 不稳、聚类易碎（间接加剧 P3）。
- 单样本 identity（一帧一张脸）没有任何投票修正机会。
- `config.py:31` `female_threshold: float = 0.5`，样本级阈值过松且只有这一道闸。

### 1.2 方案：先聚类、identity 级裁决（ADR-014）

流水线顺序改为（`pipeline.py::_process_video`）：

```
检测 → 尺寸/质量/截断/姿态过滤（P2 门控）
     → 全部候选样本 embed + 记录 female_prob / occlusion_score（不再按性别丢弃）
     → Chinese Whispers 聚类（现有 clustering.py 不变）
     → 【新】identity 级性别裁决 → 合并 pass（P3）→ 样本修剪（P3）
     → rep/缩略图/打分入库
```

identity 裁决规则：

- `identity_female_prob = 组内样本 female_prob 均值`（写 `face_identity.female_prob_mean`）。
- 保留条件：均值 ≥ `SOPHOS_FEMALE_IDENTITY_THRESHOLD`（默认 **0.60**）；**单样本** identity 用更严的 0.65（无投票修正，须保守）。
- 未过阈值 → 整组连同样本删除，不入库（保持"面容库只含女性"语义）。

收益：男性偶发误判被组均值稀释（组内绝大多数帧 fp<0.5 才会被拒）；女性 identity 从"更多边缘帧参与投票"中受益，样本更全、均值更稳。

### 1.3 变更点

| 位置 | 变更 |
|---|---|
| `face_engine.py::process_frame` | 去掉 fp 硬过滤；`female_prob` 降级为"样本级记录值" |
| `pipeline.py::_process_video` | 聚类后加 `_gender_filter(groups, samples)`；删除样本级阈值传参 |
| `config.py` | 新增 `female_identity_threshold: float = 0.60`；`female_threshold` 注释改为"样本级记录口径" |
| `db/models.py::FaceIdentity` | 新增 `female_prob_mean REAL`（迁移见 §6） |
| 前端 RateView/PairView | 卡片小字显示性别置信度（可选，便于用户抽检） |

**可选增强（列后续，本期不做）**：`--pkg buffalo_l` 换 det_10g 检测器（更准）；轻量性别复核模型。

## 2. P2 质量/姿态过滤与遮挡处理

### 2.1 质量门真实生效（现状等于没有）

- 现状：`quality_score`（`face_engine.py:165-177`）= 拉普拉斯清晰度 × 尺寸因子，但流水线只丢弃 `q <= 0.0`（`face_engine.py:243`）——**几乎不过滤**，模糊人脸照常入库。
- 变更：
  - 新配置 `min_quality: float = 0.15`：`q < min_quality` 的样本丢弃。
  - **截断剔除**：bbox 与帧边界接触边 > 自身边长 20% → 视为"不完整"丢弃（出画半张脸不再入库）。实现在 `process_frame` 内（需传入帧宽高，现有签名已有 img）。
  - `min_face_size`（64px）维持不变。

### 2.2 姿态估计（不加模型，5 点启发式）

- SCRFD 已输出 5 点关键点（眼×2、鼻、嘴角×2），据此估算：
  - **yaw**：`dL=|nose.x−left_eye.x|`、`dR=|right_eye.x−nose.x|`（原图坐标），`asym=(dL−dR)/(dL+dR)`，`yaw ≈ arcsin(asym)` 经验放大系数；
  - **pitch**：`(nose→mouth 垂直距)/(eye_center→nose 垂直距)` 与 ArcFace 标准比例（≈20.1/20.6≈0.975，见 `face_engine.py:25-27`）的偏差做经验映射。
- 启发式精度有限（±10° 量级），**只用于剔除极端角度，阈值放宽**：
  - 新配置 `max_yaw_deg: float = 80`（保留正面/接近正面/**清晰完整的侧面**；>80° 近全侧/后侧剔除）、`max_pitch_deg: float = 40`（重度俯仰剔除）。
  - 样本落库字段：`face.pose_yaw / pose_pitch / pose_class`（`frontal` |yaw|≤15、`near` ≤45、`side` ≤80）。
  - **定标任务**：实施时新增 `scripts/probe_pose.py`，用真实片源人工核对 20+ 帧校准放大系数与阈值（沿用 M3 探针脚本模式）。
- 代表帧与打分偏好：rep 选择排序键由"quality 最高"（`pipeline.py:172`）改为 `(干净优先, pose_class 等级, quality)`——同 identity 有正面清晰样本时绝不选模糊侧面做代表（直接影响缩略图与 base_score 输入，见 §5）。

### 2.3 遮挡（面具等装饰）——按用户语义实现

用户语义原文：**遮挡物视为面容的一部分（保留、可评分=偏好信号），但不得影响训练倾向（避免学会"喜欢面具"）**。落地为三条规则：

1. **库内保留**：含遮挡的 identity 是合法面容，正常出现在评分/对比/视频聚合中。
2. **特征隔离**：`mean_embedding` 只用"干净样本"（`occlusion_score < 0.6`）计算；处理期写库时即按此口径（训练/apply 端无需再改）。
3. **全遮挡剔除出训练**：整组无干净样本 → identity 标记 fully-occluded，从个性化训练集（绝对评分 + 对比对）**剔除**；其评分仍计入统计与页面展示。

实现：

- **样本级** `face.occlusion_score`（0-1，启发式提示）：对齐 112 图下半区（嘴/下巴，约 y∈[64,112]）灰度方差 + 边缘密度显著低于上半区 → 疑似口罩/面罩。仅作提示，不自动剔除任何样本。
- **identity 级** `face_identity.occluded`（0/1）+ `occluded_source`（`auto`/`manual`）：组内多数样本 `occlusion_score ≥ 0.6` → 置 auto 提示；**评分页提供"有遮挡"切换按钮**（用户人工标记，manual 覆盖 auto）→ `POST /api/faces/{id}/occlusion`。
- `personalizer.py::identity_features`：fully-occluded identity 不进 `build_training_set`（`personalizer.py:83-99` 处过滤）；`occluded=1` 且有干净样本的 identity 正常参与（embedding 已是干净口径）。
- `aggregator.py` 不改：全遮挡 identity 的 base_score 可靠性差，但 top-3 按 base_score 择优（`aggregator.py:36`）天然低分淘汰；rep 已优先干净样本。记为已知限制，文档回写。

### 2.4 与 P1 的配合

先聚类后裁决让 P2 的门控（质量/截断/姿态）在**样本级**执行、遮挡在**identity 级**执行——层级清晰：样本级管"这张脸能不能作为证据"，identity 级管"这个人算不算女性/是否带遮挡"。

## 3. P3 相同人物冗余

### 3.1 现状

- Chinese Whispers 单次聚类（`clustering.py`，阈值 0.68），**无合并 pass**：同一人因侧脸/光线变化常碎成多个 identity。
- identity 内全部样本平等参与 `mean_embedding`（`pipeline.py:175`），大量近重复帧会拉偏均值。
- 无跨视频合并（ADR-007 有意不做，维持）。

### 3.2 方案

| # | 措施 | 说明 |
|---|---|---|
| 3a | **合并 pass** | 聚类后计算各组 mean_embedding，贪心合并：组间余弦 ≥ `SOPHOS_MERGE_THRESHOLD`（默认 **0.78**，显著高于聚类阈值以防过合）→ 合并为一组（样本并集、rep/缩略图/分数重选）。迭代至无合并发生 |
| 3b | **样本修剪** | identity 的 `mean_embedding` 只用 top-N 样本（`SOPHOS_MAX_SAMPLES_PER_IDENTITY: int = 16`），排序键 = (干净样本优先, pose_class, quality)；`n_samples` 仍记全量（聚合路人过滤语义不变，`aggregator.py:34`） |
| 3c | **rep 优选** | 见 §2.2：正面 > 近正面 > 侧面，同级比 quality |
| 3d | 聚类阈值 | 0.68 维持默认可配；合并 pass 兜底碎片化，避免盲目调高阈值伤召回 |

验收口径：同一视频内"明显同一个人"不再出现多个 identity（人工抽检 ≥5 个多人视频）；评分页同脸卡片显著减少。

## 4. P4 mkv 在线播放 —— 播放三档（ADR-015）

### 4.1 现状

- `streamer.py:12-21` MIME 白名单仅 mp4/m4v/webm/mov，mkv 落到 `videos.py:96-101` → **415 FORMAT_UNSUPPORTED**；前端提示"暂不支持"（`VideosView.vue:88-95`）。
- Docker 镜像内置 ffmpeg（`Dockerfile` apt ffmpeg）；开发机 `tools/ffmpeg-9.0.1` 已有——**具备现场转码/重封装条件**，缺的只是后端实现。

### 4.2 方案：按编码能力分档，而不是按扩展名一刀切

**第一步（扫描期探测）**：`scanner.py` 每视频落库后调 ffprobe（新增 `locate_ffprobe`，与 `frame_sampler.locate_ffprobe` 同模式）探测 `vcodec/acodec` 写入 `video` 表新列（失败留空，播放期再试）。

**第二步（播放期决策）**：重构 `streamer.py`，`GET /api/videos/{id}/stream` 内部自动选档：

| 档位 | 条件 | 实现 | 体验 |
|---|---|---|---|
| ① 直出 direct | mp4/m4v/webm/mov 且 vcodec∈{h264,vp9} acodec∈{aac,opus} | 现状 FileResponse（真 Range，可拖动） | 最佳 |
| ② 重封装 remux | 容器不兼容（mkv/avi/ts…）但 **vcodec=h264 且 acodec=aac**（编码浏览器本就支持，只是容器不认） | `ffmpeg -i src -c copy -movflags frag_keyframe+empty_moov -f mp4 pipe:` | 秒开，CPU≈0；fMP4 分段支持基本拖动 |
| ③ 转码 transcode | vcodec∈{hevc,mpeg4,...} 或 acodec 不兼容 | `ffmpeg -c:v libx264 -preset {transcode_preset} -crf 23 -c:a aac -movflags frag_keyframe+empty_moov -f mp4 pipe:` | 启动延迟数秒；CPU 高 |

通用约定：

- ②③ 走 `StreamingResponse` 管道，无 HTTP Range（fMP4 分段由浏览器自行 seek）；**客户端断开时终止 ffmpeg 子进程**（`finally: proc.kill()`，防僵尸进程堆积）。
- 转码并发限流：`SOPHOS_TRANSCODE_MAX_CONCURRENCY`（默认 1，`threading.Semaphore`）；②③ 响应头 `X-Sophos-Stream-Mode: direct|remux|transcode`。
- `SOPHOS_TRANSCODE_ENABLED`（默认 true）= false 时退回现状 415 行为。
- ffprobe 探测失败且扩展名不在白名单 → 保持 415（错误信息给出诊断：探测失败/编码不支持）。
- 转码缓存（重复播放不重转）列为后续可选，本期不做。

### 4.3 变更点

| 位置 | 变更 |
|---|---|
| `streamer.py` | 重构：`probe_codec()`、`decide_mode()`、`iter_ffmpeg_pipe()` |
| `frame_sampler.py` | 补 `locate_ffprobe()`（供 streamer 复用，或挪到公共工具模块） |
| `api/videos.py` | stream 端点接入三档；`GET /api/videos/{id}` 与列表项新增 `stream_mode` 字段（前端提示用） |
| `db/models.py::Video` | 新增 `vcodec/acodec TEXT` |
| `scanner.py` | 落库后异步探测编码（放在 process 前即可，不阻塞扫描主流程；简单起见扫描时同步探测，单条 <100ms） |
| `config.py` | 新增 `transcode_enabled / transcode_preset(veryfast) / transcode_max_concurrency(1)` |
| 前端 VideosView | 播放弹窗按 `stream_mode` 显示标签（"直出/重封装/转码中，首次加载稍慢"）；移除 mkv 一律不支持的提示 |

**测试**：用 tools/ffmpeg 合成两个小样本——h264+aac 的 mkv（验 remux 档 200 + 管道输出 MP4 魔数）、mpeg4 编码 mkv（验 transcode 档，低分辨率控 CPU）；TestClient 断言 content-type 与 `X-Sophos-Stream-Mode`；真机浏览器验证拖动。

## 5. P5 视频库按库筛选

- 数据已具备：`video.dir_path` 存扫描根目录（`scanner.py:75`），`GET /api/workdirs` 返回当前库列表。
- 后端：`GET /api/videos` 新增 `library: str | None` 参数（=dir_path 精确匹配）；列表响应每项已有 `path`，无需改结构。
- 前端 VideosView：新增"库"下拉——选项 = workdirs ∪ DB 中 distinct dir_path（删除库后历史视频仍可筛）；显示 `Path.basename`，value 存完整 dir_path；与状态/搜索过滤叠加。
- API_DESIGN §3.3 补参数说明。

## 6. 数据库变更与迁移

### 6.1 新增列（全部可空/带默认，SQLite ADD COLUMN 即可）

| 表 | 列 | 用途 |
|---|---|---|
| video | `vcodec TEXT`、`acodec TEXT` | 播放分档 |
| face | `pose_yaw REAL`、`pose_pitch REAL`、`pose_class TEXT`、`occlusion_score REAL` | P2 |
| face_identity | `female_prob_mean REAL`、`occluded INTEGER DEFAULT 0`、`occluded_source TEXT` | P1/P2 |

### 6.2 迁移方式

- 项目无 alembic；`init_engine`（`session.py:34`）只 `create_all`，**对已存在的表不会加列**。
- 新增 `app/db/migrations.py`：`run_migrations(engine)` 按 `PRAGMA table_info` 检查缺列 → `ALTER TABLE ... ADD COLUMN`；`init_engine` 末尾调用（幂等）。
- 存量数据兼容：新列为空时按"旧行为"处理（如 `occluded` 空=0、`pose_class` 空=不参与偏好）。**已有面容数据需重处理视频才补齐新字段**——当前 sophos.db 为空库（已实测无表），无迁移负担；若用户环境已有数据，升级说明注明"建议全量重处理（有评分的视频仍受既有保护逻辑约束）"。

## 7. 新增配置汇总（config.py，全部 SOPHOS_ 前缀）

| 配置 | 默认 | 说明 |
|---|---|---|
| `female_identity_threshold` | 0.60 | identity 级女性裁决（单样本 identity 用 0.65，写死在代码内并注释） |
| `min_quality` | 0.15 | 样本质量门（拉普拉斯×尺寸分） |
| `max_yaw_deg` / `max_pitch_deg` | 80 / 40 | 姿态剔除阈值（探针定标后可调） |
| `merge_threshold` | 0.78 | identity 合并 pass 余弦阈值 |
| `max_samples_per_identity` | 16 | mean_embedding 参与样本上限 |
| `transcode_enabled` | true | 播放转码总开关 |
| `transcode_preset` | veryfast | x264 preset |
| `transcode_max_concurrency` | 1 | 转码并发上限 |

## 8. API 变更汇总

| 端点 | 变更 |
|---|---|
| `GET /api/videos` | 新增 `library` 参数 |
| `GET /api/videos`（列表项）/ `GET /api/videos/{id}` | 新增 `stream_mode` 字段（direct/remux/transcode/unsupported） |
| `GET /api/videos/{id}/stream` | 三档自动选择；新增响应头 `X-Sophos-Stream-Mode` |
| `POST /api/faces/{id}/occlusion` | **新增**：body `{occluded: bool}`，写 `face_identity.occluded/occluded_source='manual'` |
| `GET /api/faces` 列表项 | 新增 `female_prob_mean / occluded`（前端展示与标记按钮用） |

## 9. 任务拆解与顺序（批次 R1）

| 任务 | 内容 | 依赖 | 涉及文件 |
|---|---|---|---|
| T1 | DB：models 新列 + `db/migrations.py` 轻量迁移 + config 新增 8 项 | — | db/models.py、db/migrations.py（新）、config.py |
| T2 | face_engine：去性别硬过滤、质量门、截断剔除、姿态估计、occlusion_score；`scripts/probe_pose.py` 定标 | T1 | face_engine.py、scripts/ |
| T3 | pipeline：先聚类后性别裁决 → 合并 pass → 样本修剪 → rep 优选 → 干净样本 mean_embedding | T2 | pipeline.py、clustering.py（加 merge_embeddings）、config.py |
| T4 | personalizer/aggregator 适配：全遮挡 identity 剔出训练集 | T3 | personalizer.py |
| T5 | 播放三档：ffprobe 探测入库 + streamer 重构 + stream 端点 + 并发限流 | T1 | streamer.py、frame_sampler.py、api/videos.py、scanner.py |
| T6 | 按库筛选后端：videos API `library` 参数 | T1 | api/videos.py |
| T7 | 前端：视频库库下拉 + 播放模式提示；评分页遮挡切换按钮 + 性别置信度小字 | T5/T6 | VideosView.vue、RateView.vue、api/faces.py |
| T8 | pytest 补齐（见 §10）+ 全量回归 | T1-T7 | tests/ |
| T9 | 真实片源验收（§11 清单）+ 文档回写（§12）+ `APP_VERSION` 升 1.0.1 + tag `v1.0.1` | T8 | 全部文档、app/__init__.py |

顺序：T1 → T2 → T3 → T4（面容链，一条线）∥ T5 → T6 → T7（播放/筛选线）→ T8 → T9。

## 10. 测试计划（pytest 新增项）

- `test_face_engine`：姿态估计合成 5 点用例（正/左/右/俯仰）、截断剔除、occlusion_score（合成均匀下半区图）、质量门生效。
- `test_clustering`：合并 pass——构造碎片组（同人均值互相 ≥0.78）合并为 1 组；异人组不合并。
- `test_process_pipeline`：①男性样本（组均值 <0.6）整组被拒；②女性多帧均值过阈保留；③全遮挡 identity 入库但 `mean_embedding` 为空/标记 fully-occluded；④rep 选正面不选模糊侧面。
- `test_personalizer`：全遮挡 identity 不进训练集样本数统计。
- `test_videos_stream`：remux 档（合成 h264 mkv → 200 + MP4 ftyp + `X-Sophos-Stream-Mode: remux`）、transcode 档（mpeg4 mkv，小分辨率）、`transcode_enabled=false` 回退 415、不认识的白名单外格式 415。
- `test_videos_api`：library 筛选 count/列表正确、与 q/status 叠加。
- `test_migrations`：旧 schema 库加列幂等。

## 11. 验收标准（对应用户 5 条反馈）

| # | 验收项 | 方法 |
|---|---|---|
| P1 | 面容库中男性误入 <5%，女性召回明显改善 | 真实片源人工抽检 ≥30 个 identity（正面/侧脸/模糊分层） |
| P2a | 评分队列不再出现明显模糊/极端角度/半张脸 | 同上抽检 + 抽帧对比处理前后 |
| P2b | 戴面具面容保留可评分；训练启用后不出现"面具偏好"（面具面容不因面具得分虚高） | 标记遮挡 → 训练 → 对比同一人遮挡/无遮挡帧的 personalized_score |
| P3 | 同一视频同一人物不碎成多个 identity；评分页同脸卡片显著减少 | ≥5 个多人视频人工核对 |
| P4 | h264+aac mkv 浏览器可播（秒开可拖）；hevc/mpeg4 mkv 转码可播 | 真机 Chrome/Edge 实测 |
| P5 | 视频库可按库筛选且与其他过滤叠加 | WebUI 操作 |

## 12. 文档回写清单（T9 完成时）

- `README.md`：功能特性（播放三档、按库筛选、面容质量门）、批次表加 R1。
- `ARCHITECTURE.md`：§3.2 流水线图（先聚类后裁决/合并 pass）、§3.5 播放策略改三档表。
- `DATA_MODEL.md`：§2 新列、迁移机制说明。
- `API_DESIGN.md`：§3.3 library 参数、stream_mode、`POST /api/faces/{id}/occlusion`。
- `DECISIONS.md`：**ADR-014 面容准入**（先聚类后性别裁决 + 质量门/姿态/截断样本级门控 + 遮挡三规则）、**ADR-015 播放三档**（编码探测分档替代扩展名白名单）。
- `HANDOFF.md`：R1 交接记录。
- `.env.example`：新增 8 个配置项。

## 13. 风险与回退

| 风险 | 影响 | 应对 |
|---|---|---|
| 5 点姿态启发式不准 | 误删侧面好脸/放进坏角度 | 阈值放宽（80°/40°）只剔极端；`probe_pose.py` 真实片源定标；样本保留 pose 字段可事后审计 |
| 全样本 embed 增加 CPU（原男性样本也 embed） | 处理变慢 | MobileFaceNet 单张毫秒级；样本数随 P2 门控下降，总量近似持平；必要时先性别后质量顺序微调 |
| 合并阈值 0.78 过合（把两个相似的人并了） | 面容库少人 | 阈值可配 + 仅组均值级合并（非样本链式）；抽检把关 |
| 转码占满 CPU 影响处理任务 | 处理变慢 | 并发限 1；后续可加"播放转码让位于处理任务"调度 |
| 重封装无真 Range，超长视频拖动体验一般 | 体验 | fMP4 分段 + 浏览器渐进 seek；不满足再上转码缓存/分段切片（后续版本） |
| 存量部署升级后旧数据无新字段 | 功能不完整 | migrations 加列 + 升级说明建议重处理；空字段按旧行为兜底 |
