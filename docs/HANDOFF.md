# 交接文档（HANDOFF）

> **新接手者读这里**。维护规则：每个批次结束时在最上方新增一节"交接记录"；进行中的批次随时更新"当前进行中"。
> 阅读顺序：README → 本文 → DEVELOPMENT_PLAN → ARCHITECTURE / DATA_MODEL / API_DESIGN → DECISIONS。

## 当前状态快照

- **最新完成批次**：R18（v1.10.0：独立设置页 + 三档性别策略 + 全库分批重建面容），2026-10-08
- **下一批次**：部署 R17/R18；用户验收 R15-R18 浏览器交互、快速拖动与数据落库；继续验收 R11-R14（导出/导入；图片分析；同评/撤销；双池并行；播放三项；新 UI；启用任务化与进度；分数去饱和观感），并复检 R8-R10 移动端播放项
- **版本**：**v1.10.0 源码就绪，待部署**；R16 v1.9.1 已部署并通过最终容器完整回归与技术性播放压测
- **项目根**：`D:\Code\Sophos`（原 `X:\Sophos` 为 SMB 盘；backend/.venv 已重建：Python 3.11 + requirements。前端 node_modules/.bin 迁移失效，build 用 `node node_modules/vite/bin/vite.js build` 或重装 npm install）；测试片源 `\\Nas02\temp\Sophos_data`（即 Z:\Sophos_data；`fail_sample\` 为 R9 报障的 8 个样本）

## 当前进行中

R17/R18 待部署：评分/对比页可从当前面容进入同视频面容抽屉并整体/逐个评分；移动端面容抽屉返回只关闭抽屉；新增指定视频重提与全库分批重建面容 API；抽帧 ffmpeg 降优先级。R16/R15 待用户浏览器验收：时间轴、快速拖动、任务排序、视频面容评分抽屉。R14 与 R11-R13 复检项见上文。

---

## 交接记录 — R18（2026-10-08，v1.10.0）

### 背景

用户要求拆分设置项与任务列表；人脸性别提取支持仅女性/仅男性/全部；提供“重新进行人脸识别”以应用最新面容优选，且不改变已训练模型。

### 实现摘要

1. **独立设置页**：新增 Settings 页签，任务页保留目录、扫描/处理和任务控制；桌面与移动端均为七个页签。
2. **三档性别策略**：`gender_selection=female|male|all` 运行时设置；仅女性沿用 genderage+CLIP 合取，仅男性按反向阈值合取，全部跳过性别门。
3. **全库重建入口**：`POST /api/process/rebuild-faces` 将可处理视频置 pending，按现有 `process_batch_size` 分批链式重跑；任务支持暂停/取消/恢复。
4. **模型不动**：已训练模型文件和 active 版本不变。重建只替换面容派生数据；无法可靠映射到新 identity 的评分/对比会被删除，避免错绑。
5. **轻量化边界**：新面容优选和性别策略需要重新抽帧/推理/聚类才能生成新 identity；无法只从旧缩略图轻量重建。分批+低优先级 ffmpeg+任务控制是可用的减负方式。

### 验证

- 前端构建通过（仅既有 chunk size warning）。
- 目标回归覆盖设置默认/校验、male/all/female 性别裁决、重建任务分批与活动任务 409。
- 完整回归：**229 passed**（容器依赖环境）。
- 部署前停机备份：`/root/Documents/Codex/2026-10-08/sophos-v1100-deploy/sophos-stopped.db`，`quick_check=ok`，SHA-256 `224010347e7656f3941a481e8e7b313a31b22964cd757d699ca5f7b15b292d7e`；保留回滚镜像 `sophos:rollback-20261008-pre1100`。
- 部署前面容批任务 `3332` 已暂停并写入备份；模型启用任务 `3336` 已完成。
- 待部署后先用 1 个视频/批验收三档性别和重建结果，再考虑全库长任务。

## 交接记录 — R17（2026-10-08，v1.9.2）

### 背景

用户反馈：移动端播放后进行面容评分、返回时不应退出上一级页面；评分/对比页也需进入当前视频全部面容并整体评分；同时汇报面容优选落实情况，并评估能否重跑面容提取。

### 实现摘要

1. **移动端返回层级**：`FaceRatingDrawer` 打开时占用一个 history 条目；系统返回/侧滑只关闭抽屉并保留原页面。UI 内关闭不额外触发 history 返回，避免连续关闭时退出应用。
2. **评分/对比入口**：新增复用组件 `FaceRatingDrawer`。评分页和对比页均可从当前面容进入其所属视频，查看全部提取面容，逐个 1-10/好评/差评，或一次整体好评/差评；视频库继续使用同一交互。
3. **指定重提面容**：新增 `POST /api/process/reprocess`，仅接受 done/failed 且无活动处理任务的指定视频；处理层沿用既有保护——已有评分/对比的面容会被拒绝删除，未评分历史结果可安全重建。
4. **抽帧 CPU 治理**：抽帧 ffmpeg 与播放转码一致使用低优先级，避免 4K/HEVC 解码占满 CPU 造成在线页面卡顿。

### 面容优选落实结果

- 当前排序已落实为：未遮挡/低遮挡 → 正面姿态 → 更小 yaw → 更高质量；同组入库样本也按该排序保留。
- 弱代表（单样本、遮挡、非正脸、低质量）触发半间隔相位补采样；基础帧数 >900 时跳过，防止长视频成本翻倍。
- 回归锁定：高质量但遮挡的样本不会胜过干净正脸样本；弱代表才补采样。相关 18 项目标测试通过。
- 存量结果不会自动重跑；需用重提 API 逐个指定无人工信号视频。候选技术样本 ID：7、2485、13078、12925、7334（均无评分/对比，时长约 284–564s）。
- 当前部署曾恢复任务 3305，实际仍在处理 ID 13141；未部署 R17 前不能安全提交重提任务。

### 验证

- 前端 `npm run build` 通过（仅既有 chunk size warning）。
- 容器内目标回归 `18 passed`：指定重提、抽帧降优先级、干净正脸排序、相位补采样触发等。
- 待部署后用候选 ID 之一执行真实重提，并对比 phase_samples/identities/samples 与缩略图技术结果；不输出标题、路径或内容语义。

## 交接记录 — R16（2026-10-08，v1.9.1）

### 背景

用户反馈：重封装/转码视频时间轴不完整、拖动缓慢；快速拖动（含直出视频）会转圈、卡顿并连带服务卡死。同时授权持续修复“视频中存在质量更好、更正面的面容但没有截取到”的问题，并以当前数据库为基础部署测试；测试不得解析或输出视频标题/内容。

### 实现摘要

1. **播放稳定性**：转码信号量等待改为可取消轮询；客户端在首块输出前断开时立即终止 ffmpeg 并幂等释放槽位。播放器 direct 档 25 秒停滞不再误降级，仅 remux 停滞或真实 MediaError 才降级；换源前显式断开旧 `<video>` 请求。
2. **真实时长链路**：新增 `GET /api/videos/{id}/metadata`，只用 ffprobe 返回容器技术时长；扫描新增/变更视频时回写 `duration_sec`，播放器打开后异步拉取并钉住时间轴。
3. **数据库性能**：新增并幂等补建 `user_rating(identity_id,id)`、`pair_comparison(winner,loser)`、`face_identity(id,video_id)` 复合索引，覆盖最新评分、已对比过滤与轻量抽样路径。
4. **面容相位补采样**：代表为单样本、疑似遮挡、非正脸或低质量时，追加半间隔相位抽帧；基础帧数超过 `SOPHOS_PHASE_RESAMPLE_MAX_FRAMES=900` 时跳过，避免长视频成本翻倍。该策略仅影响后续处理/重处理。

### 验证

- 后端完整回归：222 passed（容器依赖环境）；前端 `npm run build` 通过（仅既有 chunk size warning）。
- 迁移幂等与补采样触发/成本护栏均有单测；断连取消含首块前断开回归。
- 最终镜像 `sha256:54153b7d8ebdf30b7ae25bdaa9f8531e15926f485cc0f7875095d9f48501b392`，API `1.9.1`，容器 healthy；保留回滚镜像 `sophos:rollback-20261008-pre191` 和停机备份 `/root/Documents/Codex/2026-10-08/sophos-r191-deploy/sophos-stopped.db`。
- 最终容器内隔离源码完整回归：**222 passed**。三个新增复合索引均存在；自动化扫描/处理保持关闭；任务 `3305` 保持 `paused`。
- 技术压测仅按技术模式选择样本并只输出 ID/指标：direct（ID 1）10 次 1 MiB Range 均为 206，耗时约 0.009–0.047s；remux（ID 25）3 次服务端 seek 首块 64 KiB 均为 200，耗时约 0.076–0.101s；transcode（ID 7）3 次首块 64 KiB 均为 200，耗时约 2.149–5.671s。
- 断开 remux/transcode 读取后等待 10 秒，ffmpeg 进程连续为 0；健康接口三次约 0.0008–0.0011s，未复现服务连带卡死。
- 待用户浏览器验收：真实时间轴显示、快速拖动/频繁换源、面容抽屉交互与评分落库；补采样只作用于后续处理/重处理。

---

## 交接记录 — R15（2026-10-08，v1.9.0）

### 背景

用户要求继续当前开发：任务列表中正在进行的任务始终排在最前面、已完成的往后排，且组内保持原顺序；视频库需支持对某个视频出现的全部面容整体好评/差评，并调出其中提取的面容逐个评分或好评/差评。

### 实现摘要

1. **任务排序**：`GET /api/jobs` 改为状态组优先（running → queued → paused → 终态），每组内继续按 id 倒序。排序回归锁定 done/cancelled 均排在活动任务之后。
2. **视频整体评价**：新增 `POST /api/faces/rate-video`，按 `video_id` 在同一事务内给全部 `face_identity` 写入同一条 thumbs 评分；无面容返回 409、视频不存在 404、非法 verdict 422，并沿用自动训练触发。
3. **视频库 UI**：桌面表格与移动卡片均提供“整体好评/差评”（先确认写入数量）；“面容/提取面容”打开抽屉，分页展示缩略图、分数、样本数与最新评价，可逐个 1-10 打分或好评/差评。
4. **版本**：APP_VERSION 1.8.1→1.9.0。

### 验证

- 新增回归：任务排序、单视频全部面容批量评价（成功、空视频、不存在视频、非法 verdict）均通过。
- 前端 `npm run build` 通过（沿用既有大包体积警告）。
- 未部署、未连接生产业务库、未在真实浏览器写入评分；这些留待用户验收。

---

## 交接记录 — R14（2026-10-07，v1.7.0 未发版）

### 背景（用户反馈 3 项）

①启用训练后的模型响应慢——应作为独立任务进列表、享独立资源池，点击即响应；②长任务（主要是 process）进度条长期不动，需要更细的进度提示；③大量视频的个性化分和综合分都是 100，难分高下——为什么？能否改进？

### 根因与定夺（ADR-035）

1. **启用/停用任务化**：原 `POST /train/activate/{v}` 同步执行 apply 全库个性化分 + 聚合重算（大库分钟级）。改为 202 + interactive 池 job（享 R13 双池优先地位）；API 同步预检（版本不存在 404、activate/deactivate 已有排队/运行 409）；job 分相进度（apply 进度回调 per 块 → "应用个性化分 x/y 张面容"；aggregator.recompute_all 增加 progress_cb → "聚合视频综合分 x/y"）。`personalizer.activate/deactivate` 增加 progress 参数（回滚恢复逻辑不变）。
2. **帧级进度**：job 表新增 `detail` 文本列（migrations.NEW_COLUMNS 补 job）；`jobs.make_detail_updater(session, job)` 节流提交（≤1 次/秒——逐帧提交会重现 R6 写锁挤占）。process："视频 2/4 xxx.mp4 第 1520/3600 帧"（抽帧/逐帧/聚类三段）；analyze 逐帧；任务页（桌面表格/移动卡片/详情弹窗）在进度条下展示 detail。
3. **分数去饱和（用户问题③的根因）**：个性化分 = clip(w·x+b, 0, 100)——pairwise 损失无界拉开分差 + 512 维 embedding 对未评分人脸**外推**，原始输出可达 ±数百（真实库实测 ||w||≈20、raw∈[-840,-41]），clip 后全库大片并列 0/100（真实库 321 张面容唯一值仅 170：==100 有 60 张、==0 有 89 张——用户截图即此）。修复 = **apply 端单调软饱和** s=50+50·tanh((z-50)/50)：保序、50 锚定、中段（25~75 分）近似恒等；实测唯一值 170→299，==100/==0 归零。**曾试验 tanh 有界训练头并否决**：ds/dz≤50 放大梯度，第一步即把 z 推出可达域且因导数饱和冻结（实测 z∈[-19,14] 1500 轮恒定）；y=0/100 的最优 z 为 ±∞ 也不可达。结论：训练端保持线性头，去饱和在应用端做（旧模型"重新启用"即生效，无需重训）。
4. **训练发散修复（顺带发现的真缺陷）**：真实小库上 LR=0.05 振荡发散（resid 50→253→1080→6450→…，十步内溢出；v1.meta 的 mae=NaN 即此因；大库碰巧收敛）。修复：LR 0.01 + **梯度范数裁剪 2.0**（w/b 联合，步长数据无关地有界）+ ITERS 3000 补偿；标准化后按维裁剪 ±10（训练池近常数维 x_std 触底 1e-6 会把库内人脸该维放大 ±1e6，一步溢出）。

### 落地清单

- **后端**：`db/models.py`（Job.detail）+ `db/migrations.py`（job 补列）、`services/jobs.py`（make_detail_updater、job_detail.detail、POOLS+activate/deactivate）、`services/aggregator.py`（recompute_all progress_cb）、`services/personalizer.py`（apply_version/activate/deactivate 进度参数、version_exists、apply 端软饱和 + LEGACY_SQUASH_SCALE、LR/裁剪/ITERS、norm 裁剪）、`services/pipeline.py`（activate_handler/deactivate_handler、process/analyze detail、单例初始化锁）、`api/train.py`（202+job、409/404 预检）。
- **前端**：`TrainView.vue`（启用/停用任务化 + 进度横幅 + 刷新恢复）、`TasksView.vue`（detail 展示 ×3 处）。
- **版本**：`APP_VERSION` 1.6.0→1.7.0；ADR-035；README 功能/进度/页签同步。

### 验证

- pytest **213 passed**（新增：activate/deactivate 任务化 API 流与 409、apply/recompute 进度回调终值、detail 节流、有界不并列、旧线性模型 squash 保序去并列；迁移测试补旧 schema job 表）。
- 真实库验证：训练不再发散（mae=0.04，0.06s）；去饱和对比（唯一值 170→299，==100 60 张→1 张、==0 89 张→5 张）。
- `npm run build` 通过。

### 已知限制与未尽事项

- 软饱和会整体重排旧模型的绝对分（保序；中段 ±25 分内近似恒等，90 分以上被拉开）——用户重启用/重训后生效；训练页建议提示重训。
- detail 列每秒最多落库一次（节流），任务页刷新间隔 2s 下足够；终态保留最后一次 detail 便于诊断。
- activate/deactivate 的 409 互斥：同一时刻只允许一个启用/停用任务（防交错回滚）。
- 停用（deactivate）同样分钟级，已一并任务化——用户需求只提了启用，属同径顺带。

### R14.1 补丁（v1.7.1，用户复检反馈：重新部署后仍见大片综合分/个性化分 100）

- **根因**：`apply_version` 的 R5 幂等优化（`pers_model_version == version` 的行跳过）——修复**前**激活已把旧语义分数（并列 100.0）stamp 到全库；重新部署后重新启用同版本，所有行被跳过、软饱和从未执行。
- **修复**：移除该跳过，无条件全量重算（activate 已任务化，进度可见）；有回归测试锁定（stamped 100.0 的行重启用后被清除）。
- **版本**：APP_VERSION 1.7.0→1.7.1。用户侧操作：部署本补丁后在训练页对当前模型**重新点一次"启用"**（或重训/启用新版本），全库分数即按去饱和语义重算。

### R14.2 修订（v1.8.0，用户复检 2 项：新训练模型不入列表；"从普遍 100 变成普遍 99.99"未根治）

- **去饱和定稿 = 排名百分位**：软饱和 squash 治标——病态模型 raw 散布 ±数百，(raw-50)/50 后 raw>250 全部并列 99.99（用户复测证实）。apply_version 改**两遍式**：分块扫描全库 raw（不写库）→ 全局 stable argsort 排名 → 分块写入 `score = 0.01 + rank/(n-1)·99.98`（4 位小数）。分布无关、严格保序、铺满区间；语义 = **库内偏好百分位**（越高越合口味，随库规模滚动变化属预期，需在 README/训练页向用户说明）。tanh50 分支与 LEGACY_SQUASH_SCALE 移除。进度分两相（扫描 x/y → 写入 x/y），activate_handler 总量 = 2×ident + n_vid。
- **新模型不入列表**：本地无法复现（真实目录 v1-v4 列表正常、训练→列表链路正常）。已加防护/可见性：train 版本号解析 `isdigit` 防护（目录混入手工文件不再崩溃）、训练完成 `log.info`（版本号+写入路径，容器日志可查）、TrainView 训练完成后校验 result.version 在列表中，缺失则明示"检查 data/models 卷挂载与容器日志"（最可能是 Docker 卷挂载未含 personalizer 子目录或训练任务 failed——任务页/训练页告警可见）。另注意：R13 双池下 interactive 单 worker，activate 长任务期间提交的训练会排队（任务页可见 queued），属预期。
- **版本**：APP_VERSION 1.7.1→1.8.0。

### R14.3 修复（v1.8.1，用户定位根因：v10+ 排序问题）

- **根因确认**：`list_versions` 按文件名字典序排序——`v10.meta.json` 排在 `v2` 前，反转（新版本在前）后 v10+ 被挤到列表尾部，看起来像"新训练的模型没出现在列表中"。版本**编号**（train 的 max+1）一直是数字递增，无碰撞/覆盖，仅展示序错误。
- **修复**：按数字版本号排序（正则从完整文件名提取，`^v(\d+)\.`）——**注意 `Path.stem` 只剥最后一个后缀**（"v10.meta.json".stem == "v10.meta"，首轮修复即栽在此：isdigit 恒 False、排序退化回字典序，回归测试当场拦截）；无 version 字段的手工杂项不入列表（不可激活/导出）。
- **版本**：APP_VERSION 1.8.0→1.8.1。pytest 215 passed（新增排序回归测试：v1/2/9/10/11 + 杂项文件）。
- **验证**：pytest 214 passed（有界/唯一/保序/进度终值更新为两相）；build 通过；真机：病态旧模型 v3（raw ±800）启用后个性化分铺满 [0.01, 99.99] 且两两不同。

---

## 交接记录 — R13（2026-10-07，v1.6.0 未发版）

### 背景（用户反馈 5 项）

①（部署后找不到新功能——部署/缓存问题，用户确认解决后跳过）；②外部分析应高优先级：允许插队或专用线程，最好同时进行两个任务（常态 scan/process 长跑，偶尔 train/analyze 插入，希望立刻被处理，可放弃进行中的 process 或给 train/analyze 单独资源池）；③评分/对比页按钮变多有点丑；④重封装视频进度条仍随缓冲增长；⑤直出视频拖动后偶发卡死、连带其他视频转圈、最后无法播放。

### 根因与定夺（ADR-034）

1. **双资源池**：jobs.py 单 worker FIFO 是"analyze 排在整批处理后面"的根因。改为 batch（scan/process）/ interactive（train/analyze）两池两线程：交互任务提交即执行并与长批**并行**（即"同时进行两个任务"）；用户提的"放弃进行中 process"备选不采用——池隔离已满足优先级且无中断代价。并行安全复核：引擎单例为无状态 ORT 会话（线程安全）、WAL 读写并发、`get_face_engine`/`get_scorer` 惰性初始化加锁防双线程竞态；CPU 由 R12 的 ORT 线程上限与 ffmpeg 降优先级兜底。池内仍串行（analyze 409 防重、process 批链、scan/process 互斥全保留）；pause 恢复回原池；job_detail 下发 `pool` 字段。
2. **流输出停滞看门狗**：直出拖动卡死→连带其他视频无法播放的机制：某路流因源盘/NAS 停滞长时间不产字节，(a) 转码档**无限期占住唯一的转码槽**→后续全部 503/长转圈；(b) 停滞的 FileResponse 读占住 anyio 线程→容量不足时拖垮所有同步端点。修复：`iter_ffmpeg_pipe` 加看门狗线程，连续 30s 无输出即 kill→StreamFailure→槽位立即归还（正常转码每 ~2s 关键帧必有输出，不误杀；阈值可调 NO_PROGRESS_KILL_SEC）；anyio 线程池 64→128 加余量。
3. **起播时长回填**：remux/渐进流进度条钉真实总时长（R10）依赖 DB `duration_sec`，老行缺失（扫描期 ffprobe 失败/列晚于数据）时前端拿不到 duration，进度条退回"缓冲多少显示多少"（用户复测仍报）。`_load_playable_video` 起播时惰性 ffprobe 回填（与 vcodec/container 回填同模式，一次写库永久生效）。
4. **UI 重排**：评分页——主操作（👍/👎 大按钮）一行，跳过/撤销降为次级文字链接；对比页——策略选择+（换一对/撤销）一行，"同时好评/同时差评"主操作移到两张卡下方居中并附使用说明。

### 落地清单

- **后端**：`services/jobs.py`（POOLS 映射、双队列双线程、resume 回原池、pool 字段）、`services/pipeline.py`（单例初始化锁）、`services/streamer.py`（NO_PROGRESS_KILL_SEC 看门狗、probe_duration）、`api/videos.py`（duration_sec 惰性回填）、`main.py`（线程池 128）。
- **前端**：`RateView.vue` / `PairView.vue`（布局重排）。
- **版本**：`APP_VERSION` 1.5.0→1.6.0；ADR-034；README 功能/进度表同步。

### 验证

- pytest **207 passed**（新增 3：双池并行集成测试（batch 长任务占住 worker 时 interactive 立即完成）、停滞看门狗（无输出进程 5s 内被杀并 StreamFailure）、起播时长回填）；全量含既有 scan/train/analyze 端到端（现分别跑在两池上）。
- `npm run build` 通过。

### 已知限制与未尽事项

- 双池并行时 CPU 竞争由 ORT 线程上限（核数-2/每会话）与 ffmpeg 低优先级兜底；弱机上 analyze+process 并行仍可能双双变慢（不再卡死 UI）——需要更彻底的隔离可再把 ORT 线程上限调小。
- 停滞看门狗 30s 阈值按 2s 关键帧间隔估算，极端慢的合法转码（<0.07× 实时）理论上可能误杀——`NO_PROGRESS_KILL_SEC` 可调大。
- duration 回填只发生在起播时（逐个惰性）；批量回填可走重扫描。
- 直出拖动的原生 Range 语义不变（服务器 FileResponse）；停滞看门狗覆盖 remux/transcode 管道流，direct 档无 ffmpeg 无槽位、停滞由前端 R7 无进展看门狗自动降级转码兜底。

---

## 交接记录 — R12（2026-10-07，v1.5.0 未发版）

### 背景（用户需求 4 项）

①对比页提供"同时好评/同时差评"；②对比/打分提供"撤销最近一次、重来"；③仍有不定期卡死，尝试修复；④有更好正脸帧时仍可能截取不太好的面容，尝试改进。

### 根因与定夺（ADR-033）

1. **卡死实根（本次最重要发现）**：`stream_video` 是 async 端点，但其预检 `_load_playable_video` 在**事件循环上**做 DB 查询 + vcodec/container 惰性探测回写——后者会触发 **ffprobe 子进程（NAS 片源可达数秒）与网络文件头读取**。一旦命中缺元数据的视频起播，整个事件循环被卡 = 所有请求无响应，与"不定期卡死"完全吻合。修复：预检移入 `run_in_threadpool`（hls 端点是 sync def，本就跑在线程池，不受影响）。
2. **WAL 三态（auto/true/false，默认 auto）**：项目已迁本地 D: 盘，回滚日志模式下写提交的 EXCLUSIVE 锁阻塞读——处理/训练/应用模型期间浏览卡顿。auto 按 Windows `GetDriveTypeW` 判定 DB 所在盘为本地固定磁盘（DRIVE_FIXED）即启用 WAL（SMB/NFS 自动排除，M2 实测决策不破坏）；`SOPHOS_DB_WAL=true/false` 可显式覆盖。`synchronous=NORMAL` 随 WAL 启用。
3. **CPU 让步**：全部 6 个 ORT 会话（检测/特征/性别/头姿/CLIP/颜值）原来用默认线程配置吃满所有核——新增 `ort_session_options()` 统一限制 intra-op 线程（默认核数-2，`SOPHOS_ORT_INTRA_THREADS=0` 自动）；jobs worker 线程降为 BELOW_NORMAL（Windows，GetCurrentThread 伪句柄须在 worker 线程内调用；POSIX nice 为进程级，不可线程级设置，由 ORT 上限兜底）。
4. **撤销语义**：`POST /api/faces/undo` 跨表按 created_at 取**全局最近一条**删除；返回 kind=rating（含 identity_id/type/value，评分页把该面容放回队首重评）或 kind=pair（含 winner/loser，对比页重新摆出）。**utcnow 升至微秒精度**——秒精度下同秒内"先评分后对比"无法排序（与旧数据字典序兼容：同秒内 ".fraction" 排在 "+" 后）；评分/对比只在训练时被读取，撤销无需重算任何分数。
5. **同时好评/差评**：`POST /api/faces/pair/rate-both` {identity_ids:[a,b], verdict} 一事务两行 thumbs 评分，前端成功后自动换下一对。
6. **rep 更正脸优选**：用户反馈"有更好正脸帧却截了不太好的面容"两个成因——(a) `_sample_rank` 同姿态档位内原来只比质量，15° 与 44° 两个 near 可能因质量微差选出明显更侧的脸 → 同档内先比 |yaw| 再比质量；(b) 缩略图用 timestamp÷间隔 近似回推帧下标，抽帧数与估计不符（短视频/fps 怪癖）时截到别的一帧 → FaceSample 新增 `source_frame`，process_frame/analyzer 记录来源帧，`_persist_groups`/analyze_video 缩略图按 rep **实帧**取景（无 source_frame 的旧路径退回近似）。**注意**：跨 identity 碎片（更好正脸帧聚进了另一个 identity）仍由 merge pass ADR-018/028 治理，本轮不动。

### 落地清单

- **后端**：`api/faces.py`（rate-both/undo）、`api/videos.py`（stream 预检线程池）、`db/session.py` + `config.py`（WAL 三态 + `_wal_enabled`/`_db_on_fixed_disk`、`ort_intra_threads`）、`services/face_engine.py`（`ort_session_options()` ×6 会话、`FaceSample.source_frame`、`process_frame(source_frame=)`）、`services/pipeline.py`（`_sample_rank` 细化、实帧缩略图、source_frame 透传）、`services/analyzer.py`（同口径两处）、`services/jobs.py`（worker 降优先级）、`db/models.py`（utcnow 微秒）、`services/scorer.py`（会话选项）。
- **前端**：`PairView.vue`（同时好评/差评/撤销上一条；撤销对比重新摆对）、`RateView.vue`（撤销上一条；撤销评分该面容回队首）。
- **版本**：`APP_VERSION` 1.4.0→1.5.0；`docs/DECISIONS.md` ADR-033；README 功能/进度/页签描述同步。

### 验证

- pytest **204 passed**（新增 7：rate-both 端点与非法输入、撤销跨表顺序三轮、同秒"先评分后对比"撤销命中对比、WAL 三态解析、本地盘 WAL journal_mode 实测、_sample_rank 更正脸优选、source_frame 缩略图兜底路径）。
- `npm run build` 通过；真机冒烟见 R12 会话记录（WAL 文件生成、rate-both+undo 往返、健康检查 1.5.0）。

### 已知限制与未尽事项

- 撤销只删最近一条记录：若同一面容被多次评分，撤销最后一行后更早的一条重新生效（"最新一条为准"语义下符合直觉）。
- POSIX 上 `_db_on_fixed_disk` 恒为 True（无可靠 NFS 判定）——NFS 放 DB 的部署须显式 `SOPHOS_DB_WAL=false`。
- worker 降优先级仅 Windows；ORT 线程上限会让处理吞吐略降（约核数比），可按机器用 `SOPHOS_ORT_INTRA_THREADS` 调回。
- 跨 identity 碎片导致的"更好正脸帧在另一张卡上"不在本轮范围（merge 阈值治理，见 ADR-018/028）。

---

## 交接记录 — R11（2026-10-07，v1.4.0 未发版）

### 背景（用户需求 3 项）

①训练模型导入/导出——"维持轻量级的导入/导出处理，重点是保存用户训练出来的偏好"；需求经复述确认：导入不自动启用，meta 仅记录训练指标（对比数/训练时间/结果），不记录导出时的激活状态。②"分析"功能上传较大视频报 `Request failed with status code 413`。③分析功能允许上传图片。

### 根因与定夺（ADR-032）

1. **413 根因**：`analyze_max_upload_bytes` 应用层默认 2GiB（上传端点自检，非框架行为），大视频必然超限。改默认 **0=不限**（上传本就流式落盘不占内存）；`SOPHOS_ANALYZE_MAX_UPLOAD_BYTES` 可设上限；413 报错文案自带"如何放开"提示。
2. **导出/导入（轻量语义）**：偏好本体 = 线性打分头 `w/b/x_mean/x_std` + 训练指标（几 KB）。导出 `GET /api/train/versions/{v}/export` → 单个 zip（{v}.npz + {v}.meta.json）；导入 `POST /api/train/import` 校验（zip 结构 / npz 数组齐全 / 维度自洽 / meta 合法 version / **特征维度==FEATURE_DIM(514)**，跨特征定义的包直接拒绝防写坏分）→ 落盘为新版本（版本号沿用包内原号、冲突重编号 max+1，不覆盖本地）；**不自动启用**。
3. **顺带发现的真实缺陷**：真实库 v1.meta.json 含 NaN 指标（极端训练产物），而 Starlette JSONResponse `allow_nan=False` → 带该版本的 `GET /train/versions` 会 500。`list_versions`/导出统一 NaN→None 清洗。
4. **图片分析**：整图即一帧——PIL 解码 + **EXIF 方向转正**（cv2 不处理旋转标记），PIL 打不开退回 cv2.imdecode；检测/门控/聚类/打分/人像缩略图与主链路同口径（分数可比）；**跳过 merge/cap**（单图样本 timestamp 全 0，同帧共现否决 ADR-018 本就禁止任何合并，显式跳过避免语义混淆）；隔离语义不变。结果 JSON 新增 `media_type`（video/image），历史列表同步下发。

### 落地清单

- **后端**：`personalizer.py`（`FEATURE_DIM` 常量、`export_version`/`import_version`、`_json_safe`、`_load` 版本号防穿越守卫）、`api/train.py`（export/import 端点，400 INVALID_EXPORT / 404）、`config.py`（`analyze_max_upload_bytes=0`）、`api/analyze.py`（上传收图片扩展与 image/*、inbox 列表含图片 + `kind` 字段、415 分型 UNSUPPORTED_MEDIA_TYPE）、`analyzer.py`（`IMAGE_EXTS`、`_decode_image`、`analyze_image`、media_type）、`pipeline.py`（analyze handler 按扩展分发）。
- **前端**：`TrainView.vue`（每版本"导出"= blob 下载；头部"导入"= el-upload 上传 zip，成功后提示手动启用）、`AnalyzeView.vue`（accept 加 image/*、结果卡"图片"徽标并隐藏时长/帧数/时间戳、历史列表图片徽标）。
- **版本**：`APP_VERSION` 1.3.1→1.4.0；`docs/DECISIONS.md` 新增 ADR-032；README 功能/进度表同步。

### 验证

- pytest **197 passed**（新增 8：导出/导入 roundtrip（含"导入不自动启用、手动启用生效"）、冲突重编号、非法包多类拒绝、API 层导出/导入、版本列表 NaN 兼容；分析图片上传/隔离/删除、损坏图片任务 failed 分型）。
- **环境性 flake 观察（与本批改动无关）**：新 venv（Py 3.11）下全量跑 5 次中观察到 2 次单用例偶发失败且每次不同（`test_automation_tick_daily_scan_and_process` 线程计时 / `test_analyze_delete_rejects_symlink_and_malicious_result_paths`），隔离重跑 3 次均过、复跑全量全绿——均为负载下时序敏感，留待后续观察。
- `npm run build` 通过（vite 以 `node node_modules/vite/bin/vite.js build` 直调，.bin 迁移失效）。

### 已知限制与未尽事项

- 特征定义（FEATURE_DIM=514）变更时必须同步改常量并考虑旧导入包的拒绝路径（已在 personalizer.py 注释标明）。
- 图片分析不跑 merge/cap：拼图类图片中同一人出现两次会得到两张面容卡（同帧共现否决的正确语义）。
- 性别门全拒时图片与视频分析一样直接 0 张（分析链路无主库的 R6 保底逻辑，维持口径一致）。
- 导出 zip 内 meta 为严格 JSON（NaN 已清洗），第三方工具可安全读取。

---

## 交接记录 — R10（2026-09-22，v1.3.1 未发版）

### 背景（用户复检反馈 5 项）

①移动端进视频库页面后"宽度变化"，底部 TabBar 入口显示不全；②需要转码的视频速度较慢，且从头播放时"缓冲多少就显示多少总长"；③拖动进度条可能导致应用卡死；④应用周期性失去响应（疑似与视频播放行为相关）；⑤一个视频转圈可能连累其他视频也转圈。

### 根因与定夺（ADR-031）

1. **TabBar 显示不全 = 分页条把文档撑宽**：视频库分页条 `layout` 含 `sizes, total`，Element Plus `.el-pagination` 默认单行不换行；列表加载完成（pager 变宽 + "共 N 条"就位）后分页行必然超出窄屏 → 文档可横向拖动 → fixed 定位的 TabBar 错位。修复：移动端分页布局收窄 `prev, pager, next`（`pager-count=5`，总数筛选行已有）+ `.el-pagination` 换行兜底 + `.app-shell.mobile { overflow-x: clip }`（不产生滚动容器，sticky/fixed 均不受影响）。
2. **总时长=已缓冲长度**：HLS event playlist 与渐进 fMP4（empty_moov）的 `video.duration` 只覆盖已转码/已缓冲部分。修复：`video.duration_sec`（ffprobe 实测）经视频列表/faces API（新增 `duration_sec` 字段）下发，播放器 `open({duration})` 接收后以**实例属性 getter + noop setter** 覆盖 `video.duration`（纯 getter 在严格模式下赋值抛 TypeError，会打断 hls.js 内部 duration 写入）；ENDLIST 探测改判"seekable 未覆盖真实总时长才 VOD 重载"。
3. **拖动会话风暴**：HLS 原生 seek/热切换的前沿容差 0.5s→4s（≈2 个切片），且 `onSeeking` 原生路径与 `hotSwapTo` 同一容差——小幅越界拖动交还原生 seek（Safari pending seek/hls.js 等前沿推进），不再每 250ms 杀会话重启转码。
4. **转码起播慢**：`_HLS_TIME_SEC` 4→2 + 转码档 `-force_key_frames expr:gte(t,n_forced*2)`（不强制时 x264 默认 GOP ≈10s，HLS 只能按关键帧切——首片可长达 10s 内容）；转码命令（渐进+HLS）加 `-tune zerolatency`。首片等待从最长 ~10s 内容降到 ≈2s。
5. **整机响应/连带转圈**：播放/切片 ffmpeg 子进程降优先级 `streamer.spawn_kwargs()`（Windows `BELOW_NORMAL_PRIORITY_CLASS`；POSIX `os.nice(10)`）——转码只吃"剩余"CPU，Web 服务不再被抢占失响；hls.js 撞 503 STREAM_BUSY 明示"转码通道被占用，等待中"，不再默默转圈。

### 落地清单

- **前端**：`VideosView.vue`（移动端分页布局 + open 透传 duration）、`App.vue`（overflow-x clip + 分页换行）、`PlayerDialog.vue`（`realDuration` 注入/pin、ENDLIST 探测改判、`HLS_SEEK_OVERSHOOT_SEC=4` 双路容差、503 提示）、`RateView.vue`/`PairView.vue`（透传 duration）。
- **后端**：`streamer.py`（`spawn_kwargs()` + 渐进转码 `-tune zerolatency`）、`hls.py`（`_HLS_TIME_SEC=2`、转码强制关键帧 + zerolatency、Popen 降优先级）、`faces.py`（`_item` 新增 `duration_sec`）。
- **版本**：`APP_VERSION` 1.3.0→1.3.1；`docs/DECISIONS.md` 新增 ADR-031。

### 验证

- pytest 全绿（转码命令变更由 `test_hls_transcode_scale_cap_applied`/`test_build_ffmpeg_cmd_with_start_sec` 等既有用例锁定语义，无契约变更；faces API 新增字段向后兼容）。
- `npm run build` 通过（dist 已更新）。

### 已知限制与未尽事项

- `?ss=` 起播/热切换后进度条起点为 0（时间轴仍按流内计），ArtPlayer 无时间轴偏移概念，本轮维持既有行为（真实总长已正确，比例在 ss>0 会话内自洽）。
- `duration_sec` 缺失（扫描期 ffprobe 失败）的视频退回"缓冲多少显示多少"旧行为（pin 不生效）。
- ffmpeg 降优先级后，若机器同时跑处理流水线，播放转码会让位给流水线——属预期取舍。
- POSIX `preexec_fn` 分支为尽力而为（主部署形态 Windows 无此顾虑）。

---

## 交接记录 — R9（2026-09-21，v1.3.x 未发版）

### 背景（用户复检反馈 2 项）

①"仍然有视频播放失败"——用户在 `Z:\Sophos_data\fail_sample` 放了 8 个播放失败的样本；②"页面不定期失去响应，可能是由播放视频失败/反复播放视频/快速退出播放视频等相关问题引起"。**约束：只从播放器角度分析，不得识别视频内容**（本次全程只读容器/编码元数据，未做任何抽帧/面容处理）。

### 根因与定夺（ADR-030，全部本机实测）

1. **播放失败主根因 = 扩展名骗人**：8 个样本文件头均为 `0x47` 同步字节（@0/188/376），ffprobe `format_name=mpegts`——**全部是 MPEG-TS 容器却叫 `.MP4`**（下载工具按来源站点后缀改名）。旧 `decide_mode` 只看扩展名 → 判 `direct` → FileResponse 把 TS 字节流当 mp4 直出 → 浏览器必然 MediaError → 前端降级转码（含 1080p50 与 **4K** 源）。修复 = **不信扩展名，看文件头**（见 ADR-030）。修复后这 8 例全部走 **remux**（`-c copy`，CPU≈0，秒开）。
2. **页面失去响应（4 条独立机制，全部与"播放失败/反复播放/快速退出"吻合）**：
   - **4K 转码打满 CPU**：4K 源实时 h264 转码单路即可占满整机（Web 服务一起僵死）→ 转码**分辨率封顶 1080p**（`SOPHOS_TRANSCODE_MAX_HEIGHT`，渐进档与 HLS 档同规格，不放大）。
   - **HLS 登记锁内等信号量**：`get_or_start` 持全局锁等最长 15s，期间**其他视频、其他 token 的全部请求**（含秒开的 remux）被串行阻塞 → 等待移到锁外。
   - **槽位与线程被占满**：转码**正常播完**的会话原先还占着转码槽到 120s 心跳超时（"看完立刻点下一条"要等 15s→503）；被顶替/已死亡的会话 `wait_playlist` 要等 ffmpeg 真退出才返回（单请求最长占同步线程 20s，连发即拖垮线程池）→ 进程退出即还槽 + 顶替立即快返（409 STREAM_SUPERSEDED，不打 error 日志）+ anyio 线程池 40→64 兜底。
   - **前端泄漏与不及时回收**：hls.js 动态 import 竞态（关闭后仍建实例 → 永久泄漏一个拉切片的 hls.js + 拖着服务端 ffmpeg）；`open()` 未先拆上一实例（反复播放每次泄一份）；快速退出只能等服务端 120s 心跳（转码进程继续吃满 CPU）→ 新增 **`DELETE /api/videos/{id}/hls/{token}`** 显式结束会话（前端关闭/换视频/热切换即调用，`keepalive`），并加实例拆除与滚动锁幂等。

### 落地内容

- **后端 streamer**：`sniff_container()`（前 600B 魔术字节；mp4/mov/webm/matroska/mpegts(含 192B M2TS)/avi/flv/ogg/mpeg/asf；不做子进程，SMB 友好）；`decide_mode(..., container=)` 实测容器优先、未知回退扩展名；`build_ffmpeg_cmd(..., max_height=)` 转码 `-vf scale='min(iw,1920)':'min(ih,1080)':force_original_aspect_ratio=decrease:force_divisible_by=2`。
- **DB/扫描**：`video.container` 列（models + migrations 补列）；扫描期探测新列并**回填存量空值**（借用一次扫描，免用户额外操作）；列表端点本页惰性补探测、播放端点补探测（老库无需重扫即可播）。
- **HLS**：`get_or_start` 槽位等待移出登记锁（并发同 token 复用兜底）；`wait_playlist` 顶替快返；`touch`/janitor 进程退出即还槽；`stop_session()` 显式回收；`build_hls_cmd` 分辨率封顶。
- **API**：列表/详情新增 `container` 字段；`DELETE /api/videos/{id}/hls/{token}`（204/400/幂等）；HLS playlist 顶替场景 409 STREAM_SUPERSEDED。
- **前端 PlayerDialog**：`teardownPlayer()`（open 先拆、close 共用；pause+destroy+清空容器）；attachHls 的 import 竞态守卫（`state.open`/epoch 双检）；`hlsToken` 跟踪 + `stopHlsSession()`（DELETE，keepalive）在关闭/降级/手动重试/热切换四处调用；滚动锁幂等（`scrollLocked`）。
- **config/.env.example**：`transcode_max_height`（默认 1080，0=不限）。
- **main.py**：lifespan 内 anyio 线程池上限 40→64（拿不到限流器时仅告警）。

### 验证

- **真实样本端到端**（`scripts/probe_playback_r9.py [样本目录]`，临时 DB/端口，仅元数据）：8 样本列表全部 `container=mpegts / mode=remux`；渐进 remux 流 3.1MB 可解析（ffprobe: h264 1920x1080 + aac）；HLS playlist + 切片（sync 0x47、**Range bytes=0-1 → 206**）；DELETE 会话 204；最大样本（4K）`?fallback=1` 转码输出 **1920x1080**（封顶生效）。真实库 `Z:\Sophos_data` 已扫描一次：30 条视频 `container` 全部有值（22 matroska + 8 mpegts），8 个报障样本登记为 `remux` 档（`pending`，未做面容处理——本次仅播放链路）。
- **pytest 181 passed**（新增 `test_stream_container.py` 8 例：魔术字节/TS 冒名/容器优先/端到端 remux/列表回填/封顶命令与实转；`test_videos_hls.py` +5：显式停止与槽位归还/播完还槽/顶替快返/转码等待不阻塞 remux/封顶命令）。顺带修 `test_health` 过期版本断言（1.2.x→1.3.x）。
- 前端 `npm run build` 通过（dist 已更新）。

### 已知问题与未尽事宜

- **存量库需要一次扫描或首次播放**才会补上 `container`（列表/播放端点已惰性回填，不阻塞使用）。
- 4K 源在弱 CPU 上转码仍需数秒起播（封顶后 CPU 压力大幅下降，但"实时转码"本身有极限）。
- 顶替快返为 409 属正常竞态（前端按 epoch 静默丢弃）；若在 UI 上看到该错误提示，说明前端 epoch 守卫失效，需排查。
- 真机（iOS Safari / Android Chrome）复检仍待用户：R8 三题 + R9 两组。
- 临时探针脚本 `scripts/probe_playback_r9.py` 保留（用法见其 docstring），验收通过后可删。

### 下一步入口

1. 用户复检：`fail_sample` 那 8 条（以及库里其他同类）是否秒开、反复播放/快速退出是否还卡；真机移动端一并复检。
2. 若仍有零星失败的源，用 `sniff_container`+`probe_codec` 先看容器/编码判定是否符合预期（`scripts/probe_playback_r9.py` 可作模板），再查 `stream_mode`。
3. 阈值入口：`SOPHOS_TRANSCODE_MAX_HEIGHT`（0=不限）、`SOPHOS_TRANSCODE_MAX_CONCURRENCY`、`SOPHOS_TRANSCODE_ENABLED`。
4. 复检通过后可并入 v1.3.0 tag（R8/R9 同批）或发 v1.3.1。

---

## 交接记录 — R8（2026-09-20，v1.3.0）

### 背景（用户复检反馈 3 项）

①移动端播放 TS/H264 视频报"视频播放失败：源文件不存在或格式不支持"；②播放器无法正确全屏（控件与屏幕大小不匹配、页面可被拖动）；③按返回键应退出播放器，而非直接退出网页。

### 根因与定夺（ADR-029，全部本机复现）

1. **传输层无 Range 是 ① 主根因**：iOS Safari 起播前发 `Range: bytes=0-1` 探测并要求 206；`/stream` 的 fMP4 管道流是 200 + chunked → remux/transcode 在 iOS 双双失败（MediaError 4），`?fallback=1` 降级仍是同一无 Range 传输层，救不了。curl 实测复现（`Range: bytes=0-1` → `200 OK`）。
2. **方案 = HLS 会话（Jellyfin/Emby 同款架构）**：新增 `services/hls.py` + `GET /api/videos/{id}/hls/{token}/index.m3u8?ss=&fallback=` + 切片 FileResponse 下发（天然 206）。移动端非 direct 档走 HLS：iOS Safari 原生 HLS，其余浏览器 hls.js（npm 新依赖，独立 chunk 按需加载）；direct 档 FileResponse 本就有 Range，保持渐进直出；**桌面渐进三档完全不动**。会话生命周期：event 型 playlist 轮询即心跳，看门狗 120s 无拉取 kill ffmpeg、异常退出补写 ENDLIST、目录 TTL 15min；同视频新 token 挤旧会话；全局上限 4；转码档复用 streamer 并发信号量。seek：playlist 边界内交原生 seek，超出转码前沿才换新会话（沿 `?ss=` 语义）。
3. **转码兼容性实测定稿三连（③之外的新发现，②①均涉及）**：
   - 切片格式定 **MPEG-TS**：ffmpeg 的 fMP4-HLS 输出（init 带 edts/sidx）被 Chromium MSE 误拒——hls.js 344 次 append 全部 SourceBuffer error，同一内容的连续 fMP4 渐进流却可正常解码；TS 切片是 iOS 原生 HLS 经典形态 + hls.js 最成熟路径 + Jellyfin 转码默认。
   - 转码补 `-pix_fmt yuv420p`：**10bit 源默认转出 H.264 High10，全浏览器不解**——这是 **R5 起就存在的桌面渐进转码老缺陷**（x265 10bit mkv 转码后依旧播不了，probe 实锤 profile=High 10）。
   - HLS 转码补 `-ac 2`：5.1 AAC 经 hls.js 重封装进 MSE 被拒（audio SourceBuffer error 循环，实测）；网页/移动客户端按 Jellyfin 惯例下混立体声（渐进档实测 5.1 可播，保持不变）。
4. **前端播放器治理（②③）**：播放器高度 `100dvh`（移动浏览器动态地址栏下与可视区精确匹配）；滚动锁改 body `position:fixed + 负 top`（iOS 对 `overflow:hidden` 免疫，全屏播放时页面仍可拖动的根因），关闭时还原滚动位置；返回键 = 打开时 `history.pushState` 占位，popstate 关闭播放器而非退出网页，UI 内关闭时 `history.back()` 消费占位（真机语义：Android 返回手势 / iOS 侧滑都走 popstate）。另加原生 HLS 分支的 **ENDLIST 探测→VOD 重载**兜底：部分 Chromium 系内核自带原生 HLS 把 event 流当纯直播（不设 duration、seekable 为空、原生 seek 全被丢弃、playlist 高频轮询），探测到 ENDLIST 后同 URL 重载即恢复完整 VOD 语义（duration/seekable 实测恢复 30/30）。

### 验证

- 后端 pytest **168 passed**（新增 `test_videos_hls.py` 12 例：token 校验/404 分型/playlist 与切片/206 Range 回归/ss+fallback/会话挤占与复用/看门狗回收/ENDLIST 补写/启动清理）。
- 浏览器实测（内置 Chromium，390×844 手机视口）：假 TS（H264 High@L5 1080p50 + AAC）HLS remux 播放、拖动、全屏铺满（overlay 390×844 精确匹配）、返回键关播放器且页面不退出、播放器内返回按钮正确消费占位历史；10bit HEVC mkv 的 HLS 转码在 hls.js 路径实时播放并领先缓冲（1080p 实时转码 + 边界内 seek 落点准确）；桌面视口渐进三档回归通过（含此前被 High10 缺陷影响的转码档）。
- 已知边界：内置 Chromium 的**原生 HLS** 对 EVENT 转码流在 ENDLIST 出现前可能无法起播（该内核怪癖，VOD 重载兜底在转码完成后自动恢复；真实手机 iOS Safari/Android Chrome 不走这条路径）；HLS event 流的转码推进期，进度条总时长随转码增长（与渐进转码一致）。

---

## 交接记录 — R7（2026-09-19，v1.2.x 未发版追加）

### 背景（用户复检反馈 3 项）

①移动端播放希望是**专门的全屏播放器**（而非弹窗套壳）；②评分/对比页点击播放"跳到面容出现处"不生效，从头播放；③播放时有时仍转圈圈（附媒体信息：TS 容器，H264 High@L5.0 1080p25 + AAC LC）。

### 根因与定夺（三个独立根因 + 一个 seek 基准缺陷，ADR-029）

1. **TS+AAC remux 必死（③主根因）**：TS 容器的 AAC 是 ADTS 裸流，ffmpeg `-c copy` 封 fMP4 报 `Malformed AAC bitstream detected` 并立即中止——实测整条响应只剩 ftyp+moov 头 **~1.2KB**（生产同款命令复现）。浏览器收到只有头没有数据的流**不触发 MediaError 而是永远转圈**。修复 = remux 命令补 `-bsf:a aac_adtstoasc`（mkv 的 AAC 本就是 ASC，该 bsf 透传无副作用，TS+mkv 双向实测）；修复后同一响应 6.5MB 完整 fMP4。
2. **open() 未同步 artEpoch（②直接根因之一，R5.2 引入）**：`state.epoch`/`artEpoch` 双计数在 open() 只增前者——direct 档 startAt 起播 seek 的 epoch 守卫永远不成立，seek 被静默丢弃（mp4 从头播放）；seek 拦截器的同款守卫也误伤。
3. **Chrome 对 fMP4 管道流 seekable=[0,0]（②"拖动回开头"的根因，R5 设计缺陷）**：`empty_moov` 头 duration=0，Chrome 判定整条流不可 seek——拖到缓冲区外的目标在原生 seeking 事件前就被**钳制到 0**（事件序列实测 `seeking@0.00`），R5 的"原生 seeking 拦截"拿不到真实落点 → 浏览器发出注定失败的 Range 请求 → 整流重载回开头。修复 = 改在 **ArtPlayer `seek` setter 的 `emit("seek", clamped, intended)`** 拦截（第二参数是未钳制目标），带 `?ss=` 热切换；原生钩子保留兜底不钳制的浏览器。
4. **seek 基准偏移（timeBase）**：`?ss=T` 起播的流时间轴从 0 重排，seek 换流必须叠加基准（`ss=T+t`），否则落点偏早 T 秒——观感同样是"拖不到想要的位置"。
5. **死流无法服务端预检 → 前端无进展看门狗**：ffmpeg 写满管道缓冲后阻塞等消费者排空，退出时机取决于读取方——起播预检 `wait(3s)` 实测只能得到 None（进程还"活着"）。残余死流场景由前端兜底：25s 无播放推进**且缓冲无增长**才动作（慢速但在流的源不误杀），direct/remux 自动降级转码，转码档显示错误+重试。
6. **remux 不再占用转码并发槽**：信号量保护的是转码 CPU；remux（-c copy，CPU≈0）全程占槽使"一路 remux 播放中 + 开下一条流"最长等 15s（长转圈）甚至 503 STREAM_BUSY。`iter_ffmpeg_pipe` 仅 transcode 持槽；断连看门狗的 kill 职责不变。

### 落地内容

- **后端**（streamer/videos）：remux 命令补 `-bsf:a aac_adtstoasc`；`iter_ffmpeg_pipe` 仅 transcode acquire 并发槽（remux 的 `proc_holder["release"]` 为 no-op，看门狗 kill 生效不变）；端点注释说明死流不可服务端预检的原因。
- **前端 PlayerDialog.vue 重写**：
  - **移动端专门全屏播放器**：Teleport 到 body 的 `position:fixed` 全屏层（z-3000，盖过 TabBar）——顶栏返回键 + 单行截断标题 + 状态行（直出/重封装/转码/已切换转码/跳转中），播放器填满其余空间，safe-area 适配，打开期间 body 滚动锁；桌面 el-dialog 形态不变。
  - open() 同步 artEpoch；**startAt 只应用一次**（防 fallback 换流后 loadedmetadata 重放把时间轴跳乱）；timeBase 随每次换流更新。
  - art `'seek'` 拦截 + `hotSwapTo` 统一热切换（原生 seeking 钩子兜底）；mountPlayer 改 40×50ms 重试（原 50ms 单发在慢设备上会静默失挂）；无进展看门狗（canplay/playing/timeupdate 解除，waiting/起播布防）。
  - 移动端 ArtPlayer `fullscreen: true`（Android 原生全屏/横屏锁按钮；iOS Safari 走 webkit 回退）；overlay 本身即全屏，按钮是增强不是依赖。
  - 播放器状态全部**实例局部化**（原模块级共享在多视图 KeepAlive 下是隐患）。

### 测试与验证

- pytest **155 → 156 passed**（+1：`test_stream_remux_ts_adts_aac`——TS+ADTS AAC remux 回归，断言完整 fMP4（>16KB 且 `mfro` 收尾）；修复前该场景返回 200+1.2KB 僵流）。断连回归用例源改 mpeg4（remux 不再持槽后保持原回归语义）。
- **端到端（puppeteer-core + 本机 Chrome；桌面 1280×800 与移动 390×844 双视口；测试片源带时间码烧录，验收后测试数据/临时脚本已清理）**：
  - 评分页点 TS（remux）→ `?ss=30` 起播，画面时间码 **00:00:30.6** ✓（修复前 1.2KB 死流转圈）；
  - 评分页点 mp4（direct）→ currentTime **30.1** 起播 ✓（修复前从头播放）；
  - 拖到缓冲区外（条宽 96%）→ 热切换 `ss=59.13`（=基准 30 + 落点 29.1），画面时间码 **01:02.4** ✓（修复前钳制回 0）；
  - 缓冲区内拖动 → 原生 seek 落点 30+5.2，画面 34.9 ✓；
  - 移动端：全屏播放器 390×844 全覆盖、返回键关闭、状态标签、标题单行截断；remux 与 transcode（HIMYM mkv）双档起播 ✓。
- 前端 build ✓（dist 已更新）；后端重启即生效（本次验证跑在修复后代码上）。

### 已知问题与未尽事宜

- **fMP4 渐进流时长估计限制仍在（沿 R4/R5/R6）**：进度条总长 = 已缓冲估计值（随播放增长），拖动上限为当前估计时长。R7 已把"拖哪里就从哪里播"修对，但条长仍受限——根治 = HLS 演进（ADR-025 后续项）。
- TS 无索引：`?ss=` 深处 seek 时 ffmpeg 需从头扫描到目标，SMB 大文件可能数秒转圈后开播（看门狗按缓冲增长判定，不会误杀）。
- 看门狗 25s 是"慢源不误杀"与"死流尽快反馈"的折中。
- 真机（iOS Safari / Android Chrome）待复检：全屏播放器手势、返回键、原生全屏按钮、自动横屏。

### 下一步入口

1. 用户复检三题（真机优先）+ R6.3 的自动化/外部分析复检。
2. TS 库规模大且 seek 扫描延迟明显时 → 评估 HLS 演进。
3. 复检通过并入 v1.2.0 tag 或发 v1.2.1。

---

## 交接记录 — R6.3（2026-09-19，v1.2.x 未发版）

### 背景（用户复检反馈 4 项）

①设置项"自动训练所需新增评比次数"；②"自动扫描/自动处理面容"（可联动，扫描到新视频即处理）；③统一名词"人脸"→"面容"（按钮文本"处理人脸（抽帧/检测/打分）"→"处理面容"）；④**外部视频一次性分析**：上传/投放目录 → 面容处理+打分 → 综合结果，不影响自管面容库/视频库（容器化下大文件的传入方式由实现定夺）。

### 落地内容

- **运行时设置扩展**（services/runtime_settings）：SPECS 支持 bool/str(HH:mm) 类型；新键 `auto_train_every`（0=关闭）/ `auto_scan_enabled` / `auto_scan_time`（每日触发时刻，R6.3b 按用户反馈由间隔制改为**每天固定时刻**）/ `auto_process_enabled`；`faces._maybe_autotrain` 改读运行时值（原 .env 只读）。
- **自动化服务**（services/automation.py，新增）：守护线程 20s 一 tick；auto_scan 开启时**每天 auto_scan_time 时刻触发一次扫描**（tick 时刻已过当天配置点且今天未触发过 → 触发，错过当天补跑一次；上次触发日期存 kv `last_auto_scan_date`）；auto_process 且有 pending → 提交处理链首批（与 /api/process/start 同口径）；**联动**两条路：扫描产出 pending 后下一 tick 自动起处理 + scan_handler 在 auto 扫描发现新增/变更时立即链首批（免等 tick）。lifespan 启动/停止；默认全关。
- **外部视频分析**（services/analyzer.py + api/analyze.py，新增）：
  - 传入双通道：`POST /api/analyze/upload`（multipart 流式落盘 data/inbox/，1MB 分块不占内存）或直接拷文件到**投放目录** `data/inbox/`（Docker 即挂载卷 ./data/inbox，NAS 大文件走 SMB/NFS 拷贝更稳）；
  - `POST /api/analyze/start` {filename} → analyze 任务（单 worker 串行、可暂停/取消、进度=帧数）；`GET /api/analyze/{token}` 取结果；`GET /api/analyze/{token}/face_{n}.jpg` 缩略图；`GET /api/analyze/list|inbox`；`DELETE /api/analyze/{token}` 连源文件一起删；
  - 分析口径与主链路完全一致（同一引擎/打分器单例、同一门控/聚类/三层合并/上限阈值、top-K 聚合），但**隔离语义**：不写 video/face/face_identity/face_score 任何表，产物落 `data/analyze/{token}/`（result.json + face_N.jpg），保留最近 `SOPHOS_ANALYZE_KEEP`(20) 个自动清理；
  - 前端新增「分析」页（AnalyzeView，App 第 4 个页签）：拖拽上传带进度条、投放目录选择、任务进度轮询、综合分 + 面容卡网格（缩略图/分数/时刻）、历史分析查看/删除。
- **设置卡片**（TasksView）：自动训练阈值、自动扫描开关+每日时刻选择器（R6.3b）、自动处理面容开关；任务类型名加 analyze→外部分析。
- **名词统一**：前端全部"人脸"→"面容"（3 处：处理按钮去括注、JOB_TYPE_NAMES、PlayerDialog 注释）。

### 测试与验证

- pytest **148 → 155 passed**（+7）：settings 新键/bool+HH:mm 校验 2、autotrain 运行时阈值（0=关闭/15 触发）1、automation.tick 每日定时触发+当天去重+跨天再扫 1、`_is_scan_due` 纯函数（到点/已触发/未到/补跑/跨天）1、analyze 上传与校验 1、无脸视频 1、lena 端到端**隔离断言**（video/face/identity 计数不变+删除连源文件）1。注：automation 测试曾因 tick 与单 worker 排干竞态偶败，已改为等待队列清空后断言（确定性）。
- 前端 build ✓；浏览器验证待用户复检（服务器 8000 已重启为新代码）。

### 已知问题与未尽事宜

- 外部分析时长受 CPU 限制（与主链路同量级）；分析期间 worker 被占用，扫描/处理/训练排队（单 worker 设计语义）。
- automation 的 process 提交与用户手点 409 防重存在理论竞态（提交前 has_active_job 检查 + 单 worker 串行使影响限于偶尔多排一批，幂等无害）。
- 上传通道无鉴权豁免（走统一口令中间件）；大文件上传经 http 反代时注意 body 限制。
- iOS/Android PWA 安装、外部分析页的真机体验待用户复检。

### 下一步入口

1. 用户复检：分析页上传小视频看综合分与面容卡；设置卡片开启"自动扫描+自动处理"观察任务页自动出任务；确认全站"面容"用词。
2. 阈值/开关入口：全部在设置卡片（kv 运行时设置），`.env` 仅默认值（`SOPHOS_AUTO_SCAN_TIME` 等见 .env.example）。
3. 复检通过可合并进 v1.2.0 tag 或发 v1.2.1。

---

## 交接记录 — R6（2026-09-18，v1.2.0）

### 背景（用户 R5.2 复检反馈 6 项 → PLAN_v1.2.0.md）

①加访问权限（一个密码，建立容器时可配置）；②移动端 UI 大面积显示不正常（必要时重构/换现代 UI）；③播放仍常报"流加载失败或编码不支持"（直出视频也报）；④快速拖进度条转圈、开始播放转圈后页面失去响应（推测与训练批次/应用模型相关，未稳定复现）；⑤同一人被识别成不同人 + 每视频应至少一张有效面容 + 宁缺毋滥不凑数；⑥明确训练方向=根据评分/对比结果调整模型，不是对指定视频面容分数直接规范化。

### 根因与定夺

- **直出也报错（T2，ADR-027）**：错误文案来自 `<video>` error 事件一刀切；direct 档失败无法自愈——h264 High10/4:2:2 等"探测显示 h264 兼容但浏览器实际解不了"的变体、扫描期探测失败时的乐观直出、文件怪癖。解法=**自动降级**：`?fallback=1` 强制转码档，前端 error 时自动重试一次，仍失败按 MediaError.code 分型。
- **页面失去响应（T3，ADR-027）两个独立根因都修**：①R5.2"首块交付即释放"信号量的副作用=转码流**存续期不限流**——快速拖动每 250ms 起新转码流、旧流靠看门狗 1s 后才回收，弱 CPU 上多条 1080p x265→h264 并行打满整机（Web 服务一起僵死）→ 恢复**流生命周期全程持有**（看门狗 kill 兜底已使僵尸窗口亚秒级）；②apply_version/recompute_all 分块提交的连续 EXCLUSIVE 写锁挤占全部在线读（回滚日志模式无 WAL）→ 块间 sleep(0.02) 留读窗口。
- **同人碎裂（T4，ADR-028）**：R4 谨慎层要求双方 ≥2 干净样本，而基线 ~78% identity 为单样本——单样本碎片永远合不上，正是"同一个人识别成不同的人"的主因 → 第三层合并（单样本对、[0.74, 0.78)、无共现）。
- **每视频 ≥1 张（T4）**：性别门全拒时有边际误判可能 → 保底（fp≥0.50 且 CLIP≥0.50 且 rep 非侧脸且质量 ≥min_quality×0.5 且非全遮挡，**只保 1 个**，纯男性视频仍 0）；不凑数=cap 上限语义维持"封顶不补齐"。

### 落地内容

- **T1 访问口令（ADR-026）**：`SOPHOS_ACCESS_PASSWORD`（空=不启用）；`app/api/auth.py`（login/logout/status，Cookie=sha256("sophos:"+password)，改密码全体旧会话失效）+ `app/api/middleware.py` 纯 ASGI 中间件（保护 /api/* 与 /docs /openapi.json；放行 auth/health、SPA 静态壳、OPTIONS）；前端 api.js 401 拦截 + App.vue 全局登录遮罩（登录成功整页刷新）。compose/.env.example 注释位。
- **T2 播放降级**：videos.py stream 端点 `fallback` 参数；PlayerDialog 重写 error 处理（未兜底→自动 `?fallback=1` 重试，提示"已自动切换转码"；最终失败按 code 2/3/4 分型 + 手动重试按钮）。
- **T3 并发治理**：streamer 信号量回归全程持有 + `_StreamSlot` 幂等槽位（生成器 finally 与断连看门狗双路释放，治 Starlette spec≥2.4 断连不关同步生成器的泄漏）+ `StreamBusy`（等待 15s → 503 STREAM_BUSY）；看门狗 0.35s；personalizer.apply_version 与 aggregator.recompute_all 块间 sleep(0.02)。
- **T4 面容**：clustering.merge_groups 第三层（singleton_threshold，pipeline 传 `SOPHOS_MERGE_SINGLETON_THRESHOLD`=0.74）；pipeline `_gender_filter_detailed`（记 rejected 证据）+ `_rescue_group` 保底；process result 增 `rescued_groups`；status_msg 区分 "no faces detected" / "no valid female faces (all gated out)"。
- **T5 训练语义**：TrainView 说明区重写（评分→调整模型参数→全库预测；不是对指定视频分数直接改写）+ README 特性行。
- **T6 移动端响应式（Element Plus 保留）**：`src/ui.js`（isMobile 响应式单例 + 登录态）；App.vue 双形态外壳（桌面 border-card 页签不变；≤768px 顶栏+底部 TabBar+safe-area+KeepAlive）；VideosView/TasksView 移动端卡片列表；RateView/PairView 图片限高与纵排；TrainView 表格横滑兜底；PlayerDialog 移动端 flex 列布局（100dvh）+ 弹窗标题单行截断；index.html viewport-fit=cover。
- **R6.1 任务详情弹窗（用户复检反馈）**：任务页"结果/错误"列不再全文铺开——表格/卡片内只留一行摘要（结果=顶层标量字段拼 `键=值 · …`，错误=前 60 字符），点"详情"弹窗看完整内容（任务基本信息 + 参数 + 完整结果 JSON/错误栈，等宽滚动块）。
- **R6.2 图标 + 文案精简 + PWA（用户复检反馈）**：根目录 `icon.png`（1254² RGBA）生成 `frontend/public/` 下 favicon 64 / banner 256 / PWA 192+512 / maskable-512（构建打包进 dist）；浏览器标签 favicon + 顶栏 banner 圆形展示；banner 副标题、"训练是怎么工作的…"长说明（保留一行功能规则）、"· 随机出现"、"点行/点卡片在线播放"、任务页两处括号说明等冗余文案删除。**PWA**：`public/manifest.webmanifest`（standalone、图标三档）+ `public/sw.js`（/api/* 一律直连不缓存；页面导航网络优先回落 SPA 壳；同源静态资源缓存优先+后台刷新；缓存名 sophos-v1，改 SW 需升版本）+ main.js 安全上下文内注册 + iOS apple-touch-icon/meta。**限制**：Service Worker 仅安全上下文（localhost/https）生效——手机经局域网 IP 的 http 访问时 SW 不注册（iOS"添加到主屏幕"仍可用，Android 安装需 https 反代，平台限制）。
- **版本**：APP_VERSION=1.2.0；测试断言同步。

### 测试与验证

- pytest **129 → 148 passed**（+19）：auth 6（无口令放行/401 分型/错密码/Cookie 解锁/文档保护+SPA 放行/改密码失效+logout）、stream 6（fallback 强转转码/transcode 关闭时 fallback 不生效/信号量占用 503 STREAM_BUSY/**槽位幂等双释放/生成器未消费完 close 释放/断连后下条流 200 端到端回归**）、merge singleton 2（单样本对合并/下限与共现护栏）、rescue 4（边际女保底/选最优正面/四类护栏不保/多样本簇+CLIP 过）+ health 版本更新。
- **生产缺陷受控复现并修复（本批次最重要发现）**：信号量全程持有后，**Starlette 1.6（spec≥2.4）在客户端断连时不关闭同步生成器**（`stream_response` 以 OSError 结束，`async for` 半途废弃）——生成器永远挂在 yield 上，finally 的释放永不执行 → 次条流 503（真实库 uvicorn + curl --max-time 3 中断复现；R5.2 曾以"首块释放"绕开同一底层行为）。修复 = `_StreamSlot` 幂等槽位（生成器 finally 与断连看门狗 kill 后双路释放、旗标只放一次），端到端回归测试（进程内 uvicorn + httpx 早停）锁定。
- 真实 HTTP 鉴权冒烟（uvicorn，SOPHOS_ACCESS_PASSWORD 设置态，临时数据目录）：无 Cookie /api/videos→401 ✓；auth/status required=true ✓；错密码→401 ✓；登录→200+Cookie ✓；带 Cookie /api/videos→200 ✓；`/`→200（SPA）✓；/openapi.json 无 Cookie→401 ✓；/api/thumbs/1.jpg 无 Cookie→401 ✓。
- **浏览器实机（IAB，390×844 与 1280×800 双视口，真实库 22 集）**：移动端首访弹登录层 ✓ → 输密码登录整页刷新 ✓；评分页（图片自适应/按钮两行排布/长文件名截断）✓；视频库卡片列表（分数/状态/播放方式徽标）✓；点卡片播放（转码档起播、currentTime 推进、无 MediaError）✓；快速连续 seek ×3 + 关闭重开同一条流 → 立即起播（槽位回收）✓；播放器弹窗标题单行截断（实测曾占三行挤播放器，已修）✓；桌面端 border-card 页签 + 表格布局不变 ✓。截图存档会话 artifacts。

### 已知问题与未尽事宜

- **fMP4 渐进流时长估计限制（固有限制，沿 R4/R5）**：浏览器对 remux/转码管道流只能估计**已下载部分**的时长（实测 ~20s 起随播放增长），进度条总长与超出估计时长的远距跳转受浏览器钳制（seek 目标 > 估计时长时浏览器回退到 0）。缓冲区外的带内 seek 拦截与服务端 `?ss=` 正常（R5 验证 + 本批次端到端确认）。根治 = HLS 演进（ADR-025 后续项，播放列表带真实总时长）。
- **转码并发槽位**：断连回收由看门狗兜底（0.35s 轮询 + `_StreamSlot` 幂等释放），信号量等待 15s 超时 → 503 STREAM_BUSY（前端提示"转码通道被占用"）；单用户场景下另一路播放占用时预期出现。
- 保底为"尽力"语义：全部 cluster 证据不足（fp<0.50 或 CLIP<0.50 或侧脸/过暗）时视频仍为 0 张（status_msg 说明）；样本级门控（质量/姿态）全挂的视频无 embedding 无法保底。
- 单样本合并阈值 0.74 偏保守（证据弱一档）；用户复检若仍嫌碎可降 `SOPHOS_MERGE_SINGLETON_THRESHOLD`（如 0.70），0=关闭。
- 移动端为响应式重构（Element Plus 保留），未换 UI 框架——桌面行为不变、风险可控；用户复检后若仍不满意可评估整体换壳（Vant/Naive）。
- 前端 npm 依赖不变（本批次无新增）；dist 已重新 build。

### 下一步入口

1. **用户复检**：①设置 `SOPHOS_ACCESS_PASSWORD` 后浏览器/手机访问（登录层、缩略图、播放）；②手机访问各页签 + 播放器全屏；③曾报"流加载失败"的直出视频（应自动切换转码）；④快速拖动进度条 + 训练/启用模型期间浏览是否仍卡；⑤重处理 2-3 集看同人碎片是否减少、每视频是否有面容。
2. 阈值入口：`SOPHOS_MERGE_SINGLETON_THRESHOLD`（嫌碎→调低）/ `SOPHOS_TRANSCODE_MAX_CONCURRENCY`（并发观看）。
3. 复检通过 → git tag v1.2.0。

---

## 交接记录 — R5.2（2026-09-18，v1.1.0）

### 背景（用户 R5.1 复检反馈）

①评估"NFS 挂视频库 + 本地 SSD 放 DB/缩略图"形态下优化是否适用（结论：全部适用，WAL 解禁 → `SOPHOS_DB_WAL` 开关，ADR-025）；②视频库悬停显示完整路径；③评分/对比页点击视频链接可播放；④评分页随机出现；⑤播放器适配移动端（全屏），播放器用成熟开源库不硬写。

### 落地内容

- **前端播放器统一**（`components/PlayerDialog.vue`）：采用 **ArtPlayer**；评分页/对比页/视频库三页共用；移动端（≤768px 或移动 UA）弹窗全屏 + playsInline + autoOrientation，桌面端保留全屏按钮。评分/对比页视频名标签可点击播放并**跳到面容时刻**（direct 档 currentTime、转码/重封装档 `?ss=`）。faces API `_item` 下发 `stream_url/stream_mode/video_path`。
- **评分页随机**：`GET /api/faces?order=random`（过滤后 id 上 ORDER BY random() 取页）。
- **视频库悬停**：文件名列 el-tooltip 显示完整路径。
- **后端**：faces.py 上述两处；config `db_wal` + session.py WAL 开关；.env.example 注释位。
- **生产缺陷修复（浏览器实测复现）**：**僵尸 ffmpeg 占满转码信号量**（1.7GB 进程，后续播放全部挂死）——同步生成器阻塞在 threadpool 管道读，协程取消不可达，finally 永不执行。修复：①信号量改"首块交付即释放"（启动限流语义）；②stream 端点改 async + `request.is_disconnected()` 看门狗（1s 轮询），断连直接 kill ffmpeg（proc_holder 传递句柄）；③顺带清理一处重复 except 死代码。
- **前端调试中发现的坑（记入交接资产）**：ArtPlayer 构造后内部 `<video>` 未必立即入 DOM（监听要轮询挂载）；`art.destroy(false)` 残留死 DOM 元素（querySelector 会打到死元素）；matchMedia 放 computed 会缓存陈旧值（改 open() 时求值）。

### 测试与验证

- pytest **129 passed**（新增 3：faces stream 字段 / order=random / unrated+random 组合）。
- 浏览器实测（IAB，1280×720 与 390×844 双视口）：悬停 tooltip 显示 `\\Nas02\...` 全路径 ✓；视频库点行播放 ✓（readyState=4）；评分页 ▶ 播放并跳到 ss=面容时刻 ✓；拖动进度条服务端 seek 生效（src 切 ss=574 续播）✓；对比页 ▶ 播放且不误触选对比 ✓；移动端弹窗满屏（390=视口宽、播放器高 724）✓。截图存档会话 artifacts。

### 已知问题与未尽事宜

- HLS（ffmpeg -f hls + hls.js）列为播放/转码的后续可选演进（ADR-025），当前 fMP4 + ?ss= 已满足单用户场景。
- 前端 npm 依赖新增 `artplayer`（~150KB），换机部署需 `npm install`。
- `SOPHOS_DB_WAL` 默认 false；DB 迁本地 SSD 后设 true。

### 下一步入口

1. 用户复检：移动端真机播放（横竖屏）、评分页随机体验、悬停路径。
2. 若需多并发观看/更强拖动体验 → 评估 HLS 演进（ADR-025）。
3. DB 迁 SSD 后：`SOPHOS_DB_WAL=true`。

---

## 交接记录 — R5.1 压测批次（2026-09-18，v1.1.0）

按用户目标在本地开展"测试→开发→迭代"循环（每阶段记录见 [docs/STRESS_LOG.md](docs/STRESS_LOG.md)）：

- **工装**（`scripts/stress/`）：`gen_clips.py`（源片确定性低质短切片，可重复幂等）+ `seed_db.py`（Core executemany 直插种子库）+ `bench.py`（端点/训练/应用计时）+ `run_pipe_stress.py`（流水线编排：分批链/暂停/恢复/取消）+ 1TB 目录守卫。测试库 `Z:\SophosStress\`（1.18GB，按用户约定暂保留）。
- **S3 流水线压测 ✅**：620 视频（120 真实切片 + 500 合成）→ 156 个分批 job 全 done（5.8 视频/s）；暂停冻结进度、恢复续跑、取消停链、扫描幂等（unchanged=620/2.1s）全部符合预期。
- **S2/S4/S5 基准 ✅**：30k 视频 / 24 万 identity / 96 万 face / 6 万缩略图——评分页 unrated 95ms、缩略图 p50 14.9ms、对比选对暖态 0.52s（修复前 6.37s）、train 3.1s、apply 41s、recompute_all 85s。
- **修复与优化 5 项**（pytest 126 全绿）：①`/api/jobs?active` 500（in_ 变参误用，真实缺陷）；②`session.py` PRAGMA cache_size=128MB + temp_store=MEMORY（SMB 随机读 4×+，最大单点收益）；③共现计算收缩到候选集（96 万行→~2000 行，删指纹缓存）；④pair 候选向量化（12.5 万对 Python 循环→numpy 矩阵）；⑤抽样分段连续寻道 + 全矩阵乘替代 fancy-index。
- **工装教训（SMB 规律）**：ORM 逐行 flush × SMB 往返 = 小时级；批量 executemany 快两个数量级——佐证 ADR-024 写路径批量化判断。

---

## 交接记录 — R5（2026-09-17，v1.1.0）

### 背景（用户 R4 复检反馈 → PLAN_v1.1.0.md）

五项需求 + 规模目标：①转码视频点击未缓冲区域直接报"解码失败（编码不支持）"；②任务暂停/取消；③大量视频人脸处理自动分批（可调）、批间可插队其他任务；④**同一视频文件的同一个人最多保留 5 张面容**（可调）；⑤对比优先级：跨视频不同人 > 同视频不同人 > 跨视频可能同人。另：项目拟面向**数万个视频**，要求架构/数据库/训练/打分应用机制优化（打分图片慢、缩略图大且检索难）。

### 根因与定夺

- **seek 报错根因（T1，ADR-021）**：remux/transcode 是 fMP4 渐进管道流**无 HTTP Range**，浏览器 seek 未缓冲区域 → 注定失败的 Range 请求 → MediaError 3。解法=服务端 seek（`?ss=` → ffmpeg `-ss` 输入 seek）+ 前端拦截 `seeking`（buffered 外 → debounce 重起流），不做转码缓存。
- **分批语义（ADR-022）**：链式提交（任意时刻 ≤1 个 process job，批间插队天然成立）替代一次全量 job；多线程并行不做（ONNX/ffmpeg 内部已并行 + SMB SQLite 写竞争）。
- **面容上限语义（ADR-023）**：人物真值不可得 → 余弦 ≥0.50 单链连通分量 ≈"可能同一个人"，**每组件独立封顶**（多女性视频不跨人强并）；共现否决 + floor 护栏保留（宁超限不误并）。数值按用户修订 = **5**（原表述 20）。
- **对比层级（ADR-023）**：用户修订覆盖 ADR-019 层级——跨视频不同人（明显不同>证据不足）> 同视频不同人（共现>明显不同）> 跨视频可能同人 > 同视频证据不足；R4"共现压过一切"测试按新语义重写。

### 落地内容

- **T1 播放 seek**：`stream?ss=`（videos.py/streamer.py `build_ffmpeg_cmd(start_sec)`，-ss 置于 -i 前）；前端 VideosView 拦截 seeking（±1s buffered 判定、250ms debounce、保留暂停态、"跳转中"标签）；direct 档忽略 ss。
- **T2 任务控制**：jobs.py 状态机加 paused；`check_point()`/`JobInterrupted` 协作中断（process 每视频+逐帧、scan 每目录、train 起点）；`POST /api/jobs/{id}/pause|cancel|resume`（409 JOB_STATE）；process 中断回退视频 pending；reset_stale_jobs 保留 paused；任务页操作列。
- **T3 分批链**：process/start 切片只提交首批（409 NO_PENDING_VIDEOS 新增）；handler 尾部检查点→链式提交下一批；`runtime_settings` 服务 + `/api/settings` GET/PUT（白名单：process_batch_size / max_faces_per_person，kv_setting 存储）+ 任务页设置卡片。
- **T4 面容上限**：`clustering.cap_groups`（组件化封顶）接入 pipeline（result 增 `cap_merged_groups`）；`max_faces_per_person=5`、`cap_merge_floor=0.50`。
- **T5 diverse 重排**：`pairs._pick_diverse` 四层（0/1/2/3）实现；测试按新层级重写（含"跨视频明显不同 > 同视频共现"新语义与 ①层内置信度次序）。
- **T6-T9 大规模（ADR-024）**：缩略图分片 `thumbs/{id//1000:03d}/` + `services/thumbs.py` + 动态端点（immutable 缓存 + `?v={updated_at}`）+ 重处理清孤儿 + 评分页预取 3 张；`face(video_id,timestamp_sec)` 复合索引（ORM `__table_args__` + 迁移守卫补建）；faces unrated 改 NOT EXISTS；pair 选对轻量行+抽样（`pair_sample_size=500`）+ 共现 SQL 指纹缓存 + 已比对候选集查询；训练特征只取有评分/对比的 identity、pair 梯度向量化（exp 裁剪 ±30）、`apply_version` 分块 keyset 翻页 + SQLite UPSERT + 版本幂等跳过、`recompute_all` 分块提交（500/块）。

### 测试与验证

- pytest **100 → 126 passed**。新增 26：cap 7（组件隔离/多人独立/桥接/共现/floor/noop）、任务控制 3（生命周期/queued 跳过/中断回退+停链/链式提交）、settings+分批 5、stream seek 4（-ss 位置/transcode/remux/direct 忽略）、thumbs 4（分片路径/回落/端点缓存/重处理清理）、diverse 重写新增 3。
- 前端 `npm run build` ✓（dist 已更新）。

### 已知问题与未尽事宜

- cap 上限为"尽力保证"：全部剩余对被共现否决或低于 floor 时可停在 >N（设计语义，宁超限不误并）。
- pair 抽样在库 >500 identity 时为近似排序信号（ADR-024）；小库保持全量精确。
- 旧缩略图不主动迁移：`find_thumb` 回落旧平铺路径，重处理后自然落入分片。
- diverse 层级变化后，存量 pair_comparison 不受影响（仅排序语义）。
- 转码 seek 首字节延迟 = 从 ss 点起重新转码（数秒），无缓存（沿 R4 决策）。

### 下一步入口

1. 用户复检：E11（hevc）播放中拖动进度条到远处 → 应显示"跳转中"并在数秒后从该位置续播，无"解码失败"弹窗；处理一批视频时点暂停/取消/恢复；设置卡片改分批大小/面容上限后跑批验证。
2. 大库压测建议：分批导入万级视频，观察评分页首图时间（缩略图分片+缓存+预取）、`GET /api/faces?unrated=1` 响应、训练耗时。
3. 阈值入口：`SOPHOS_PROCESS_BATCH_SIZE` / `SOPHOS_MAX_FACES_PER_PERSON`（或 WebUI 设置卡片）、`SOPHOS_CAP_MERGE_FLOOR`、`SOPHOS_PAIR_SAMPLE_SIZE`。
4. PLAN §7 验收清单全绿；PLAN §8 明确不做项（多线程并行/双尺寸缩略图/PG 迁移/转码缓存）。

---

## 交接记录 — R4（2026-09-17，v1.0.4）

### 背景（用户 R3 复检反馈 → PLAN_v1.0.4.md）

三件事：①两两对比先追求**不同的人**比较（人物 > 视频 > 分差 > 随机）；②同一视频内同人碎成多 identity 需**适度合并**（但明确**不做跨视频合并**）；③修复在线播放 `Request failed with status code 404`。

### 根因与定夺

- **碎裂基线（T1）**：主库 E01-E16 共 321 identity，**~78% 单样本**（E14 37 中 26；E09 32 中 26）——合并 pass 值得强化，但 plan 原则"宁可少量重复，不可误合并"。
- **播放 404 根因（T6，一次定位）**：`VideosView.open()` 用 `api.head('/api/videos/{id}/stream')` 预检流，axios 实例 `baseURL:'/api'` → 实际请求 **`/api/api/videos/...`** → StaticFiles fallback 404 → 弹窗被误杀报 "Request failed with status code 404"。`<video>` 本身与 MKV/HEVC/转码档无关。
- **diverse 语义（ADR-019）**：跨视频同演员 pair 只降级不禁止（不建 global person、`pair_comparison` 表不变）；同帧共现 = 最高置信"不同人物"证据。

### 落地内容

- **T2 双层 merge（clustering.py + pipeline.py，ADR-018）**：第一层组间全样本均值余弦 ≥0.78 直接合并（沿 R1）；第二层 [0.72, 0.78) 谨慎合并须同时满足 双方 ≥2 干净样本 + 干净样本均值也过线 + 无同帧共现。**同帧共现（face.timestamp_sec 差 ≤0.5s）一票否决，优先级高于任何相似度**（强相似也禁）。配置：`SOPHOS_MERGE_REVIEW_THRESHOLD` / `SOPHOS_MERGE_COOCCUR_TOLERANCE_SEC`；job result 增 `groups_before_merge`；处理日志输出三层收敛链。
- **T3/T4 diverse（pairs.py 重写 + faces API，ADR-019）**：`pick_pair(strategy='diverse')` 全候选排序（候选 ~n²/2，n 小）：`different_person_confidence`（同帧共现 1.0 > mean_embedding 余弦 ≤0.40 0.9 > 中间 0.5 > ≥0.60 0.1，缺失 0.5）→ 不同视频 → 分差+随机抖动(≤5 分)。similar 保留原语义：band 内先找高置信对、找不到回退原随机（轻度偏好）；random 不变。API 默认策略 similar→**diverse**。
- **T7/T8 播放（videos.py / streamer.py，ADR-020）**：①列表/详情增 `stream_url`（后端唯一真源）；②404 分型 `VIDEO_NOT_FOUND` / `SOURCE_NOT_FOUND`；③**起播预检**：`next(gen)` 首块前失败 → 500 `STREAM_FAILED`(remux) / `TRANSCODE_FAILED`(transcode)，message 带 stderr 尾部（新 `streamer.StreamFailure` 携带 return_code/stderr_tail）；④ffmpeg 缺失 → 500 `FFMPEG_NOT_FOUND`（原误为 415）；⑤流中途失败 log.error 完整上下文（video_id/source/mode/cmd/return_code/stderr tail）；⑥前端：`player.src = row.stream_url`、**删 axios HEAD 探测**、`<video @error>` 按 MediaError.code 分提示（4=源文件不存在/3=解码失败）。
- **前端**：PairView 加"人物优先对比（推荐）"radio（diverse 默认首位）；VideosView 上述改动；`npm run build` 通过。

### 测试与验证

- pytest **81 → 100 passed**。新增 19：merge 双层 5（谨慎合并 / 共现禁谨慎层 / **共现禁强相似层** / 干净样本数 / 干净均值口径）、diverse 8（T5 清单全覆盖 + API 默认策略 + **Tier B 跨视频优先于同视频未知**）、播放回归 6（stream_url 字段 / 404 分型 / **SPA fallback 不捕获 /api** / mp4 h264+aac direct / remux 失败 STREAM_FAILED / transcode 失败 TRANSCODE_FAILED）。
- 前置回归发现并修正一个排序边界：人物证据未知时，不能用 0.5 数值置信度压过视频差异；diverse 现明确 Tier A（不同人物）> Tier B（不同视频）> Tier C（剩余），各层内再按分数接近+随机抖动。前端 build 仍通过。
- 实机（真实库 uvicorn:8030）：videos 列表 stream_url ✓；E11（hevc/eac3）stream → 200 + fMP4 + `X-Sophos-Stream-Mode: transcode` ✓；`/api/videos/9999/stream` → JSON `VIDEO_NOT_FOUND` ✓；diverse 连续 30 对：同帧共现同视频 12 + 跨视频 18、无重复 ✓（探针产生的 30 条 pair_comparison 已从真实库清除）。
- **浏览器实机（IAB）**：视频库点击 E11 → 弹窗显示"转码播放中，首次加载稍慢"，原生 `<video>` 的 `src=/api/videos/11/stream`，`readyState=4`、`currentTime=5.42s`、`duration=9.26s`、`paused=false`、`error=null`；对比页默认 radio **"人物优先对比（推荐）"** [checked]，A/B 成功渲染。播放截图存档于本会话 artifacts。

### 已知问题与未尽事宜

- 谨慎层要求双方 ≥2 干净样本 → **单样本 identity 碎片仍不合并**（基线 78% 单样本大多是暗光小脸，0.72 区间单样本证据不足，属 plan 预期取舍）；用户复检若嫌碎，优先调 `SOPHOS_MERGE_REVIEW_THRESHOLD`（降到 ~0.68）而非放宽共现。
- 重处理才生效：旧 identity 是旧 merge 口径产物；存在用户评分的视频拒绝自动重处理（保护数据），需手动清评分。
- diverse 排序为 Tier A/B/C 确定性分层（同层抖动 ≤5 分），极端情况下连续对可能集中在同一集的高置信对上——属"人物优先"设计语义。
- 转码仍是实时管道无缓存（plan 明确不做）；长视频 seek 体验受 fMP4 分段限制。浏览器 IAB 已验证 E11 x265/eac3 MKV 正常播放（readyState=4、currentTime 推进、无 MediaError），剩余是用户对多集/长视频的主观体验复检。

### 下一步入口

1. 用户复检：WebUI 重处理 2-3 集（如 E01/E09/E14）→ 面容库看"同人卡片"是否减少、无 Robin/Lily 误合并；对比页直接体验 diverse 默认；视频库点行播放 mkv。
2. 阈值入口（.env）：`SOPHOS_MERGE_REVIEW_THRESHOLD`（嫌碎→0.70~0.68）/ `SOPHOS_MERGE_COOCCUR_TOLERANCE_SEC`（抽帧间隔 2s 下的同帧容差 0.5s 合理）/ `SOPHOS_PAIR_SIM_CLEARLY_DIFFERENT` / `SOPHOS_PAIR_SIM_LIKELY_SAME`（diverse 人物判定松紧）。
3. 若 pairs 需要更多随机性：`pairs.py DEFAULT_JITTER`（现 5 分）。
4. PLAN §27 验收抽样（50-100 对统计三类占比）可重复 `_r4_diverse_probe.py` 模式（已删，git 历史可考）；PLAN §29 出口清单见 DEVELOPMENT_PLAN R4 节。

---

## 交接记录 — R3（2026-09-17，v1.0.3）

### 背景（用户 R2 复检反馈）

效果明显改善。两点新要求：①面容截图范围扩大——头顶+20%、两侧各+20%、**向下+80%**；②提升清晰度门禁，仍截取了很多不清晰、很暗的面容，**宁缺毋滥**（面容少也能建立审美模型）。

### 落地内容

- **人像取景缩略图**（`face_engine.portrait_crop` + R3 常量）：bbox 按 头顶+20%/两侧各+20%/向下+80% 外扩（越界截断），高度 320、JPEG q90，替代原 112 对齐脸放大——观感清晰度大幅提升（原生分辨率）。**特征 embedding / genderage / CLIP / base_score 输入口径不变**（识别与打分校准不受影响，缩略图纯展示）。实现上抽帧保留到 `_persist_groups` 完成后统一清理（缩略图需原始帧；frames 参数为 None 时回退对齐脸，兼容单测）。前端评分/对比页改竖版自适应显示（height 300 width auto）。
- **质量门 v3**（ADR-017）：sharp 归一 **/200→/80 重新定标**（关键：旧归一把正常视频脸全压在 0.15-0.3，与糊脸不可分——E01 成分探针：7 张已认可脸 lum 53-83 / std 29-37 / lap 32-57）+ 曝光因子（lum<30→0）+ 对比度因子（std<12→0），`min_quality` 0.15→**0.30**。三因子同时达标才入库：暗场（lum<45）/灰蒙蒙（std<20）/糊脸（lap<24）组合必被压到阈值下。
- **设计教训（写入 ADR-017）**：首版"陡曲线+阈值 0.25"把 7 张已认可脸全杀（乘性塌缩）；用成分探针（`scripts/_q2_probe.py` 模式）回退重设计——**改归一化定标比抬阈值更本质**。

### 验证

- E01 重跑：9 identity（质量重定标后聚类分布略变，Robin 增加 2 个近正面 rep），逐格目检：**全女性、取景含头顶与肩部、原生分辨率清晰、无暗糊脸**；酒杯脸保留（清晰度本身达标，按遮挡保留语义）
- pytest **81 passed**（新增：质量 v3 曝光/对比因子 2 项、portrait_crop 取景 1 项）

### 已知问题与未尽事宜

- min_quality=0.30 对特别暗的剧集（夜戏为主）可能产量偏低——宁缺毋滥语义下的预期行为；可调 `SOPHOS_MIN_QUALITY`
- base_score（beauty 模型）输入仍为 bbox 外扩 25% 裁剪（M4 校准口径），未改为人像取景——如需审美模型"看全身"可后续评估
- 旧库缩略图是人像取景前的产物，**重处理视频后自动更新**

### 下一步入口

1. 用户复检：重处理 2-3 集看评分页（取景、清晰度、暗脸是否达标）
2. 阈值：`SOPHOS_MIN_QUALITY`（产量低→调低）；取景比例常量在 `face_engine.py` 头部（PORTRAIT_TOP/SIDE/BOTTOM）
3. 复检通过即完结；新问题走 v1.0.4

---

## 交接记录 — R2（2026-09-17，v1.0.2）

### 背景（用户 R1 验收反馈）

清晰度改善达标，但：①仍有大量男性误入评分库，**部分女性置信度高达 98%**；②侧脸、超小脸、明显看不全的人脸仍在；建议不受限于原技术栈，并用"模型自身视觉能力"（即本会话的图像目检）参与验收。

### 根因定性与方案（E01 全量目检审计，tools 见 scripts/export_review.py + contact_sheet）

- **R1 基线审计**（17 identity 逐格目检）：13 女 ✓ + **4 男漏网**（id3 打电话男 fp0.79 / id6、id13 近全侧男 / id17 Ted 喝酒 fp0.83 近正面）。结论：genderage 对部分男性**系统性高置信误判**，identity 投票无法纠正 → 必须换/加模型
- **CLIP 零样本探针**：同一批裁剪上 4 男 clip_female 全部 ≤0.07、13 女 ≥0.63 → **完美线性可分** → 采用"genderage + CLIP 一致同意"门控
- **姿态**：启发式对近全侧脸系统性低估 ~30°（真 ~70° 估成 near≤45°）；kps 几何信号（眼距/质心偏移）噪声大（SCRFD 在运动模糊下关键点跳变，正脸 eyeSp 可低至 0.03）不可用；CLIP 零样本 profile 对紧裁剪无分离度（正面脸也 0.8+）→ 引入 6DRepNet 系 ResNet-18 姿态模型（对拍：真侧脸 -58~71° 精准）
- **CLIP occluded prompt 实测误报高**（lena 帽子 0.795）→ 不作判定依据（曾短暂融合导致 lena 全遮挡误标，已回退）

### 落地内容

- **新模型两个**（均 MIT）：
  - `clip_vitb32_face.onnx` + `clip_vitb32_prompts.npz`（视觉塔 ONNX + 固化 4 类 prompt 向量，`scripts/export_clip_gender.py`，转换期依赖 torch+transformers 5.17——**transformers 5.x 的 get_text_features 返回类型有变，走 text_model→pooler→text_projection 手动链路**；ONNX 导出用 dynamo=False 且 trace vision_model 本体，get_image_features 的 kwargs 转发 trace 不稳）
  - `headpose_resnet18.onnx`（GitHub yakhyo/head-pose-estimation release 直链；输入 224 RGB ImageNet 归一化，输出 3×3 旋转矩阵 → yaw/pitch，低头为正）
- **face_engine**：CLIP 会话 + `clip_probs`（female/profile/occluded 三值）+ `headpose`（6D 解码）；`process_frame` 门控顺序：尺寸(80) → 截断(bbox+kps 出画) → 质量 → **姿态模型**（回退启发式）→ CLIP profile 拦近背面(0.95) → 对齐+推理；`kps_outside`/`square_crop` 新增
- **pipeline**：`_gender_filter` 双票一致同意（`clip_gender_min=0.50`，CLIP 缺失自动降级）；`_persist_groups` 侧脸 rep 拒绝（`allow_side_rep=false`，rep 是组内最优，rep 侧 = 整组侧）；`clip_female_mean/clip_profile_prob` 落库（migrations 补 3 列）
- **config**：min_face_size 64→80、clip_enabled/clip_gender_min/clip_profile_max/allow_side_rep；`.env.example`、docker-compose 注释位同步
- **前端**：评分页性别置信度小字显示双票（`女性 xx%·Cxx%`）；重新 build
- **工具**：`scripts/export_review.py`（处理→rep 裁剪→接触表，供目检审计）、`scripts/probe_clip.py`（CLIP 对拍探针）

### 验证（E01 单集快迭代，5s 间隔）

- 迭代过程：R1 基线 17 identity（4 男 + 2 强侧女 + 酒杯低头脸）→ R2a 一致同意+min_face+kps 9 个（男全消，剩 2 强侧女）→ R2b 侧脸 rep 拒绝 7 个 → **R2c 最终 7 个：全女性、全部 frontal/near（姿态模型值）、无小脸/出画脸**，逐格目检确认
- 头部姿态模型副产品：低头喝酒脸（pitch 77.7°）被 max_pitch=40 拦截 →"明显看不全"类同步收敛
- lena 回归曾失败（CLIP profile 形参默认 0.60 未同步 config 的 0.95 + occlusion 误报），已修并沉淀为结论写入 ADR-016
- pytest **79 passed**（R1 时 67；新增：一致同意门 3、侧脸 rep 2、kps/方形裁剪 2、CLIP 数学 2、headpose 解码 3）

### 已知问题与未尽事宜

- 姿态模型使单样本推理增加 ~15-25ms、CLIP ~60-100ms（CPU），处理吞吐略降（E01 实测可接受）
- 眼罩/酒杯等**遮挡启发式仍漏检**（CLIP occluded 不可用）；此类脸保留可评分（原语义），但不会自动标记 occluded 进训练隔离——如需可后续人工标记（评分页开关）
- CLIP/头部姿态模型文件为转换产物，**Docker 镜像不含**（同 beauty_scut 模式：卷挂载 data/models）；换机部署需在开发机跑 `scripts/export_clip_gender.py` + curl 下载 headpose
- CLIP profile/occluded 的紧裁剪失真问题：若未来需要真侧脸/遮挡判定，应换专用模型（如 6DRepNet 已解决姿态；遮挡可用 face-parsing 分割）

### 下一步入口

1. 用户复检：重处理 2-3 集（旧数据建议重处理以获得新门控结果），评分页核对男性是否绝迹、侧脸/小脸是否消失
2. 阈值微调：`SOPHOS_CLIP_GENDER_MIN`（误杀女性→调低）、`SOPHOS_ALLOW_SIDE_REP`（想要清晰侧脸→true）、`SOPHOS_MAX_YAW/PITCH_DEG`
3. 真实全库重处理 22 集预估：新门控下单集约 5-8 分钟（5s 间隔参考 4 分钟），全季 ~2-3 小时
4. 若复检通过即完结；新问题走 v1.0.3

---

## 交接记录 — R1（2026-09-16，v1.0.1）

### 完成内容（P1-P5 全部落地，对应 PLAN §9 T1-T9）

- **T1 DB/配置**：`video.vcodec/acodec`、`face.pose_yaw/pose_pitch/pose_class/occlusion_score`、`face_identity.female_prob_mean/occluded/occluded_source` 九个新列；新增 `app/db/migrations.py`（PRAGMA 查缺列 → ALTER ADD，幂等，`init_engine` 末尾调用）；config 新增 8 项（female_identity_threshold=0.60 / min_quality=0.15 / max_yaw=80 / max_pitch=40 / merge_threshold=0.78 / max_samples_per_identity=16 / transcode_enabled / transcode_preset=veryfast / transcode_max_concurrency=1），`.env.example` 已同步
- **T2 face_engine（ADR-014）**：`process_frame` 去掉性别硬过滤（fp 降级为记录值）；新增 `is_truncated`（出画>20%边长剔除）、`estimate_pose`（5 点启发式：yaw=arcsin(asym)×1.5；pitch=简化 3D 模型二分反解，**低头为正**）、`occlusion_score`（对齐 112 下半区纹理缺失 vs 眼部带）；质量门 `q<0.15` 真实生效；定标脚本 `scripts/probe_pose.py`
- **T3 pipeline**：顺序改为 聚类→`_gender_filter`（组均值≥0.60，单样本严 0.65 写死）→`merge_groups`（clustering.py 新增，组间余弦≥0.78 贪心合并迭代）→`_persist_groups`（修剪 top-16、rep 优选 干净>frontal>near>side、干净样本 mean_embedding、全遮挡置空+auto 标记、female_prob_mean 落库）
- **T4 personalizer**：`identity_features` 跳过 mean_embedding 为空的 fully-occluded identity（不进训练、不写个性化分，评分照常展示）
- **T5 播放三档（ADR-015）**：streamer 重构 `probe_codec/decide_mode/build_ffmpeg_cmd/iter_ffmpeg_pipe`（fMP4 管道；`-map 0:v:0 -map 0:a:0?` 防字幕轨 mux 失败；Semaphore 限流；客户端断开 kill 子进程；stderr 落临时文件防管道阻塞）；scanner 扫描期探测入库（新增/变更时）；stream 端点三档自动选择 + `X-Sophos-Stream-Mode` 头；`frame_sampler.locate_ffprobe` 新增
- **T6 API**：`GET /api/videos` 增 `library` 参数（dir_path 精确匹配）；新增 `GET /api/videos/libraries`；列表/详情增 `stream_mode`、`dir_path`；`POST /api/faces/{id}/occlusion`（manual 覆盖 auto）；faces 列表项增 `female_prob_mean/occluded`
- **T7 前端**：视频库页加"库"下拉（workdirs ∪ DB distinct dir_path，可筛选器搜索）+ 播放方式列 + 弹窗模式标签（转码提示"首次加载稍慢"）；评分页加性别置信度小字 + 遮挡切换开关；对比页加性别置信度；`npm run build` 通过
- **T8 测试**：36 → **67 passed**。新增：`test_pose_heuristics`（7：姿态方向/镜像对称/截断/遮挡合成图/质量门语义）、`test_clustering` 合并 pass 3 项、`test_face_admission`（10：男性组拒绝/女性组保留/单样本严阈值/全遮挡入库+置空/干净样本隔离/rep 优选/top-N 修剪/occlusion 端点/列表字段/personalizer 剔除）、`test_videos_stream` 三档 4 项（真实 ffmpeg 合成 mkv：remux 200+ftyp+头、transcode+探测回写、disabled 415、stream_mode 字段）、`test_videos_api`（3：library 筛选/叠加/libraries 端点）、`test_migrations`（4：旧 schema 补列/幂等/新库零迁移/存量行保留）
- **T9 文档**：README（特性/架构图/批次表 R1 ✅/技术栈勘误）、ARCHITECTURE §3.2 流水线顺序 + §3.5 三档表、DATA_MODEL（新列 + §2.3.1 迁移机制）、API_DESIGN（§3.3/3.4/3.5）、DECISIONS（ADR-014/015）、`.env.example`、`APP_VERSION=1.0.1`

### 真实片源验证（Z:\Sophos_data，HIMYM S01E01，1080p x265/EAC3）

- **扫描**：22 集全入库；E01 探测 `vcodec=hevc acodec=eac3` → stream_mode=**transcode** ✓
- **流播放实测**：转码档首字节 ~1.8s，fMP4 `ftyp` 正常，读 256KB 后断开（客户端断开→kill 子进程路径覆盖）；合成 h264+aac mkv 走 remux 档 200+头 ✓
- **处理（E01，5s 间隔，264 帧抽取）**：61 样本过门控 → 聚类 52 组 → **35 组被 identity 级性别裁决拒绝**（男性角色整组拒绝，样本级 female_prob 全部 ≥0.68 入库组）→ 合并 pass → 17 identity（12 单样本，暗场景人脸小质量 0.15-0.36，属预期；P3"同脸显著减少"待用户多集抽检）
- **姿态定标（probe_pose.py，90s 间隔 23 脸目检）**：低头 +52°（会剔）、强低头 +37°（保留）、清晰侧脸 |yaw| 55-76°（保留）、全侧 |yaw|>89°（剔除）——**发现并修复 pitch 符号反了**（3D 几何：低头压缩投影鼻嘴距 → 取负号才符合"低头为正"；已目检 4 帧确认）
- **遮挡**：本集无遮挡素材，occlusion_score 全 ≤0.23（合成图单测覆盖口罩场景 1.0）

### 环境/依赖变化

- 无新依赖（全部 stdlib + 既有 numpy/opencv/onnxruntime）；前端重新 build（dist 已更新）

### 已知问题与未尽事宜

- **质量门副作用**：暗场景（情景剧酒吧）大量人脸 quality<0.15 被拒，女性召回会下降——阈值可调（SOPHOS_MIN_QUALITY），待用户抽检权衡误杀/误放
- 17 identity 中 12 个单样本：合并阈值 0.78 下暗光侧脸不合并（ADR-014 已知风险，宁可碎片不过合）；用户若嫌碎可降 SOPHOS_MERGE_THRESHOLD 或升 CLUSTER_THRESHOLD
- 重封装/转码无真 Range（fMP4 分段 seek）；转码 1080p x265 CPU 负载高（veryfast 限流 1 并发）
- 存量部署升级后旧数据无新字段：migrations 补列 + 建议全量重处理（PLAN §6.2）
- Docker 实机验收仍为 M7 遗留（不阻塞）

### 下一步入口

1. **用户验收**：WebUI 处理 2-3 集 → 评分页核对"男性误入<5%、模糊/极端角度/半张脸不入库、同脸卡片减少"（PLAN §11）；mkv 播放（hevc 转码档）真机浏览器拖动体验
2. 阈值微调入口全在 `.env`：MIN_QUALITY / MAX_YAW_DEG / MAX_PITCH_DEG / MERGE_THRESHOLD / FEMALE_IDENTITY_THRESHOLD / TRANSCODE_*（docker-compose.yml 已留注释位）
3. tag `v1.0.1` 已随批次提交打上（沿 M7 先例）；抽检若需调阈值/修问题 → v1.0.2 修复批次（不回退本 tag）
4. 后续可选（PLAN §1.3/§4.2 列明本期不做）：det_10g 检测器、轻量性别复核模型、转码缓存、跨视频人物合并（ADR-007 扩展）

---

## 交接记录 — M7（2026-09-16）

### 完成内容
- **部署产物（根目录）**：`Dockerfile`（多阶段：node:22-alpine 构建前端 → python:3.12-slim + apt ffmpeg；模型不打镜像，卷挂载提供——体积与非商业许可原因）、`docker-compose.yml`（`/videos:ro` + `./data` 持久化卷 + SOPHOS_ 环境变量）、`.env.example`、`.dockerignore`
- **关键约定（Windows 宿主）**：DB 中 `video.path` 存容器内路径 `/videos/...`，工作目录一律填 `/videos`，换宿主目录只改 `.env` 映射不动 DB（已写入 docker/README 与 compose 注释）
- `scripts/backup_db.py`：`VACUUM INTO` 在线备份（本机实测 ok，输出 data/backups/）
- `docker/README.md` 部署指南定稿；README 快速开始改为 Docker 优先
- 本机 Docker 未安装（安装属系统级变更 WSL2+重启，留用户决策）——**容器实机验收是唯一未闭环项**

### 测试与验证结果
- `python -m pytest`：36 passed
- backup 脚本实测通过；Dockerfile/compose 人工静态审查（无 docker 环境无法 build）
- git：v0.5.0（基线）→ v0.6.0（M6 训练）→ 本批次提交 v0.7.0

### 下一步（完结路径）
1. 用户在有 Docker 的机器（或本机装 Docker Desktop 后）：`copy .env.example .env` → 设 `SOPHOS_VIDEOS_HOST` → `docker compose up -d --build` → `docker compose run --rm sophos python scripts/download_models.py` → WebUI 全流程
2. 验收通过：`git tag v1.0.0` + DEVELOPMENT_PLAN M7 勾掉末项
3. 遇到构建/路径问题：优先排查 apt 源、`/videos` 映射与 `safe.directory`（git 操作在容器内不需要）；结论回写 docker/README.md
4. 长期可选：真实片源抽检打分/聚类质量、mkv 转码播放评估、非线性个性化模型（ADR-008 可选项）、跨视频人物合并（ARCHITECTURE §6）

---

## 交接记录 — M6（2026-09-16）

### 完成内容
- **git 版本管理落地**：MinGit 2.55.0 便携版至 `tools/cmd/git.exe`（winget 不可用的替代）；`git init` + 全局 `safe.directory`（SMB 网络盘必须）+ `core.autocrlf=false`；基线提交 `a2fe155` + tag `v0.5.0`
- **personalizer 定稿（偏离原计划，已回写 ADR-008/013）**：不引入 sklearn，numpy 手写联合线性打分头——
  - 特征 [mean_embedding(512), base/100, rep_quality]；标签：score→(v-1)/9·100、**up→100/down→10**（ADR-008 定稿）、pair y=±1
  - 损失 = w_abs·MSE + w_pair·softplus(-y·(s_i-s_j))（RankNet/BT）+ L2；全批 GD 1500 轮 CPU 毫秒级
  - 门槛：总样本（abs+pair）≥20 拒训（NotEnoughSamples）；版本 `v{n}.npz + meta.json`（n_abs/n_pair/mae/r2/pair_acc）
- **滚动更新闭环**：activate → apply（全 identity 写 personalized_score）→ kv.active_pers_model → `aggregator.recompute_all`（视频表切换个性化分）；deactivate → 清个性化分回退 base_final
- **API**：`POST /api/train/start`（409 防重）/ `GET /api/train/status|versions` / `POST /api/train/activate/{v}` / `POST /api/train/deactivate`；train_handler 接入 job（结果=指标 meta）
- **自动触发**：每次评分/对比提交后检查总数 ≥20 且为 `SOPHOS_AUTO_TRAIN_EVERY`(30) 整数倍 → 自动提交训练 job（**不自动启用**）
- **WebUI 训练页**：训练按钮（带轮询）、启用状态标签、版本表（n_abs/n_pair/MAE/R²/pair_acc/时间/启用）、停用按钮
- requirements.txt 定稿：sklearn/joblib 不引入（注释说明）

### 测试与验证结果
- `python -m pytest`：**36 passed**（新增：样本不足拒绝、合成数据训练 pair_acc≥0.75 且好/差组个性化分差>20、启用→视频分切换、回退→基础分、train API 全流程）
- 实环境闭环（8030 端口实测）：4 视频 → 2 评分 + 18 对比 → train job done → activate v1 → 4 视频重算 → face 个性化分 47.94 落库 → 训练页 UI 渲染启用状态
- 已知现象（非 bug）：退化数据（同脸翻转、矛盾标签）指标 pair_acc=0/mae=50 属预期；有信号数据指标正常（见 pytest）

### 已知问题与未尽事宜
- "累计 ~200 条评分后排名上移"的主观验收需真实用户数据（计划项保留，随使用观察）
- 线性头为 MVP；非线性（MLP/GPU 微调）列为可选后续
- 训练页在"训练中"没有进度条（训练本身毫秒级，影响小）

### 下一步入口（M7 开工指引）
1. `docker/Dockerfile`：多阶段——node 阶段 `npm run build`；python 阶段 `python:3.12-slim` + `apt ffmpeg` + requirements + `frontend/dist` 拷贝；**模型文件走卷挂载**（`data/models/`），不打进镜像（许可+体积）
2. `docker/docker-compose.yml`：`/videos:ro`、`./data:/app/data`、`SOPHOS_WORK_DIRS`；注意 Windows 宿主路径映射后 `video.path` 一致性（DB 存容器内路径）
3. `.env.example`（SOPHOS_ 前缀全字段）
4. 端到端验收：`docker compose up -d` → WebUI 全流程（需本机 Docker 可用性确认，未验证）
5. README 定稿（一键 compose）+ 备份说明（`VACUUM INTO`）+ mkv 转码播放评估结论回写 ARCHITECTURE §3.5
6. 收口：pytest 全绿 + 容器内验收 + 三份文档回写 + git tag v1.0.0

---

## 交接记录 — M5（2026-09-16）

### 完成内容
- **M5 后端（新增端点全部带测试）**：
  - `GET /api/faces/pair?strategy=similar|random`（`services/pairs.py` 选对：similar 优先分差≤10、random 任意；排除已对比对；全对比完 → 404 NO_PAIR）
  - `POST /api/faces/pair/compare`（winner≠loser 校验、存在性校验）→ `pair_comparison` 表
  - `POST /api/faces/{id}/rating`（score 1-10 / thumbs up|down 校验）+ `GET /api/ratings/stats`（histogram/pair_count/unique_faces_rated）
  - faces 列表/详情增加 `my_rating`（最新一条）与代表帧 `timestamp_sec`（批量查询）；unrated 过滤修了 count 未过滤的 bug
- **前端**（`frontend/`，Vue3+Vite+ElementPlus，手写工程 + npm install 79 包 + build 成功）：
  - 四个 tab：评分（卡片流/1-10 按钮/👍👎/跳过/自动下一张/批量进度）、对比（A/B 点选/策略切换/换一对）、视频库（对照表排序搜索分页/行点击播放器弹窗）、任务与设置（目录增删/扫描处理触发/job 表 2s 轮询）
  - `main.py` 挂载 `frontend/dist`（html=True；dist 缺失时 API-only 模式）
- **node 便携版**：`tools/node-v22.23.2-win-x64/`（node 22.23.2 / npm 10.9.8），用前 `set PATH=X:\Sophos\tools\node-v22.23.2-win-x64;%PATH%`

### 测试与验证结果
- `python -m pytest`：**32 passed**
- 真实浏览器（IAB）验收实录：①评分页空状态正确 → 新增 lena_b.mp4 处理出 identity#2（base 58.3）→ 评分卡片完整渲染 → 点击「7」→ 自动下一张 → 空状态（闭环）②对比页策略切换 + NO_PAIR 空状态 ③视频库表（综合分 64.1 标签）→ 行点击 → 播放器 readyState=4、3s 视频播放中、无错误 ④任务页 job 表 4 条历史（进度/结果/错误）全对
- 截图存档：会话 artifacts（视频库页）

### 已知问题与未尽事宜
- 多人对比的真实交互待用户真实片源入库复核（单人库下 pair 只能验证空状态与 API 语义）
- mkv/avi 的 415 提示路径未在浏览器实测（后端逻辑有测试，风险低）
- 构建产物 1.1MB JS（Element Plus 全量引入），可后续按需引入优化
- git 仍未安装（持续遗留项）

### 下一步入口（M6 开工指引）
1. `pip install scikit-learn joblib`（requirements 取消注释）
2. `services/personalizer.py` 按 ARCHITECTURE §3.6/§3.7 + ADR-008/013 实现：
   - 训练集：identity 级，X = mean_embedding + base_score + quality；Y = 评分映射 0-100（score:(v-1)/9*100；thumbs up=100/down=10——定稿后回写 ADR-008）；同一 identity 取最新；pair 样本走 pairwise logistic（共用打分头）
   - 模型 Ridge 起步（sklearn），版本存 `data/models/personalizer/v{n}.joblib` + meta.json（n_samples/mae/r2）
   - `POST /api/train/start`（job）+ `GET /api/train/status|versions` + `POST /api/train/activate/{version}`（写 kv `active_pers_model`）
   - 启用后：批量更新 face_score.personalized_score + `aggregator.recompute_all`（滚动更新）
   - 自动触发：每新增 `SOPHOS_AUTO_TRAIN_EVERY`(30) 条评分；样本 <20 拒绝训练（409 NOT_ENOUGH_SAMPLES）
3. WebUI 训练页（新 tab）：样本量/指标/版本列表/启用按钮
4. 测试：合成评分数据训练冒烟、启用后 video_score 重算、两评分映射、pairwise 路径
5. 收口：pytest 全绿 + 浏览器验证"评分→训练→个性化分变化"闭环 + 三份文档回写

---

## 交接记录 — M4（2026-09-16）

### 完成内容
- **颜值模型定稿（ADR-006 已解决）**：etrain 源 Google Drive 已失效 → 换源 HF `Gustrd/SCUT-FBP5500-PyTorch-Model`（MIT）。关键坑：该权重是 Torch7 Caffe 转换链路产物，模型卡写的 `models.resnet18` 结构是错的——实际张量形状与 torchvision ResNet-18 一致，仅命名不同（stem 在 `group1.` 下、块内卷积在 `layer*.B.group1.` 下、fc 叫 `group2.fullyconnected`）。`scripts/convert_beauty_model.py` 做键名映射后 strict 加载成功，导出**单文件** `beauty_scut.onnx`（44.7MB，注意新导出器会拆 .data 外部文件，须 `dynamo=False`），torch/ort 数值一致（maxdiff 2.4e-07）
- **打分实测定稿**（`scripts/probe_beauty.py`）：预处理 = bbox 外扩 25% → 短边 256 → 中心裁 224 → /255 → ImageNet 归一化 RGB。lena 人脸区 raw≈3.59（→64.7 分），整图对照 2.19（构图敏感，行为正常）。不喂 arcface 对齐图（训练集是整张正脸照）
- `services/scorer.py`：BaseScorer（ONNX，0-100 映射，版本 `scut_resnet18_v1`）
- `services/aggregator.py`：top-3 均值 + 路人过滤（n_samples≥2 或代表帧质量≥0.35）→ `video_score` 滚动更新；process 完成即聚合
- 流水线接入：抽帧循环内逐样本打分（FaceSample.base_score），代表样本分写 `face_score`；beauty 模型缺失时警告并跳过（不阻塞处理）
- 修 bug：videos API `_score_payload.identity_count` 键覆盖 video 行面容数（曾恒返回 null）
- **M5 前置已备**：node v22.23.2 + npm 10.9.8 便携版解压至 `tools/node-v22.23.2-win-x64/`（npm 用 `tools\node-...\npm.cmd`）

### 测试与验证结果
- `python -m pytest`：**27 passed**（新增聚合器 3 项 + 打分链路端到端 1 项；M3 的"无分"断言改为条件断言）
- torch 2.14.0+cpu / torchvision / onnx / onnxscript 已入 venv，**仅为转换期依赖**（requirements.txt 注释说明，运行时不装）

### 已知问题与未尽事宜
- 颜值模型只做了 lena 单点合理性实测；**真实片源 20 张人工抽检**待用户入库后做（计划项保留）
- 训练集为 SCUT-FBP5500（多为亚洲+欧美正脸照），对动画/侧脸/大角度的泛化未知
- git 仍未安装（跨批次版本管理建议项）

### 下一步入口（M5 开工指引）
1. **前端初始化**：`cd frontend && set PATH=X:\Sophos\tools\node-v22.23.2-win-x64;%PATH% && npm create vite@latest . -- --template vue` → `npm i element-plus axios`；vite.config 设 `server.proxy: {'/api':'http://127.0.0.1:8000'}`
2. 三个页面（契约见 `docs/API_DESIGN.md`，无需改后端）：评分页（`GET /api/faces?unrated=1` 卡片流 + POST rating + 自动下一张）、视频库页（`GET /api/videos` 表格排序 + stream 播放弹窗）、任务页（workdirs/scan/process + jobs 轮询）
3. **对比评分（ADR-013，M5 新增需求）**：`GET /api/faces/pair`（选对策略 similar/random，排除已对比对）+ `POST /api/faces/pair/compare` 后端端点——**这两个端点还没实现**，写完再接 UI；数据落 `pair_comparison` 表（M4 已建好）
4. 构建产物托管：`npm run build` → FastAPI StaticFiles 挂 `frontend/dist`（main.py 预留位置）
5. 收口：pytest 全绿 + 浏览器全流程验收（目录→扫描→处理→评分/对比→视频表→在线播放）+ 三份文档回写

---

## 交接记录 — M3（2026-09-16）

### 完成内容
- **前置落地**：ffmpeg 9.0.1 绿色版解压至 `tools/ffmpeg-9.0.1-essentials_build/`（frame_sampler 自动定位 PATH > tools）；模型三件套已装 `data/models/`（det_500m 2.5MB / genderage 1.3MB / w600k_mbf 13.6MB，来自 buffalo_s）；M3 依赖入 venv（**numpy 2.5.3 + onnxruntime 1.30 + opencv 5.0**，Python 3.13 必须 numpy≥2.1，requirements 已定稿）
- `frame_sampler.py`：间隔抽帧 + 定位 + 清理；`clustering.py`：Chinese Whispers（余弦阈值）
- `face_engine.py`：SCRFD→对齐→genderage→embedding 全链 + 质量过滤；**两个实测定稿**（关键交接资产）：
  - SCRFD 解码修复：anchor 中心必须先 reshape(K,2) 再 stack 复制（曾因 3-D 直接 stack 轴序错误导致框落到 (0,0) 格，用探针逐位比对定位）
  - 预处理实测（`scripts/probe_models.py` 可复现）：genderage = **96×96、raw 0-255、RGB**（置信度 0.999；BGR 会退化成 0.46 均匀输出——探针标签曾打反，已修正）；fc1=[女,男,age/100]，**gender 0=女**；w600k_mbf = (x-127.5)/127.5 RGB 112
- `pipeline.py`：process 流水线（幂等：重跑级联删派生数据，**存在用户评分则拒绝重处理**；单视频失败不阻塞；即处理即清帧）；帧/缩略图读写走 imdecode/imencode（Windows 中文路径安全）
- API：`POST /api/process/start`、`GET /api/faces(/{id})`、`/api/thumbs/{id}.jpg` 静态托管
- 修 bug：videos API `_score_payload.identity_count` 键覆盖 video 行面容数（曾恒返回 null）

### 测试与验证结果
- `python -m pytest`：**23 passed**（M2 9 项 + M3 14 项；lena 真人视频端到端：检测 0.81 → female_prob 0.999 → 1 identity + 缩略图 + API 全通）
- 探针脚本保留：`scripts/probe_models.py`（预处理实测）、`probe_scrfd*.py`（布局排查过程，可删）

### 已知问题与未尽事宜
- 多人真实视频的聚类效果与性别过滤准确性未验证（无真实片源）——用户首次入库后人工抽检，阈值 `SOPHOS_CLUSTER_THRESHOLD`(0.68) 可调
- 未安装 git（建议项持续遗留）；det_10g（更准检测器）未下载，`--pkg buffalo_l` 可选
- 抽帧时间戳为近似值（帧序号×间隔），非精确 PTS

### 下一步入口（M4 开工指引）
1. **SCUT 颜值模型定稿（ADR-006，当前最大风险）**：调研顺序 a) 开源 ONNX 预训练权重（github 搜 scut-fbp onnx/beauty predict）；b) 下载 SCUT-FBP5500 数据集（~1.9GB，学术许可）自训 MobileNet 回归头并导出 ONNX；c) 过渡占位：用 embedding 线性代理分（仅打通链路，不可信）
2. `services/scorer.py`：`BaseScorer` 接口 + ONNX 推理（输入=对齐 112 图，输出 0-100），落 `face_score.base_score + base_model_version`
3. `services/aggregator.py`：top-K(3) 平均 + 路人过滤（n_samples≥2 或质量阈值）→ `video_score`；处理完成即滚动更新
4. 接入点：process 流水线在聚类后调用 scorer；`GET /api/videos` 排序字段此时开始有值
5. 测试：聚合公式/top-K/dirty 重算；打分链路用假 scorer 单测 + 真模型冒烟
6. 收口：pytest 全绿 + 三份文档回写（批次纪律）

