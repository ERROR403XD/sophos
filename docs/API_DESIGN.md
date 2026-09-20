# API 设计（API_DESIGN）

> 后端统一前缀 `/api`；交互式文档 `http://127.0.0.1:8000/docs`（FastAPI 自动生成）。
> "实现批次"标注各端点落地时间；本文为**设计稿**，实现时如需偏离，须回写本文并在 HANDOFF.md 记录。

## 1. 通用约定

- 响应均为 JSON；时间 ISO8601 UTC。
- 分页：`?page=1&page_size=50`（page_size 上限 200），返回：
  ```json
  { "items": [...], "total": 123, "page": 1, "page_size": 50 }
  ```
- 错误体：`{ "code": "NOT_FOUND", "message": "video 12 not found" }`，HTTP 状态码语义化（400/404/409/500）。
- 排序参数 `sort=final_score&order=desc`；搜索 `q=关键词`（对 filename）。

## 2. 端点总览

| 方法 | 路径 | 说明 | 批次 |
|---|---|---|---|
| GET | `/api/health` | 健康检查 + 版本 + db/worker 状态 | M2 |
| GET / POST / DELETE | `/api/workdirs` | 工作目录列表 / 新增 / 删除（存 kv_setting） | M2 |
| POST | `/api/scan/start` | 触发增量扫描，返回 job | M2 |
| POST | `/api/process/start` | 触发 pending 视频的人脸流水线，返回 job（R5 起按批切片只提交第一批，链式续批 ADR-022） | M3(R5) |
| GET | `/api/jobs`、`/api/jobs/{id}` | 任务列表（`?active=1` 含 paused）/ 详情与进度 | M2 |
| POST | `/api/jobs/{id}/pause`、`/cancel`、`/resume` | 任务暂停/取消/恢复（R5，ADR-022；409 JOB_STATE） | R5 |
| GET / PUT | `/api/settings` | 运行时设置读取 / 覆盖（process_batch_size、max_faces_per_person；R5，ADR-022） | R5 |
| GET | `/api/videos` | **视频-分数对照表**（分页/排序/搜索/筛选） | M2(M4 完善) |
| GET | `/api/videos/{id}` | 视频详情（含分数与面容摘要） | M2 |
| GET | `/api/videos/{id}/stream` | 流式播放（R1 三档自适应；R4 错误分型；R5 `?ss=` 服务端 seek，ADR-021） | M2 |
| GET | `/api/videos/{id}/hls/{token}/index.m3u8` | HLS 会话 playlist（R8/ADR-029，移动端播放；`?ss=`/`fallback` 同 stream） | R8 |
| GET | `/api/videos/{id}/hls/{token}/{seg}.ts` | HLS 切片下发（FileResponse，Range 206） | R8 |
| GET | `/api/faces` | 面容库列表（`?video_id=`、`?unrated=1` 优先未评） | M3 |
| GET | `/api/faces/{id}` | 面容详情（代表图/分数/样本数/时间点） | M3 |
| GET | `/api/thumbs/{identity_id}.jpg` | 面容缩略图（R5：分片存储 + immutable 缓存；`?v=` 为内容版本） | M3(R5) |
| GET | `/api/faces/pair` | 抽一对面容供对比（`?strategy=diverse\|similar\|random`，R4 默认 diverse，ADR-019） | M5(R4) |
| POST | `/api/faces/pair/compare` | 记录对比结果 `{winner_id, loser_id}` | M5 |
| POST | `/api/faces/{id}/rating` | 提交评分 `{type, value}` | M5 |
| GET | `/api/ratings/stats` | 评分统计（总数/分布/两种方式各自数量） | M5 |
| POST | `/api/train/start` | 触发接续训练，返回 job | M6 |
| GET | `/api/train/status`、`/api/train/versions` | 当前训练状态 / 历史版本与指标 | M6 |
| POST | `/api/train/activate/{version}` | 启用某个性化模型版本（启用后触发重算） | M6 |

## 3. 关键端点定义

### 3.1 工作目录

```http
GET /api/workdirs
→ { "workdirs": ["E:\\videos", "F:\\clips"] }

POST /api/workdirs
{ "path": "E:\\videos" }
→ 201 { "path": "E:\\videos" }          # 存在性校验：目录必须存在

DELETE /api/workdirs?path=E:\videos
→ 200 { "ok": true, "workdirs": [...] }
```

### 3.2 扫描与处理任务

```http
POST /api/scan/start
→ 202 { "job": { "id": 7, "type": "scan", "status": "queued" } }

GET /api/jobs/{id}
→ { "id": 7, "type": "scan", "status": "done", "done": 1, "total": 1,
    "result": {"added": 12, "changed": 0, "missing": 0, "unchanged": 118},
    "error": null }
```

`process`（M3）为完整流水线：抽帧 → 检测/性别/特征 → 聚类 → 缩略图 →（M4）打分 → 聚合。
R5 起按 `process_batch_size` 分批：start 只提交第一批，批完成且未被暂停/取消时链式提交
下一批 pending（批间其他任务可插队，ADR-022）；无 pending 视频 → 409 `NO_PENDING_VIDEOS`。

