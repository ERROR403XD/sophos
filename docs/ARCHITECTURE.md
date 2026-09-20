# 架构设计（ARCHITECTURE）

> 版本：v0.1（M1 编写）。模块实现批次见各节标注，任务清单见 `DEVELOPMENT_PLAN.md`。
> 阅读顺序建议：本文 → `DATA_MODEL.md` → `API_DESIGN.md` → `DECISIONS.md`。

## 1. 总体数据流

```
工作目录(多个) ──扫描──► video表(pending) ──抽帧──► 帧序列 ──► face_engine
        ▲                                                     │
        │                                              ├─ 人脸检测 (SCRFD / MTCNN 备选)
        │ 增量(mtime/size)                              ├─ 性别过滤 (genderage, 仅保留女性)
        │                                              ├─ 质量过滤 (最小尺寸/清晰度)
   scanner (M2)                                        └─ 对齐+特征 (5点对齐112×112, MobileFaceNet)
                                                               │
                                                               ▼  视频内聚类 (cosine 阈值)
                                                    face_identity 面容库 (一视频多面容)
                                                               │
                              ┌────────────────────────────────┼──────────────────────────┐
                              ▼                                ▼                          ▼
                       代表帧缩略图入库                 基础打分 scorer(SCUT-FBP5500)   user_rating
                       data/thumbs/{id}.jpg            → face_score.base_score        (WebUI: 1-10 / 👍👎)
                                                       (M4)                           (M5)
                                                                                      │
                                                                                      ▼
                                                                            personalizer 接续训练 (M6)
                                                                                     个性化分数
                                                                                      │
                              aggregator 聚合 (M4) ◄───────────────────────────────────┘
                              video_score（滚动更新）
                                      │
                                      ▼
                     WebUI 视频表 + Range 流式在线观看 (M5 / streamer M2)
```

## 2. 模块职责

### 2.1 后端服务层 `backend/app/services/`（每文件一个职责）

| 模块 | 职责 | 实现批次 |
|---|---|---|
| `scanner.py` | 遍历工作目录（支持多个、递归），按扩展名识别视频，增量更新 video 表：新增/变更(→pending 重处理)/消失(→missing)。**只维护路径，不复制文件** | M2 |
| `frame_sampler.py` | ffmpeg 抽帧：默认按固定间隔（`SOPHOS_SAMPLE_INTERVAL_SEC`，默认 2s）抽取，写入 `data/frames/{video_id}/` 临时目录，处理完成后清理 | M3 |
| `face_engine.py` | ONNX 推理统一封装：检测（SCRFD det_10g，MTCNN 可切换）→ 5 点对齐 112×112 → genderage 性别过滤 → 质量过滤（尺寸/拉普拉斯清晰度）→ MobileFaceNet 512 维 embedding | M3 |
| `clustering.py` | 对同一视频内的女性人脸 embedding 做余弦相似度聚类（默认 0.68，可配），形成 `face_identity`（**面容库单元**：一个视频多张明显区别的面容）；每 identity 选最高质量样本为代表帧并生成缩略图 | M3 |
| `scorer.py` | 基础颜值打分：SCUT-FBP5500 系回归模型（ONNX），输入对齐后的人脸图，输出 0-100 分；模型版本化 | M4 |
| `aggregator.py` | 视频综合分：对视频内各 identity 的分数做 top-K（默认 3）平均，得到 `video_score`；**滚动更新**触发点见 §4 | M4 |
| `personalizer.py` | 用户偏好接续训练：以 embedding + base_score 等为特征，绝对评分做回归（映射 0-100）、两两对比做 pairwise logistic（§3.7），联合训练 Ridge/MLP 头；模型按版本存 `data/models/personalizer/`，带指标评估与启用/回滚 | M6 |
| `streamer.py` | 视频在线播放：HTTP Range 流式返回源文件（mp4/webm/m4v/h264-mov 直接播；mkv/avi 等 MVP 提示不播放，M7 评估 ffmpeg 现场转码） | M2（转码 M7） |
| `jobs.py` | 后台任务：进程内 worker 线程（单机串行，MVP 不引入 celery/redis）+ job 表记录进度；支持断点续跑 | M2 |

### 2.2 API 层 `backend/app/api/`（M2 起按 `API_DESIGN.md` 实现）

路由按资源拆分：`workdirs` / `scan` / `videos` / `faces` / `ratings` / `train` / `stream` / `thumbs`。

### 2.3 前端 `frontend/`（M5）

| 页面 | 功能 |
|---|---|
| 评分页（核心） | 面容卡片流：缩略图 + 基础分 + 来源视频/时间点；评分控件（1-10 滑条/按钮组 + 👍👎）；评分后自动下一张；**对比模式**：A/B 两张并排选更好的一张（可跳过） |
| 视频库页 | 视频-分数对照表（排序/筛选/搜索/分页），行内点击弹出播放器在线观看 |
| 任务/设置页 | 工作目录管理、扫描/处理/训练触发、job 进度实时查看 |
| 训练页（M6） | 样本量、指标、历史版本、启用/回滚 |