---

## 交接记录 — M2（2026-09-16）

### 完成内容
- 文档：**两两对比评分**已入体系（ADR-013、DATA_MODEL §2.9 `pair_comparison` 表、API §3.6 pair 端点、M5/M6 计划项）——现在共三种评分方式
- 配置：`app/config.py`（pydantic-settings，SOPHOS_ 前缀 + .env；work_dirs 支持 JSON 数组/分号分隔）
- 数据库：`app/db/models.py` 九张表建齐 + `session.py`（init_engine 幂等）+ `services/kv.py`（工作目录存 kv_setting）
- 扫描：`services/scanner.py`——递归+扩展名过滤+增量（新增/变更→pending、消失→missing 不删行、复现→恢复），跳过隐藏目录
- 任务：`services/jobs.py`（单 worker 线程 + 队列 + job 进度/结果/错误落库 + 启动清理遗留 queued/running）与 `services/pipeline.py`（scan 已实现；process/train 为 M3/M6 的明确报错占位）
- API（统一错误体 {code,message}）：health / workdirs(GET·POST·DELETE) / scan-start(202、409 防重) / jobs(列表/详情) / videos(分页·搜索·状态过滤·白名单排序) / videos/{id} / videos/{id}/stream（Range 206）

### 环境/依赖变化
- venv 已装齐 M2 依赖；确认 X: 为 SMB 网络盘（\\192.168.50.100\CODE）→ SQLite **不启用 WAL**、**不启用外键强制**（级联删除由应用层负责，已写入 db 模块 docstring）