任务控制（R5，ADR-022）：

```http
POST /api/jobs/{id}/pause     # queued 直接 paused；running 等检查点 → {"id":7,"action":"paused"|"requested"}
POST /api/jobs/{id}/cancel    # queued/paused 直接 cancelled；running 等检查点
POST /api/jobs/{id}/resume    # paused → queued 重新入队（幂等续跑）；非 paused → 409 JOB_STATE
```

运行时设置（R5，ADR-022；默认值取自 .env，覆盖存 kv_setting，立即对后续任务生效）：

```http
GET /api/settings
→ { "settings": { "process_batch_size": 4, "max_faces_per_person": 5 } }

PUT /api/settings   { "process_batch_size": 2, "max_faces_per_person": 0 }
→ 200 { "settings": {...} }     # 非法键/类型/范围 → 400 INVALID_SETTING
```

### 3.3 视频-分数对照表

```http
GET /api/videos?sort=final_score&order=desc&page=1&page_size=50&status=done
GET /api/videos?library=E:%5Cvideos          # R1(P5)：按库（dir_path 精确匹配）筛选，可与 q/status 叠加
→ {
  "items": [
    {
      "id": 3,
      "path": "E:\\videos\\a.mp4",
      "dir_path": "E:\\videos",
      "filename": "a.mp4",
      "duration_sec": 305.2,
      "identity_count": 4,
      "stream_mode": "direct",               # R1(P4)：direct|remux|transcode|unsupported
      "stream_url": "/api/videos/3/stream",  # R4(ADR-020)：后端统一下发，前端不自行拼接
      "final_score": 86.5,
      "base_final": 83.1,
      "personalized_final": 86.5,
      "status": "done",
      "updated_at": "2026-09-20T08:00:00Z"
    }
  ],
  "total": 120, "page": 1, "page_size": 50
}

GET /api/videos/libraries                     # R1(P5)：库下拉选项 = DB 内 distinct dir_path
→ { "libraries": ["E:\\videos", "F:\\movies"] }
```

### 3.4 在线播放（R1 三档，ADR-015）

```http
GET /api/videos/3/stream
→ 响应头 X-Sophos-Stream-Mode: direct | remux | transcode
```

| 档位 | 条件 | 实现 |
|---|---|---|
| direct | mp4/m4v/webm/mov 且 h264/vp9 + aac/opus（探测失败时乐观直出） | FileResponse，真 Range 206 可拖动 |
| remux | 容器不兼容（mkv/avi/ts…）但 h264 + aac/无音轨 | `ffmpeg -c copy` → fMP4 流（200，无 Range） |
| transcode | 其余编码（hevc/mpeg4/…） | libx264 veryfast → fMP4 流（首次加载稍慢） |

- 不支持的格式/探测失败且不在白名单 → 415 + JSON 说明（注明探测失败或编码不支持）。
- remux/transcode 为 fMP4 分段流，浏览器自行 seek；转码受 `SOPHOS_TRANSCODE_MAX_CONCURRENCY` 限流，`SOPHOS_TRANSCODE_ENABLED=false` 时回退 415。

**R4（ADR-020）补充**：
- **stream_url 契约**：视频列表/详情均下发 `stream_url`（§3.3），后端是 stream endpoint 的唯一真源；前端用原生 `<video :src>` 播放，禁止用 axios 拉取/探测媒体流（axios baseURL 二次前缀曾把探测打成 `/api/api/...` → 404，即"在线播放 Request failed with status code 404"根因）。
- **404 分型**：库中无此 id → `VIDEO_NOT_FOUND`；源文件已不存在 → `SOURCE_NOT_FOUND`。
- **起播预检**：remux/transcode 在返回 200 前先等首个输出块，起播即失败 → 500 `STREAM_FAILED`（remux）/ `TRANSCODE_FAILED`（transcode），message 带 stderr 尾部；流中途失败记录完整上下文日志（video_id/source/mode/cmd/return_code/stderr tail）。ffmpeg 可执行文件缺失 → 500 `FFMPEG_NOT_FOUND`。
- `/api/*` 路由注册先于 `StaticFiles(html=True)` SPA fallback，stream 端点绝不被静态托管捕获（有回归测试）。

### 3.4a HLS 会话播放（R8，ADR-029：移动端播放修复）

移动端（iOS Safari 要求起播 Range 206，fMP4 管道流 200+chunked 直接失败）非 direct 档
改走 HLS。切片为 MPEG-TS；转码输出强制 8bit 4:2:0 + 立体声（浏览器兼容性定稿见 ADR-029）。

```http
GET  /api/videos/{id}/hls/{token}/index.m3u8?ss=<秒>&fallback=0|1
→ 200 application/vnd.apple.mpegurl（event 型 playlist，起流时阻塞至首个切片就绪，
  最长 20s；Cache-Control: no-store；X-Sophos-Stream-Mode: remux|transcode）
GET  /api/videos/{id}/hls/{token}/seg_00001.ts
→ 200/206 video/mp2t（FileResponse，天然支持 Range——iOS 起播探测 206 的关键）
```

