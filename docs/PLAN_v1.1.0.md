# PLAN — v1.1.0（R5：任务控制 + 面容上限 + 对比层级重排 + 大规模优化）

> 用户 2026-09-17 反馈五项 + 规模目标修订：①转码视频拖动进度条报"解码失败"；②任务暂停/取消；
> ③人脸处理自动分批、批间可插队其他任务；④**同一视频文件的同一个人最多保留 5 张面容**（可在设置中调整）；
> ⑤对比优先级：不同视频中不同的人 > 同一视频中不同的人 > 不同视频中可能相同的人。
> 规模目标：**数万个视频**流畅运行（打分图片出现慢、缩略图体积大/检索难等问题）。

## 1. 播放 seek 修复（T1，ADR-021）

**根因**：remux/transcode 档是 ffmpeg 管道产出的 fMP4 渐进流，**没有 HTTP Range**。
浏览器拖动到未缓冲位置 → 发出注定失败的 Range 请求（服务端按 200 全量重发）→ MediaError
code 3（解码失败）→ 前端弹窗关闭播放器。

**方案（服务端 seek）**：
- `GET /api/videos/{id}/stream?ss=<秒>`：remux/transcode 档在 `-i` 前注入 `-ss`
  （输入 seek：转码档精确、copy 档对齐关键帧），输出时间戳从 0 重排；direct 档有真 Range，忽略。
- 前端拦截 `<video>` 的 `seeking`：目标不在 `buffered` 范围（±1s 余量）→ 250ms debounce
  后带 `?ss=` 重新起流（保留暂停态），显示"跳转中"标签；缓冲区内 seek 走原生路径。
- 已缓冲区内点击不再触发网络请求；未缓冲区域点击 = 服务端定位，无需"等待缓冲"。

## 2. 任务暂停/取消/恢复（T2，ADR-022）

- 状态机扩展：`queued → running → done | failed | cancelled | paused`；
  resume：paused → queued 重新入队（handler 对部分进度幂等：process 按视频重做、scan 增量）。
- **协作式中断**：`POST /api/jobs/{id}/pause|cancel` 置请求标志（queued/paused 直接改库）；
  handler 在检查点调用 `jobs.check_point()` → `JobInterrupted` → worker 落终态。
  检查点位置：process 每视频边界 + 抽帧循环逐帧、scan 每工作目录、train 起点。
- process 中断时当前视频回退 pending（帧文件由 finally 清理）；`reset_stale_jobs` 保留 paused（重启后可恢复）。
- API：`POST /api/jobs/{id}/pause | cancel | resume`（409 JOB_STATE / 404）。
- 前端任务页：操作列（暂停/取消/恢复；queued/running/paused 分态显示），"将在当前步骤结束后生效"提示。

## 3. process 自动分批链（T3，ADR-022）

- `process_batch_size`（默认 4，runtime settings 可调）：`POST /api/process/start` 把 pending
  视频切片，**只提交第一批**（params 带 `chain: true`）；批完成后 handler 经检查点
  （有暂停/取消请求则链条停止）链式提交下一批 pending。
- **批间插队**：任意时刻至多一个 process job 在队列；其他任务（scan/train）提交后排在
  下一批之前，自然插队。409 防重窗口无缝（链提交发生在 handler 内、job 仍 running）。
- 无 pending 视频 → 409 `NO_PENDING_VIDEOS`。
- 多线程并行人脸处理**不做**：ONNX Runtime/ffmpeg 内部已并行，并行视频会倍增内存并加剧
  SMB 网络盘 SQLite 写竞争；分批+插队达成同样的界面响应目标（ADR-022）。

## 4. 面容数上限（T4，ADR-023）

- `max_faces_per_person`（默认 **5**，0=不限，runtime settings 可调）：聚类→性别裁决→
  双层 merge 之后执行 `clustering.cap_groups` 上限 pass。
- **组件化封顶**（无人物真值下的"同一个人"近似）：以余弦 ≥ `cap_merge_floor`(0.50) 且无
  同帧共现做单链连通分量 = "可能同一个人"；每个组件独立反复合并组均值余弦最高、无共现、
  ≥ floor 的对，直到 ≤ N。多女性共存的视频各封各的顶，不会为凑总数跨人强并。
- 护栏与 merge pass 同口径：共现一票否决（合并后时间戳并集传递）、低于 floor 永不强并
  （宁超限不误并）。job result 增 `cap_merged_groups`。
- 运行时设置：`GET/PUT /api/settings`（kv_setting 存覆盖值，白名单 + 类型/范围校验），
  WebUI「任务与设置」页设置卡片（分批大小 / 每人面容上限），立即对后续任务生效。

## 5. 对比层级重排（T5，ADR-023 修订 ADR-019）

用户修订的优先级（diverse 策略，`pairs._pick_diverse`）：