## 3. 关键设计

### 3.1 幂等与断点续跑（M3 落地）
- 处理流水线以 `video.status` 驱动：`pending → processing → done|failed`。
- 重跑某视频前先删除其全部 `face` / `face_identity` / `face_score` 行（级联），保证幂等。
- job 表记录 `done/total` 进度；进程重启后，`processing` 状态的视频视为失败回退 `pending`，由下一次任务继续。

### 3.2 面容库的组织
- **MVP：面容按"视频内"聚类**，即 `face_identity` 隶属唯一 `video_id`（满足"一个视频形成多张明显区别的面容"）。
- **R4 重申不做跨视频同人合并**（PLAN_v1.0.4 §0-§1，ADR-019）：同一演员在不同视频是两个 identity 属预期；全局人物库（global_person / 增量重关联 / 误合并拆分 / 评分迁移）复杂度远超审美训练收益，扩展项冻结。
- **R1 处理顺序（ADR-014，P1/P2/P3；R4 合并 pass 强化 ADR-018）**：
  ```
  检测 → 样本级门控（尺寸/截断/质量门/姿态）→ 全部候选样本 embed + 记录 fp/occlusion
       → Chinese Whispers 聚类 → identity 级性别裁决（组均值 ≥0.60，单样本 0.65）
       → 双层合并 pass（ADR-018）→ 样本修剪（top-16 干净→正面→高质量）
       → rep 优选（干净 > frontal > near > side，同级比 quality）→ 入库
  ```
  样本级管"这张脸能不能作为证据"，identity 级管"这个人算不算女性/是否带遮挡"；
  `mean_embedding` 只用干净样本（遮挡特征隔离），全遮挡 identity 置空并剔出训练。
- **R4 双层合并（ADR-018）**：第一层组间全样本均值余弦 ≥ `merge_threshold`(0.78) 直接合并（沿 R1）；第二层 `[merge_review_threshold`(0.72)`， 0.78)` 谨慎合并，须同时满足 双方 ≥2 干净样本、干净样本均值也过线、**无同帧共现**（`face.timestamp_sec` 差 ≤ 容差 0.5s，一票否决且优先级高于任何相似度——同框必为两人）。原则：宁可留少量重复 identity，不把不同的人合成一个；性别一致性由上游裁决保证。

### 3.3 评分与滚动更新触发点
`video_score` / `face_score` 的更新时机：
1. 新视频处理完成 → 计算该视频综合分；
2. 新用户评分（含两两对比结果）入库 → 标记对应 identity/video 为 dirty；
3. 新个性化模型训练完成并启用 → 批量重算受影响（或全部）identity 的 `personalized_score` 与 `video_score.final_score`；
4. 扫描发现文件消失 → video 置 `missing`，**不删除记录**（保留分数历史，可配置清理）。

### 3.4 聚合公式（默认值，M4 微调）
- `video.final_score = personalized_final`（存在且模型已启用）否则 `base_final`。
- `*_final = mean(top-K identity 分数)`，K 默认 3；参与聚会的 identity 需满足：样本数 ≥ 2 或代表帧质量分 ≥ 阈值（过滤路人/误检，可配）。

### 3.5 播放策略（R1 三档，ADR-015）
- **扫描期**：ffprobe 探测 vcodec/acodec 入库（失败留空，播放期再试）。
- **播放期** `GET /api/videos/{id}/stream` 按编码能力自动分档（响应头 `X-Sophos-Stream-Mode`）：

| 档位 | 条件 | 实现 | 体验 |
|---|---|---|---|
| direct | mp4/m4v/webm/mov 且 h264/vp9 + aac/opus（探测失败乐观直出） | `FileResponse`（真 Range 206，可拖动） | 最佳 |
| remux | 容器不兼容（mkv/avi/ts…）但 h264 + aac/无音轨 | `ffmpeg -c copy` → fMP4 管道 | 秒开，CPU≈0 |
| transcode | 其余编码（hevc/mpeg4/…） | libx264 veryfast crf23 + aac → fMP4 管道 | 启动延迟数秒，CPU 高 |

- remux/transcode 走 `StreamingResponse`（fMP4 分段，浏览器渐进 seek，无 HTTP Range）；
  `-map 0:v:0 -map 0:a:0?` 只取首视频流+可选首音频（mkv 字幕轨会致 mp4 mux 失败）；
  **客户端断开时终止 ffmpeg 子进程**（生成器 close → finally kill）；并发受
  `SOPHOS_TRANSCODE_MAX_CONCURRENCY`（默认 1）限流；`SOPHOS_TRANSCODE_ENABLED=false` 回退 415。