- `token`：客户端每次起流/热切换随机生成（`[A-Za-z0-9_-]{1,64}`）；新 token = 新会话，
  同视频旧会话被挤掉（快速 seek 不堆积 ffmpeg）。`ss`/`fallback` 语义与 §3.4 相同。
- 会话回收：playlist 轮询超时（120s 无拉取 → kill ffmpeg）→ 异常退出补写 ENDLIST →
  目录 TTL 15min 删除；启动时清理遗留会话目录；全局会话上限 4。
- 列表/详情/面容条目下发 `hls_url: "/api/videos/{id}/hls"`（前端拼 `{token}/index.m3u8`）。
- 移动端 direct 档仍走 §3.4 渐进直出（FileResponse 有真 Range）；桌面播放行为不变。


### 3.5 面容库与评分

```http
GET /api/faces?unrated=1&page=1&page_size=20
→ { "items": [
     { "id": 55, "video_id": 3, "rep_thumb": "/api/thumbs/55.jpg",
       "base_score": 78.4, "personalized_score": 81.0,
       "n_samples": 6, "timestamp_sec": 42.5,
       "female_prob_mean": 0.91,             # R1(P1)：identity 级性别置信度
       "occluded": false,                    # R1(P2)：遮挡标记（auto/manual）
       "my_rating": null }
   ], "total": 310, ... }

POST /api/faces/55/rating
{ "type": "score", "value": 8 }        # 或 { "type": "thumbs", "value": "up" }
→ 200 { "ok": true }

POST /api/faces/55/occlusion          # R1(P2)：人工遮挡标记（manual 覆盖 auto）
{ "occluded": true }
→ 200 { "ok": true, "occluded": true }

GET /api/ratings/stats
→ { "score_count": 120, "thumbs_count": 45, "pair_count": 210,
    "score_histogram": {"1":0,"2":1,...,"10":9},
    "unique_faces_rated": 103 }
```

### 3.6 两两对比（pairwise，ADR-013；R4 diverse，ADR-019）

```http
GET /api/faces/pair?strategy=diverse      # 默认（R4 起）
→ { "a": {"id": 55, "rep_thumb": "/api/thumbs/55.jpg", "base_score": 78.4},
    "b": {"id": 82, "rep_thumb": "/api/thumbs/82.jpg", "base_score": 76.9} }
    # strategy=diverse（R4 默认）：人物优先多样化对比，排序
    #   明显不同人物 > 不同视频 > 分差小 > 随机抖动；
    #   "不同人物"用 mean_embedding 余弦 + 同帧共现软估计，只影响抽样排序，
    #   不落库、不产生人物关系、不禁止跨视频同演员（候选不足时自然 fallback）
    # strategy=similar: 优先 base_score 接近（差 ≤10）且未对比过的对（轻度人物偏好）
    # strategy=random: 纯随机（仅排除已对比对）

POST /api/faces/pair/compare
{ "winner_id": 55, "loser_id": 82 }
→ 200 { "ok": true }
```

R4 明确：`pair_comparison` 数据结构不变（winner/loser/created_at），diverse 只是"下一组给用户看什么"的抽样策略。

### 3.7 训练（M6）

```http
POST /api/train/start
→ 202 { "job": { "id": 9, "type": "train", "status": "queued" } }

GET /api/train/versions
→ { "versions": [
     { "version": "v3", "n_samples": 210, "mae": 6.2, "r2": 0.71,
       "active": true, "created_at": "..." }
   ] }
```

## 4. 错误码表

| code | HTTP | 场景 |
|---|---|---|
| DIR_NOT_FOUND | 400 | 工作目录不存在 |
| INVALID_RATING | 400 | 评分类型/取值非法 |
| INVALID_STRATEGY | 400 | pair 策略非 diverse\|similar\|random |
| NOT_FOUND | 404 | 通用资源不存在 |
| NO_PAIR | 404 | 无可对比面容（全部对比完成或库为空） |
| VIDEO_NOT_FOUND | 404 | 视频 id 不存在于库（R4 分型） |
| SOURCE_NOT_FOUND | 404 | 视频在库但源文件已不存在（R4 分型） |
| JOB_RUNNING | 409 | 同类任务已在运行 |
| FORMAT_UNSUPPORTED | 415 | 视频格式不支持在线播放（探测失败/转码关闭） |
| NOT_ENOUGH_SAMPLES | 409 | 训练样本不足（默认 <20） |
| STREAM_FAILED | 500 | remux 起播失败（R4，含 stderr 尾部） |
| TRANSCODE_FAILED | 500 | transcode 起播失败（R4，含 stderr 尾部） |
| FFMPEG_NOT_FOUND | 500 | ffmpeg 可执行文件缺失（R4） |
| INTERNAL | 500 | 未分类错误 |