| tier | 语义 | 判定 |
|---|---|---|
| ① 0 | 跨视频不同人物 | embedding 明显不同（conf 0.9）先于证据不足（0.5） |
| ② 1 | 同视频不同人物 | 同帧共现（conf 1.0）先于 embedding 明显不同（0.9） |
| ③ 2 | 跨视频可能同一人 | conf 0.1（很像），降级不禁止 |
| ④ 3 | 同视频证据不足/疑似同人 | R5 面容上限后此类碎片已少，信息量最低 |

层内按 分差接近 + 抖动；①② 层内保留置信度次序。R4 旧层级（明显不同人物 > 不同视频）
被本表覆盖；R4 测试相应重写（跨视频明显不同 > 同视频共现 为新语义）。

## 6. 大规模优化（T6-T9，ADR-024；目标：数万视频流畅运行）

规模测算：3 万视频 × ~10 identity ≈ **30 万 identity / 百万级 face 行 / 30 万缩略图**。

### T6 缩略图（打分图片慢 + 缩略图大/检索难）
- **分片存储** `thumbs/{id//1000:03d}/{id}.jpg`（单目录 ≤1000 文件，治 NTFS/SMB 平铺大目录）；
  旧平铺文件 `find_thumb` 回落兼容，重处理自然迁入分片。
- **动态端点**替代 StaticFiles：`GET /api/thumbs/{id}.jpg` + `Cache-Control: immutable`，
  `rep_thumb` 带 `?v={updated_at}` 内容版本（identity 重建后 URL 变化自然失效）。
- 重处理**清理旧缩略图**（旧实现遗留孤儿文件无限累积）。
- 评分页**预取**后续 3 张（打分点击后下一张立即可见）。

### T7 数据库与查询
- 复合索引 `face(video_id, timestamp_sec)`（共现滑窗/共现 SQL），ORM `__table_args__` +
  旧库 `run_migrations` 守卫补建（列存在性检查，幂等计数不变）。
- `GET /api/faces?unrated=1`：`NOT IN (SELECT DISTINCT …)` → **NOT EXISTS** 关联子查询。
- faces 列表本就分页（≤200）；payload 无 embedding 大字段。

### T8 pair 选对可扩展化
- 原 `_identity_rows` 全量加载 embeddings（30 万 × 2KB = GB 级）+ 全量候选 O(n²) 不可行：
  **轻量行（id/video_id/score）+ 超库随机抽样**（`pair_sample_size=500`，库小于该值保持
  全量精确语义）；embedding 仅按候选集加载。
- 共现改 SQL 拉取 + **指纹缓存**（face 行数/最大 id，连续评分会话零重算）。
- 已比对：全量加载 → **按候选集一次查询**（winner 索引）。

### T9 训练与打分应用机制
- 训练集特征**只加载有评分/对比的 identity**（用户生成、千级；原实现为训练扫全库）。
- pairwise 梯度**向量化**（原逐对 Python 循环 × 1500 轮为分钟级热点）+ exp 溢出裁剪。
- `apply_version` 分块（2000/块）keyset 翻页 + **SQLite UPSERT 批量写** + 逐块提交 +
  跳过已当前版本（幂等增量）；`aggregator.recompute_all` 分块提交（500/块）。

## 7. 任务清单与验收

| # | 任务 | 验收 |
|---|---|---|
| T1 | stream ?ss= + 前端 seek 拦截 | pytest：-ss 注入位置 / remux·transcode ss 200 ftyp / direct 忽略 |
| T2 | jobs 暂停/取消/恢复 | 慢 handler 生命周期测试 / queued 直改 / 409 / 中断回退 pending |
| T3 | process 分批链 | 链式提交下一批断言 / 取消停链 / start 按批切片 total_batches / 无 pending 409 |
| T4 | cap_groups + settings API | cap 单测 7 项（组件隔离/桥接/共现/floor）+ settings 校验 |
| T5 | diverse 层级重排 | 8 项 diverse 测试按新层级重写/新增 |
| T6 | 缩略图分片+端点+缓存 | 分片路径/回落/端点 404/immutable 头/重处理清理 |
| T7 | 索引+NOT EXISTS | migrations 幂等计数不变 / faces unrated 回归 |
| T8/T9 | pair 采样+训练/apply 分块 | personalizer 全流程回归 / 训练指标与 R4 一致量级 |
| 全局 | pytest 全绿 + 前端 build | 126 passed（R4 100 → +26）/ `npm run build` ✓ |

## 8. 明确不做（本期）

- 不做多线程并行处理多个视频（ADR-022 记录理由）；
- 不做缩略图多尺寸双份存储（单文件 + immutable 缓存 + 预取已达标）；
- 不迁移 PostgreSQL / 不引入 WebP；
- 不做转码缓存（沿 R4 决策）。
