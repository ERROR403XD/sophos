# Sophos v1.0.4 实施计划

## R4：人物优先对比 + 视频内同人适度合并 + 在线播放 404 修复

> 前置版本：v1.0.3
> 目标版本：v1.0.4
>
> 本期核心目标：
>
> 1. 两两对比首先追求**不同的人之间比较**；
> 2. 如果无法得到足够明确的不同人物组合，再优先选择**不同视频**；
> 3. **不做跨视频同人合并**；
> 4. 同一个视频中，同一个人如果被拆成多个 identity，应进行**适度合并**；
> 5. 修复视频在线播放 `Request failed with status code 404`；
> 6. 本期不做年龄或儿童过滤。

---

# 0. 设计原则

R4 对“人物”和“视频”的关系重新明确：

```text
身份组织：
一个视频
    └── 若干 face_identity

允许：
Video A / Robin -> identity A1
Video B / Robin -> identity B3

不要求：
A1 与 B3 合并成全局 Robin
```

也就是说：

> **跨视频的同一个人可以继续作为两个 identity 存在。**

当前架构本身就是“面容按视频内聚类”，`face_identity` 隶属于单一 `video_id`，跨视频同人合并原本也只是扩展项。

R4 不再推进这个扩展项。

---

# 1. 为什么不做跨视频同人合并

建立全局人物库会带来大量额外复杂度：

* 新增 global person 数据模型；
* 增量扫描后重新关联人物；
* 人物误合并后的拆分问题；
* 历史评分迁移；
* 删除视频后的全局 identity 更新；
* 同一演员年龄、妆容、发型变化造成 embedding 漂移。

而这些都不是当前审美训练真正需要解决的问题。

即使：

```text
S01E01 Robin
S01E02 Robin
```

在数据库里是两个 identity，也没有本质问题。

真正应该避免的是：

```text
S01E01 Robin #1
S01E01 Robin #2
S01E01 Robin #3
```

也就是：

> **同一个视频内部因为聚类碎裂而重复出现同一个人。**

这才是本期需要处理的“同人合并”。

---

# 2. P1：同一视频内的同一个人适度合并

现有流水线已经具备：

```text
Chinese Whispers 聚类
→ merge pass
→ sample trim
→ identity
```

并且当前 merge pass 本来就用于防止同一人物被聚类拆碎。

R4 不重写整个聚类系统，而是对现有 merge pass 做一次针对性强化。

---

# 3. “适度合并”的含义

这里不能简单地把 embedding threshold 大幅降低。

否则容易发生：

```text
两个长得比较像的不同女性
        ↓
被错误合并成一个 identity
```

因此原则是：

> **宁可留下少量重复 identity，也不能大量把不同的人合成一个人。**

R4 的目标不是追求：

```text
一个视频中每个人绝对只有一个 identity
```

而是：

```text
显而易见属于同一个人的碎片尽量合并
```

---

# 4. 视频内 merge pass 改进

现有同视频 merge threshold 约为：

```text
cosine >= 0.78
```

用于解决 identity 碎裂。

R4 首先保留这个机制，不直接大幅放宽。

增加一个**双层 merge 判定**。

## 第一层：高置信直接合并

例如：

```text
similarity >= STRONG_THRESHOLD
```

建议：

```text
0.78
```

沿用现有行为。

这种情况直接认为：

```text
likely same person
```

执行 merge。

---

## 第二层：中等相似度谨慎合并

例如：

```text
MERGE_REVIEW_THRESHOLD <= similarity < STRONG_THRESHOLD
```

可先以：

```text
0.72 ~ 0.78
```

作为实验区间。

但不能只看 embedding。

还需要至少满足辅助条件，例如：

* 两组都具有多个稳定样本；
* mean embedding 均由非遮挡样本产生；
* 性别裁决一致；
* 两组不存在明显同时出现在同一帧的证据。

其中最后一项尤其重要。

---

# 5. “同帧共现”作为禁止合并证据

如果 identity A 和 identity B：

```text
在相同或非常接近的时间戳
同时出现在画面中
```

则它们几乎肯定是两个不同的人。

因此：

```text
same-frame co-occurrence
    =>
禁止 merge
```

这是比单纯调余弦阈值更可靠的防误合并机制。

可以利用现有 `face.timestamp_sec` 数据；该字段已经是样本级数据模型的一部分。

实现时可判断：

```text
abs(timestamp_A - timestamp_B) < tolerance
```

例如同一抽帧时间点或 ±0.1~0.5 秒范围内存在样本。

一旦发现共现：

```text
merge forbidden
```

即使 embedding 很高也不合并。

