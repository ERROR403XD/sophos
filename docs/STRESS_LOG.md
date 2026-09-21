# 压力测试日志（STRESS_LOG）— v1.1.0 大规模优化验证

> 目标（用户 2026-09-18）：在本地开启**测试 → 开发 → 迭代**循环，直至中止；每阶段留交接记录。
> 思路采纳：用本地测试视频（`<network-share>\temp\<sample-video-dir>`，HIMYM S01）生成**可重复**的低质量
> 短切片 + 合成切片 + 直插种子库，对 v1.1.0 大规模优化（ADR-024）做压测。
> **测试库约定**：目录 `<stress-data>\`，总占用上限 **1TB**（工装强制守卫，
> 超 90% 中止写入）；**压测通过、取得明显成效前保留测试库**，不自动清理。
> 压测**不要求**检出面容（用户明确）：流水线照常执行检测/门控/聚类链路，产量不作为验收项。

## 阶段总览

| 阶段 | 内容 | 状态 |
|---|---|---|
| S1 | 压测工装（gen_clips / seed_db / bench + 1TB 守卫） | ✅ 完成 |
| S2 | 查询层基准：3 万视频 / 24 万 identity / 96 万 face 规模端点计时 | ✅ 通过（修复后复测） |
| S3 | 真实流水线压测：620 切片 scan+process 分批链 + 暂停/恢复/取消 | ✅ 通过 |
| S4 | 缩略图基准（6 万分片缩略图下端点 p50=14.9ms） | ✅ 并入 S2 |
| S5 | 训练/打分应用基准（train 3.1s / apply 41s / recompute_all 85s） | ✅ 并入 S2 |
| S6 | 问题修复 + 回归（pytest 126 基线不回退） | ✅ 修复 5 项，全绿 |

---

## S1 — 压测工装（2026-09-18）

### 环境

- 工作区 `<stress-data>\`（NAS temp 卷，约 13.4TB 空闲；与项目盘 X: 不同卷）。
- 片源：HIMYM S01E01（1327s，1080p x265）——真实 DB video#1 的 path（从 `data/sophos.db` 读取，
  规避 cmd 对 `￡` 特殊目录名的处理问题）。
- ffmpeg/ffprobe：`tools/ffmpeg-9.0.1-essentials_build`（自动定位）✓。

### 工装（`scripts/stress/`）

| 文件 | 用途 |
|---|---|
| `_common.py` | settings 引导（env 优先于 .env，测试数据目录隔离）+ **1TB 目录守卫**（`ensure_cap`，>90% 上限即中止） |
| `gen_clips.py` | 从源片确定性切片（起点按 index+seed 推导，**可重复**；已存在跳过，幂等）。真实切片：6s/640px/crf35/fps12（低质量）；`--synthetic`：testsrc 合成片（无人脸，纯量级） |
| `seed_db.py` | 直插种子库：--videos × 平均 8 identity（同"演员"40 人复用制造跨视频同人对，组内 cos≈0.75-0.9）× 4 face；可选分片缩略图文件、评分、对比；全程确定性 |
| `bench.py` | 端点计时（TestClient 进程内）：health/videos 首页·深页·搜索/faces unrated 冷热/pair diverse 冷热（共现指纹缓存）/thumbs p50-p90/settings；`--with-train` 时含 train / apply（24 万 identity 分块 UPSERT）/ recompute_all（3 万视频）计时；报告落 `stress_bench.json` |
| `sanity_clip.py` / `_dump_frames.py` / `_probe_source.py` | 小样检测验证 / 抽帧目检 / 片源探测 |

### S1 记录与决定

- 3 个真实切片小样：抽帧 3 帧/片正常；face_engine 检出样本 0——目检确认画面有人脸
  （640px 下人脸约 60-90px，低于 min_face=80，且 crf35 压缩后质量门 0.30 难过）。
  **用户裁定：压测不需要确保检出面容**——检测/门控/聚类链路照常执行即可，不调整参数、
  不做选点优化。面容产量不作为压测验收项。
- 真实切片 120 个 + 合成切片 500 个 + 种子库（3 万视频/6 万缩略图/2 千评分/5 千对比）
  并行生成中（结果回填 S2/S3 节）。

---

## S2 — 查询层基准 ✅（2026-09-18，含 S4/S5；修复后复测）

**规模**（`<stress-data>\data_query`，全确定性可重建）：30,000 视频 / **240,341 identity**（含 512 维 embedding）/ **961,364 face** / 60,000 分片缩略图 / 2,000 评分 + 5,000 对比。DB ≈ 1.2GB，测试根目录 1.18GB（≪1TB 上限）。

### 计时结果（中位数；SMB 网络盘）

| 指标 | 首轮（R5 交付态） | S6 修复后 | 提升 |
|---|---|---|---|
| videos 首页（sort final_score） | 23ms | 25ms | — |
| videos 深页（page 500） | 707ms | **171ms** | 4.1× |
| videos 搜索 | 36ms | 20ms | ✓ |
| faces unrated（暖） | 118ms | **95ms** | ✓ |
| **pair diverse 暖态** | **6.37s** | **0.52s** | **12.2×** |
| **pair diverse 冷态** | **16.99s** | **0.91s** | **18.7×** |
| 缩略图端点 p50 / p90 | 17.9 / 38.7ms | **14.9 / 16.1ms** | ✓ |
| 训练（2000 评分 + 5000 对比） | 3.6s | 3.1s | ✓（向量化后） |
| apply（24 万 identity 批量打分） | 45.2s | 41.0s | ✓（一次性成本） |
| recompute_all（3 万视频） | 92.4s | 85.4s | ✓（一次性成本） |

**结论**：30k 视频规模下评分/对比/视频表/缩略图全链路流畅（交互级 <100ms，对比选对 <0.7s）；激活模型的一次性成本（apply + recompute ≈ 2 分钟）可接受。**压测通过**。

### 遗留与接受项

- faces 深分页（page 1000）≈ 0.43s：OFFSET 深翻页固有成本，UI 实际浅分页，接受。
- apply/recompute_all 为激活/重算时的一次性成本，已分块化，接受。
- 随机抽样语义：库 >500 identity 时 pair 选择为近似排序信号（ADR-024 既定）。
- 复测入口：`python scripts/stress/bench.py --data-dir <stress-data>/data_query --no-train`。

## S3 — 真实流水线压测 ✅（2026-09-18）

**实例**：uvicorn :8040，`SOPHOS_DATA_DIR=<stress-data>/data_pipe`（与主库完全隔离），模型目录指向 `<project-root>/data/models`。

**负载**：120 真实切片（6s/640px/crf35，来自 E01）+ 500 合成切片（4s testsrc）= **620 视频**。

| 项 | 结果 |
|---|---|
| 扫描 | 首次 620 added / 72.4s（含编码探测）；重扫 **unchanged=620 / 2.1s**（幂等 ✓） |
| process 分批链 | batch=4 → **156 个链式 job 全部 done**，620 视频共 **106.9s（≈5.8 视频/秒）** |
| 暂停 | done=259 时暂停 → **进度冻结**（3s 前后状态计数一致）、链停止、无活动 job ✓ |
| 恢复 | resume → 从 pending 续跑到 620 全 done ✓ |
| 取消 | 6 视频置回 pending + batch=1 重启 → cancel → **链停止**，614 done + 6 pending ✓ |
| 面容产量 | identities=3 / faces=4（低质切片过不了质量门，符合预期，非验收项）；缩略图分片写入正常 |

**发现并修复（S6）**：
1. `GET /api/jobs?active=true` **500**（压测驱动轮询命中）——R5 将 `Job.status.in_("queued",...)` 误写成变参，SQLAlchemy 2.x 需传序列。修复后端点正常。属**真实缺陷**（WebUI 任务页 active 轮询同样会命中）。
2. 压测驱动自身语义修正：`active=true` 列表含 paused（UI 需要），驱动判定"在干活"须过滤 queued/running。
3. **播种工装 v1 教训（SMB 规律）**：ORM `add()+flush()` 逐 identity 触发单行 RETURNING INSERT，百万行 × SMB 往返 = 小时级不可用；v2 改 Core `insert().executemany()`（2000 行/批，id 显式分配免 RETURNING、逐批提交）→ **快约两个数量级**（30k 视频 1s；2500 视频 + 2 万 identity + 8 万 face 6s）。生产代码中 apply_version 已是批量 UPSERT 口径，无需改动；此教训佐证 ADR-024"大库写路径必须批量化"的判断。

**回归**：`in_` 修复后 **pytest 126 passed** ✓（2026-09-18，S6 基线保持）。

## S4/S5 — 并入 S2 基准 ✅（缩略图 p50/p90、train/apply/recompute_all，见表）

## S6 — 修复与回归 ✅（2026-09-18）

**生产代码修复**（全部回归 **pytest 126 passed**）：

1. `GET /api/jobs?active=true` 500：`Job.status.in_("a","b")` 变参误用（SQLAlchemy 2.x 需传序列）——真实缺陷，WebUI 任务页轮询同样命中（S3 发现）。
2. **SQLite 页缓存调优（收益最大）**：SMB 上默认 2MB 页缓存使随机 PK 点查全走网络（608 点查 ≈3.5s，profile 定位）。`session.py` 引擎级 `PRAGMA cache_size=-131072`（128MB）+ `temp_store=MEMORY`——videos 深页 4.1×、pair 全链受益。
3. **共现计算范围收缩**（pairs.py）：diverse/similar 只需候选集内身份的共现证据 → `WHERE identity_id IN (候选)`（~2000 行）替代全表 96 万行扫描；全局指纹缓存删除（随机抽样下无意义）。冷态 17s → 0.9s。
4. **候选对计算向量化**（pairs.py）：12.5 万候选对的余弦/分层原为 Python 逐对循环（暖态 6.4s），改 500×500 矩阵乘 + numpy 分层/lexsort，语义与逐对版一致（15 项 pairs 测试全过）。暖态 6.4s → 0.52s（12.2×）。
5. **抽样寻道优化**（pairs.py）：随机 id 点查 → 分段连续 id 段（寻道次数降到段级）；einsum 的 125k×512 临时矩阵 → 全矩阵一次乘。

**工装修复**：播种 v1 ORM 逐行 flush 在 SMB 上小时级 → v2 Core executemany 快约两个数量级（教训见 S3 节 #3）；压测驱动 active 判定口径（paused ≠ 在干活）。

**产物**：`<stress-data>\data_query\stress_bench.json`（两轮）、`<stress-data>\pipe_stress.json`。

## 收口结论（2026-09-18）

- **压测通过**：30k 视频 / 24 万 identity / 96 万 face 规模下，R5 大规模优化方向正确且经压测淬炼后全指标流畅（交互 <100ms 级，最重的对比选对 <0.7s）。
- **测试库保留**（用户约定：压测通过前不清理）——现压测已通过；`<stress-data>\`（1.18GB）暂保留供后续复测，确认无需后可整目录删除。
- 本轮共修复生产缺陷 1 个（jobs active 500）+ 性能优化 4 项（页缓存/共现收缩/向量化/寻道），无功能性回退（126 tests 全绿）。