### 测试与验证结果
- `python -m pytest`：**9 passed**（scanner 增量×2、workdirs CRUD、scan 任务端到端、409、列表/详情、Range/415/404、health）
- 真实 HTTP 冒烟（uvicorn 8010 + curl）：health ok(v0.2.0) → 添加目录 → scan job done(result added=1) → videos 列表出片 → `Range: bytes=0-99` → **206** + 100 字节

### 已知问题与未尽事宜
- 宿主机无 git（建议项未完成）、无 ffmpeg（M3 必需）；Range 语义用合成文件验证，真实编码视频播放验证并入 M3
- videos 搜索未转义 LIKE 通配符（%/_），MVP 接受
- job 无取消端点（状态机已预留 cancelled）

### 下一步入口（M3 开工指引）
1. **前置**：安装 ffmpeg（`winget install Gyan.FFmpeg`），验证 `ffmpeg -version`；装不上则先写代码、真实运行依赖 M7 容器
2. **模型**：编写 `scripts/download_models.py` 下载 SCRFD det_10g / genderage / w600k_mbf 到 `data/models/`（来源与校验见 data/models/README.md；insightface buffalo_l zip 需解包）
3. 实现：`frame_sampler.py`（subprocess 调 ffmpeg）→ `face_engine.py`（ONNX 封装 + 假引擎单测隔离）→ `clustering.py` → process 流水线接入 `pipeline.py`（幂等：重跑先删该 video 的 face/identity/score 行）
4. API：`POST /api/process/start`、`GET /api/faces`、`/api/thumbs/{id}.jpg`（StaticFiles 挂载 data/thumbs）
5. 收口：pytest 全绿 + 真实视频全链路冒烟 + 按"批次间交接纪律"更新三份文档