---

# 6. 聚类后的最终目标

一个视频处理完成后，理想结果：

```text
Episode 01

Ted      -> identity 1
Robin    -> identity 2
Lily     -> identity 3
Barney   -> 被女性过滤
Marshall -> 被女性过滤
```

而不是：

```text
Robin front
Robin near
Robin dark
Robin phone

=> 四个 identity
```

但如果：

```text
Robin
Lily
```

长得恰好比较接近，也绝不能为了减少 identity 数量而强行合并。

---

# 7. P2：重新设计 Pair Selector

当前 selector 的主要逻辑仍是：

```text
similar:
    base_score 差 <= 10 优先

random:
    任意随机
```

并排除已经比较过的 pair。

ADR-013 也明确把“base_score 接近”作为原来的主要选对原则。

R4 要改变这个优先级。

新的原则是：

```text
不同的人
    ↓
不同的视频
    ↓
分数接近
    ↓
随机
```

但这里的“不同的人”不是通过建立跨视频人物库实现的。

---

# 8. 如何判断“明显是不同的人”

使用现有：

```text
face_identity.mean_embedding
```

作为 pair selection 的**软判断信号**。

`mean_embedding` 已经存在于 face_identity，而且由干净样本组成。

计算：

```text
similarity =
cosine(A.mean_embedding, B.mean_embedding)
```

然后只用于 pair 排序。

例如：

```text
similarity 很低
    => clearly_different

similarity 中间
    => uncertain

similarity 很高
    => likely_same_or_similar
```

重要的是：

> **这个判断不产生人物关系，不写入数据库，也不触发跨视频 merge。**

它只是：

```text
pair sampling heuristic
```

---

# 9. 不把跨视频高相似 pair 永久禁止

这一点与上一版计划不同。

例如：

```text
Episode 01 Robin
Episode 02 Robin
```

embedding 很像。

R4 不会：

```text
建立 Robin global person
```

也不会永久禁止：

```text
Robin E01 vs Robin E02
```

只是：

> 当存在明显不同人物可以比较时，不优先把这种 pair 推给用户。

如果候选不足，它仍然可以成为 fallback。

因此人物相似度在 R4 中是：

```text
ranking signal
```

而不是：

```text
hard identity constraint
```

---

# 10. 新 Pair 优先级

## Tier A —— 明确不同人物

首先寻找：

```text
embedding similarity 较低
```

即明显是不同人的两个 identity。

在这些 pair 内部，再优先：

```text
score_diff 较小
```

因为对于审美模型来说：

```text
70 vs 72
```

通常比：

```text
20 vs 95
```

提供的信息更细。

---

# 11. Tier B —— 不同视频

如果找不到足够的明确不同人物组合：

```text
优先 video_id 不同
```

即：

```text
A.video_id != B.video_id
```

这时不再要求：

```text
embedding 必须证明两人不同
```

因为跨视频本身已经能够提供一定的数据多样性。

因此即使：

```text
Robin E01
vs
Robin E02
```

偶尔进入比较，也可以接受。

---

# 12. Tier C —— 其他未比较 pair

最后才从：

```text
未比较过的剩余组合
```

中选取。

例如：

* 同视频；
* embedding 关系不明确；
* score 差较大。

仍然比直接返回 NO_PAIR 更有价值。

---

# 13. Pair 排序可以理解为

核心排序逻辑：

```text
1. clearly different person
2. different video
3. lower score difference
4. random jitter
```

不是：

```text
different person
AND
different video
```

而是两个逐级 fallback 条件。

这是本期最关键的设计变化。

---

# 14. 同视频不同 identity 的特殊处理

因为本期会加强视频内 merge：

```text
同视频 + identity 不同
```

通常已经可以较可靠地理解为：

```text
不同的人
```

尤其当：

```text
存在同帧共现
```

时，更可以视为高置信不同人物。

因此 pair selector 可以给予：

```text
same_video + co-occurrence
```

非常高的 `different-person confidence`。

例如：

```text
Robin 和 Lily 同时坐在酒吧
```

这是比：

```text
两个 embedding cosine = 0.55
```

更直接的“不同人物”证据。

---

# 15. 推荐的 different-person confidence

可以内部计算：

```text
different_person_confidence
```

但不落库。

例如：

```text
同帧共现
    -> 1.0

embedding 明显不同
    -> high

embedding 中等
    -> unknown

embedding 很接近
    -> low
```

然后 Pair Selector：

```text
先按 different_person_confidence 排
```

再考虑：

```text
video difference
score difference
```

---

# 16. Pair API

现有：