- 已知限制：重封装无真 Range，超长视频拖动体验一般（fMP4 分段缓解）；转码缓存列为后续可选。
- **R4（ADR-020）**：①`GET /api/videos`/`/{id}` 下发 `stream_url`，前端原生 `<video>` 播放、禁用 axios 探测媒体（axios baseURL 二次前缀曾致 404——"在线播放 404"根因）；②404 分型 `VIDEO_NOT_FOUND`/`SOURCE_NOT_FOUND`；③remux/transcode **起播预检**（首个输出块前失败 → 500 `STREAM_FAILED`/`TRANSCODE_FAILED` + stderr 尾部；中途失败记完整上下文日志），ffmpeg 缺失 → `FFMPEG_NOT_FOUND`；④`/api/*` 先于 SPA fallback 注册（回归测试覆盖）。

### 3.6 训练闭环（M6）——绝对评分分支
- 训练数据：identity 级样本（代表帧 embedding + base_score + quality）为 X；标签统一映射 0-100（1-10 打分 → `(v-1)/9*100`；好评→100 / 差评→10；具体映射 M6 定稿，见 DECISIONS ADR-008）。
- 同一 identity 多次评分取**最新**一次。
- 触发：手动按钮 + 每新增 N 条评分自动（默认 30，可配）。
- 版本化发布：holdout 指标（MAE/R²）达标才允许"启用"；未启用时个性化分为空，回退基础分。

### 3.7 对比评分（pairwise，与 §3.6 并行的偏好来源，ADR-013/019）
- 形式：WebUI 同时给出 A/B 两张面容，用户点选"更好的一张"或跳过；结果存 `pair_comparison`（winner/loser identity 对，见 DATA_MODEL §2.9）。
- 选对策略（R4 起）：
  - **diverse（默认，ADR-019）**：人物优先的多样化对比，排序 = 明显不同人物 > 不同视频 > 分差小 > 随机抖动。`different_person_confidence` 为不落库的软信号：同帧共现 1.0 > embedding 余弦 ≤0.40 高置信不同 > 中间未知 > ≥0.60 很像（跨视频同演员降级不禁止，候选不足时仍可 fallback）。
  - **similar**：base_score 接近（差值 ≤10，可配）优先，保留原语义，仅在相近候选中轻度偏向不同人物。
  - **random**：纯随机，仅排除已对比对。
- 训练接入（M6）：与绝对评分**共用同一打分头 f(x)**，pair 样本走 pairwise logistic 损失（RankNet/Bradley-Terry 式）：`P(i 胜出) = σ(f(x_i) − f(x_j))`；绝对评分样本走回归损失，两类样本联合训练（可加权）。
- 不为单 identity 维护 Elo 分（避免双分数体系）；训练前的 UI 排序仍用 base_score。
- 触发滚动更新：新对比结果入库 → 受影响 identity/video 标记 dirty（同 §3.3）。
- R4 明确：diverse 不修改 `pair_comparison` 数据模型，不建立跨视频人物关系（§3.2）。

## 4. 部署视图（M7 落地）

```
┌──────────────────────── docker compose ────────────────────────────┐
│  ┌──────────────────── sophos（单容器） ───────────────────────┐   │
│  │  FastAPI :8000 ── 静态托管 frontend/dist（Vue3 构建产物）    │   │
│  │   ├ 后台 worker 线程：扫描/抽帧/人脸/打分/训练               │   │
│  │   └ ffmpeg · onnxruntime(CPU)                               │   │
│  └─────────────────────────────────────────────────────────────┘   │
│   volumes:  {宿主视频目录}:/videos:ro    ./data:/app/data          │
└─────────────────────────────────────────────────────────────────────┘
```

- 视频目录以**只读**方式挂载，容器内路径与 `video.path` 一致（Windows 宿主需注意路径映射一致性）。
- 模型文件预置于 `data/models/`（卷挂载或首次启动下载脚本），避免镜像频繁重建。

## 5. 运行时目录布局

```
data/
├── sophos.db                  # SQLite（路径可配）
├── models/                    # ONNX 模型 + personalizer 版本目录
│   ├── det_10g.onnx / mtcnn.onnx(备选)
│   ├── genderage.onnx
│   ├── mobilefacenet.onnx (w600k_mbf)
│   ├── beauty_scut.onnx       # M4 产出/接入
│   └── personalizer/v{n}.joblib + meta.json
├── thumbs/{identity_id}.jpg   # 面容缩略图（对齐后代表脸）
├── frames/{video_id}/         # 抽帧临时目录，处理完成即清理
└── logs/
```

## 6. 扩展点（本期不做，接口预留）

| 扩展 | 预留方式 |
|---|---|
| 跨视频人物合并（全局人物库） | **R4 冻结（ADR-019）**：收益不抵复杂度，跨视频同人以多 identity 共存为预期语义 |
| GPU 推理 | onnxruntime-gpu 换包即可，provider 可配 |
| 场景检测抽帧（替代固定间隔） | frame_sampler 增加策略参数 |
| 多用户 | user_rating 增加 user 维度（当前单用户） |