---

## 交接记录 — M1（2026-09-16）

### 完成内容
- 全套文档：README、ARCHITECTURE、DATA_MODEL、API_DESIGN、DEVELOPMENT_PLAN、HANDOFF、DECISIONS
- 工程骨架：`backend/app` stub（main/config + services 占位模块）、`backend/tests/test_health.py`、`frontend|docker|scripts|data/models` 占位 README、`.gitignore`
- 技术选型定稿（12 条 ADR）与 7 个批次计划（含验收标准、风险表、交接纪律）

### 环境/依赖状态
| 项 | 状态 |
|---|---|
| Python | 3.13.15，位于 `C:\Users\Wang\AppData\Local\Programs\Python\Python313\python.exe`，**不在 PATH** |
| venv | ✅ 已创建 `backend\.venv`；M2 依赖（fastapi/uvicorn/sqlalchemy/pydantic/pytest 等）**已安装** |
| git | **未安装**（建议 M2 安装并做首次提交） |
| ffmpeg | 宿主机未装（M3 前需装，或依赖 M7 容器内置） |
| Docker | 未验证（M7 使用） |
| 命令行环境 | cmd（无 bash 工具链；`tail` 等不可用） |
| 磁盘 | ⚠️ X: 为**网络映射盘** `\\192.168.50.100\CODE`（venv 在其上运行正常，但 IO 延迟高；模型/数据库落 `data/` 时注意性能） |