```http
GET /api/faces/pair?strategy=similar
GET /api/faces/pair?strategy=random
```

继续保留。

新增：

```http
GET /api/faces/pair?strategy=diverse
```

并将：

```text
diverse
```

设为前端默认策略。

---

# 17. diverse 的明确语义

`diverse` 不再解释为简单的：

```text
跨视频
```

而是：

> **人物优先的多样化对比。**

算法：

```text
候选 pair
    ↓
排除已经比较的组合
    ↓
估计 different-person confidence
    ↓
优先明显不同人物
    ↓
若不足，优先不同视频
    ↓
同层中优先 score 较接近
    ↓
加入少量随机性
```

---

# 18. similar / random 的行为

## similar

仍然保留：

```text
base_score 接近优先
```

但在相近候选中，可以轻度偏向不同人物。

不要改变它原本的主要语义。

---

## random

继续是随机抽样。

不做跨视频人物排除，也不做 global-person 判断。

仅：

```text
排除已经比较过的完全相同 pair
```

即可。

---

# 19. 不修改 pair_comparison 数据模型

现有表：

```text
winner_identity_id
loser_identity_id
created_at
```

已经足够。

不增加：

```text
person_id
global_identity_id
same_person
video_pair
```

等字段。

新的抽样策略仅影响：

```text
下一组给用户看什么
```

不会改变训练数据格式。

---

# 20. P3：修复视频在线播放 404

现有播放设计已经支持：

```text
direct
remux
transcode
```

三档。

因此：

```text
Request failed with status code 404
```

不能简单归因于 MKV / HEVC 不支持。

本期必须定位 404 实际发生在哪一层：

```text
前端 URL
    ↓
FastAPI route
    ↓
video DB row
    ↓
source file
    ↓
stream mode
    ↓
ffmpeg
```

---

# 21. 区分不同类型的 404

## VIDEO_NOT_FOUND

数据库中不存在 ID：

```json
{
  "code": "VIDEO_NOT_FOUND",
  "message": "video 123 not found"
}
```

---

## SOURCE_NOT_FOUND

数据库有视频，但：

```text
video.path
```

对应文件已不存在：

```json
{
  "code": "SOURCE_NOT_FOUND",
  "message": "source video file no longer exists"
}
```

---

## ROUTE_NOT_FOUND

请求没有进入：

```text
/api/videos/{id}/stream
```

例如路径：

```text
/videos/123/stream
```

或者 SPA fallback 抢占路由。

必须修正前端或 FastAPI route 注册。

---

# 22. Stream URL 后端统一生成

`GET /api/videos`

和：

```text
GET /api/videos/{id}
```

增加：

```json
{
  "stream_url": "/api/videos/123/stream"
}
```

前端不再自行拼：

```js
`/api/videos/${id}/stream`
```

后端成为 stream endpoint 的唯一真源。

---

# 23. 视频本体不要通过 axios 拉取

前端应使用：

```html
<video
  controls
  :src="video.stream_url"
/>
```

而不是：

```js
axios.get(stream_url)
```

媒体请求交给浏览器原生 `<video>`。

这样才能正常处理：

* HTTP Range；
* StreamingResponse；
* fMP4；
* seek；
* 用户停止播放后的连接断开。

---

# 24. 检查 StaticFiles 与 API 路由

现有前端构建产物已经由 FastAPI 静态托管。M5 交接记录也明确存在这一结构。

必须保证：

```text
/api/*
```

优先于：

```text
StaticFiles(html=True)
```

注册。

增加回归测试：

```text
/api/videos/{id}/stream
```

绝不能被 SPA fallback 当作不存在的静态资源处理。

---

# 25. ffmpeg 错误不能伪装成 404

对于：

```text
remux
transcode
```

失败时记录：

```text
video_id
source_path
stream_mode
ffmpeg command
return_code
stderr tail
```

并返回合理错误：

```text
STREAM_FAILED
TRANSCODE_FAILED
FFMPEG_NOT_FOUND
```

而不是统一变成：

```text
404
```

---

# 26. 实施任务

## T1 — 建立视频内 identity 合并基线

先用真实视频统计：

```text
每视频 identity 数量
人工重复人物数量
merge 前后数量
```

挑选 2-3 集作为固定回归集。

---

## T2 — 强化视频内 merge

修改：

```text
clustering.py
pipeline.py
```

实现：

```text
高相似 -> 直接 merge
中高相似 -> 谨慎 merge
同帧共现 -> 禁止 merge
```

阈值必须配置化。

---

## T3 — 增加不同人物判断

修改：

```text
services/pairs.py
```

加入：

```text
embedding difference
same-frame co-occurrence
```

计算临时：

```text
different_person_confidence
```

不写数据库。

---

## T4 — diverse pair strategy

增加：

```text
strategy=diverse
```

排序：

```text
different person
    >
different video
    >
score close
    >
random
```

设为前端默认。

---

## T5 — Pair 单元测试

至少覆盖：

```text
明显不同人物优先
同帧人物优先
人物证据不足时跨视频优先
跨视频同演员仍允许 fallback
score 接近仅为次级条件
已比较 pair 排除
候选耗尽 -> NO_PAIR
```

---

## T6 — 定位播放 404

针对实际失败视频记录：

```text
video_id
request URL
HTTP status
response body
video.path
file exists
vcodec/acodec
stream_mode
ffmpeg stderr
```

先找根因，再改代码。

---

## T7 — Stream URL 契约

API 增加：

```text
stream_url
```

Pair/Video 前端统一使用后端提供的 URL。

---

## T8 — 播放回归测试

至少覆盖：

```text
MP4 H264/AAC
    -> direct

MKV H264/AAC
    -> remux

MKV HEVC/AC3
    -> transcode

missing DB id
    -> VIDEO_NOT_FOUND

source file deleted
    -> SOURCE_NOT_FOUND

API stream route
    -> 不被 StaticFiles 捕获
```

---

# 27. 真实片源验收

现有交接文档记录的测试素材为：

```text
测试剧集 S01
1080p x265 / AC3 MKV
```

非常适合作为本期回归素材。

---

## 人物合并验收

人工检查每集：

```text
Robin 是否大量重复
Lily 是否大量重复
Victoria 等角色是否被拆分
```

要求：

> 明显的同人碎片显著减少，但不能出现 Robin 与 Lily 被合成一个 identity 之类的误合并。

---

## Pair 验收

连续抽取 50-100 个 diverse pair。

统计：

```text
明确不同人物
不同视频 fallback
明显同一人物
```

期望：

> 候选丰富时，以不同人物比较为绝对主体。

但不要求：

```text
Robin E01 vs Robin E02
```

这种组合彻底消失。

少量出现可以接受，因为系统本期明确**不维护跨视频人物身份**。

---

# 28. 本期明确不做

### 不做跨视频人物合并

不新增：

```text
global_person
person_id
global_identity
```

也不尝试建立“演员库”。

---

### 不永久排除跨视频同演员 pair

高 embedding similarity 只是降低 pair 优先级，不是 hard ban。

---

### 不做年龄过滤

不增加：

```text
age
age_mean
age_threshold
```

不剔除儿童。

---

### 不修改个性化训练算法

仍然沿用当前：

```text
pairwise logistic / RankNet
```

新的工作只是改善 pair 数据采样质量。

---

### 不做转码缓存

只解决现有：

```text
direct / remux / transcode
```

可靠工作。

---

# 29. R4 出口条件

* [ ] 不建立跨视频人物合并；
* [ ] 视频内同人碎裂情况显著减少；
* [ ] 同帧出现的两个 identity 永不被 merge；
* [ ] merge 不明显增加不同人物误合并；
* [ ] 新增 `strategy=diverse`；
* [ ] diverse 第一优先级为“不同人物”；
* [ ] 人物条件不足时优先“不同视频”；
* [ ] score_diff 降为次级选择条件；
* [ ] 跨视频同演员允许作为 fallback；
* [ ] pair_comparison 数据结构不变；
* [ ] 当前视频在线播放 404 根因明确并修复；
* [ ] direct / remux / transcode 均通过回归；
* [ ] x265 + AC3 MKV 实机播放成功；
* [ ] pytest 全绿；
* [ ] frontend build 成功；
* [ ] 回写 API_DESIGN / ARCHITECTURE / DECISIONS / DEVELOPMENT_PLAN / HANDOFF / README；
* [ ] tag `v1.0.4`。

---

# 30. 最终数据流

R4 完成后的思路应当是：

```text
视频处理阶段：

一段视频
    ↓
检测所有面容
    ↓
视频内聚类
    ↓
高置信同人 merge
    ↓
中等置信 + 无同帧冲突 -> 谨慎 merge
    ↓
形成该视频自己的 identity 集合


Pair 阶段：

所有 identity
    ↓
优先找明显不同的人
    ↓
没有合适组合时优先不同视频
    ↓
同层内优先分数接近
    ↓
仍不足则普通未比较 pair
```

核心原则可以浓缩为一句：

> **人物重复问题在视频内部解决；跨视频不建立人物身份；评分对比优先给用户看两个不同的人，人物多样性不足时再靠不同视频补充。**