### 可运行性验证（已实测）
- `python -m pytest`（backend 目录）：**1 passed**（`tests/test_health.py` 骨架冒烟）
- `uvicorn app.main:app --port 8000` 启动成功，`GET /api/health` 返回 `{"status":"ok","version":"0.1.0"}`
- ONNX 模型文件均未下载（M3 的 download_models.py 负责落地）

### 未尽事宜（移交下一批次）
- 见 `DEVELOPMENT_PLAN.md` M2 任务清单（9 项）。

### 下一步入口（M2 开工指引）
1. 建议先装 git（可选）并 `git init` + 首次提交（tag `v0.1.0`）。
2. 创建 venv 并安装 M2 依赖（requirements.txt 中 M2 段已可直接安装）：
   ```cmd
   cd /d X:\Sophos\backend
   C:\Users\Wang\AppData\Local\Programs\Python\Python313\python.exe -m venv .venv
   .venv\Scripts\pip install -r requirements.txt
   ```
3. 按序实现：`config.py` → `db/`（一次建齐八张表）→ `services/scanner.py` → `services/jobs.py` → API 路由 → `stream`。
4. 完成后执行 `python -m pytest`，按"批次间交接纪律"更新三份文档。

---

## 交接记录模板（每批次复制一份填写，新记录放最上方）

```markdown
## 交接记录 — Mx（日期）
### 完成内容
### 环境/依赖变化
### 新增/修改的关键文件
### 测试与验证结果（pytest 输出摘要、人工验收记录）
### 已知问题与未尽事宜
### 下一步入口（下一批次开工指引）
```
