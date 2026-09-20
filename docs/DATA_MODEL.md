# 数据模型（DATA_MODEL）

> 数据库：SQLite（`data/sophos.db`），ORM：SQLAlchemy 2.0。建表批次：M2。
> 命名：表名单数蛇形；主键 `id` INTEGER 自增；时间戳存 UTC ISO8601 字符串。

## 1. 实体关系

```
video 1 ──── n face                      (face.video_id)
face  n ──── 1 face_identity             (face.identity_id，聚类后归属)
face_identity 1 ─── 1 face_score         (每面容一行，滚动更新)
face_identity 1 ─── n user_rating        (同一面容可多次评分，取最新)
face_identity 1 ─── n pair_comparison    (两两对比：winner/loser 两列外键，ADR-013)
video 1 ──── 1 video_score               (视频-分数对照，滚动更新)
job / kv_setting：独立表
```

**评分单元是 `face_identity`（面容）**，不是单个检测样本；样本级信息仅用于聚类与选代表帧。

## 2. 表定义

### 2.1 video — 视频登记（只维护路径，不复制文件）

| 字段 | 类型 | 说明 |
|---|---|---|
| id | INTEGER PK | |
| path | TEXT UNIQUE NOT NULL | 容器/运行时视角的绝对路径，播放与处理都直接读它 |
| filename | TEXT NOT NULL | 冗余便于搜索 |
| dir_path | TEXT NOT NULL | 所属工作目录（扫描根） |
| size_bytes | INTEGER | |
| mtime | REAL | 增量扫描判据 |
| status | TEXT DEFAULT 'pending' | `pending / processing / done / failed / missing` |
| status_msg | TEXT | 失败原因等 |
| duration_sec / width / height | REAL / INT / INT | ffprobe 探测（M3，失败可空） |
| identity_count | INTEGER | 冗余：面容数 |
| vcodec / acodec | TEXT | R1(P4)：扫描期 ffprobe 探测的编码（h264/hevc/ac3…），播放分档依据；失败留空播放期再试 |
| created_at / updated_at | TEXT | |

索引：`path` 唯一；`(status)`；`(dir_path)`。

状态机：`pending → processing → done | failed`；扫描发现文件消失 → `missing`（不删行）；变更（mtime/size 变）→ 回 `pending` 重处理。

### 2.2 face — 人脸检测样本（单帧单脸）

| 字段 | 类型 | 说明 |
|---|---|---|
| id | INTEGER PK | |
| video_id | FK → video, CASCADE | |
| timestamp_sec | REAL | 出现时间点（播放器可跳转） |
| thumb_path | TEXT | 对齐后人脸缩略图（样本级，M3 可只存 identity 级） |
| bbox_x1 / y1 / x2 / y2 | REAL | 原帧坐标 |
| det_score | REAL | 检测置信度 |
| quality_score | REAL | R3 v3：清晰度(lap/80)×尺寸×曝光×对比度 0-1（R1 起作真实准入门：<min_quality(默认0.30) 丢弃） |
| female_prob | REAL | genderage 女性概率（R1 起仅为样本级记录值，准入裁决在 identity 级） |
| pose_yaw / pose_pitch | REAL | R1(P2)：5 点启发式姿态角（度，低头为正）；存量行为空=不参与偏好 |
| pose_class | TEXT | R1(P2)：frontal(\|yaw\|≤15) / near(≤45) / side(≤80) |
| occlusion_score | REAL | R1(P2)：遮挡提示 0-1（下半区低纹理启发式；≥0.6 视为疑似遮挡，不自动剔除） |
| embedding | BLOB | 512 维 float32（MobileFaceNet，对齐 112×112） |
| identity_id | FK → face_identity, NULLABLE | 聚类归属 |
| created_at | TEXT | |

索引：`(video_id)`；`(identity_id)`。

### 2.3 face_identity — 面容（评分与展示单元，"一视频多面容"）

| 字段 | 类型 | 说明 |
|---|---|---|
| id | INTEGER PK | |
| video_id | FK → video, CASCADE | MVP：面容隶属单一视频（跨视频合并为扩展项） |
| rep_face_id | FK → face | 代表帧（R1 起按 干净>姿态正面>质量 优选，非单纯质量最高） |
| mean_embedding | BLOB | 干净样本（occlusion_score<0.6）top-N 均值（训练特征用）；**全遮挡 identity 为空**=剔出训练集 |
| n_samples | INTEGER | 组内样本数（全量，含被修剪样本；≥2 才算稳定面容，可配） |
| female_prob_mean | REAL | R1(P1)：组内样本 female_prob 均值（identity 级女性裁决依据，≥0.60 保留） |
| occluded | INTEGER DEFAULT 0 | R1(P2)：遮挡标记（库内保留可评分，仅影响训练；manual 覆盖 auto） |
| occluded_source | TEXT | auto（处理期多数样本疑似遮挡）/ manual（评分页人工切换） |
| created_at / updated_at | TEXT | |

索引：`(video_id)`。

#### 2.3.1 轻量迁移（R1）

项目无 alembic：`app/db/migrations.py::run_migrations` 按 `PRAGMA table_info`
检查缺列 → `ALTER TABLE ADD COLUMN`（全部可空/带默认），`init_engine` 末尾调用，幂等。
存量行为空时按旧行为兜底（occluded 空=0、pose_class 空=不参与偏好）；
升级部署建议对已有视频全量重处理以补齐新字段（有评分的视频仍受既有保护逻辑约束）。

### 2.4 face_score — 面容分数（滚动更新，每 identity 一行）

| 字段 | 类型 | 说明 |
|---|---|---|
| id | INTEGER PK | |
| identity_id | FK → face_identity, UNIQUE | |
| base_score | REAL | 基础颜值分 0-100（SCUT 系模型） |
| base_model_version | TEXT | 产生 base_score 的模型版本 |
| personalized_score | REAL NULL | 个性化分 0-100 |
| pers_model_version | TEXT NULL | 个性化模型版本 |
| updated_at | TEXT | |

### 2.5 user_rating — 用户评分（两种方式）

| 字段 | 类型 | 说明 |
|---|---|---|
| id | INTEGER PK | |
| identity_id | FK → face_identity | |
| rating_type | TEXT | `score`（1-10）或 `thumbs`（好评/差评） |
| rating_value | TEXT | `score` 存 "1".."10"；`thumbs` 存 `up`/`down` |
| created_at | TEXT | |

约定：不禁止重复评分；训练与统计**取每 identity 最新一条**（两种 type 各自最新，合并规则见 M6）。

### 2.6 video_score — 视频-分数对照表（滚动更新，WebUI 主表）

| 字段 | 类型 | 说明 |
|---|---|---|
| video_id | FK → video, PK | |
| final_score | REAL | 当前生效综合分 0-100（个性化优先，回退基础） |
| base_final | REAL | 纯基础模型聚合分 |
| personalized_final | REAL NULL | 个性化聚合分 |
| identity_count | INTEGER | 参与聚合的面容数 |
| topk_detail | TEXT(JSON) | top-K identity 分数明细（调试/展示） |
| score_model_version | TEXT | 产生 final 的模型组合版本 |
| updated_at | TEXT | |

索引：`(final_score)`（列表排序）。

### 2.7 job — 后台任务（进度/断点续跑）

| 字段 | 类型 | 说明 |
|---|---|---|
| id | INTEGER PK | |
| type | TEXT | `scan / process / train` |
| status | TEXT | `queued / running / done / failed / cancelled` |
| done / total | INTEGER | 进度计数 |
| params | TEXT(JSON) | 任务参数 |
| result | TEXT(JSON) | 任务产出（scan 报告 / train 指标等） |
| error | TEXT | 失败信息 |
| created_at / started_at / finished_at | TEXT | |

状态机：`queued → running → done | failed | cancelled`。

### 2.8 kv_setting — 键值配置

| 字段 | 类型 | 说明 |
|---|---|---|
| key | TEXT PK | 如 `work_dirs`（JSON 数组）、`active_pers_model` |
| value | TEXT(JSON) | |
| updated_at | TEXT | |

工作目录列表存这里（WebUI 可改），环境变量 `SOPHOS_WORK_DIRS` 仅作首次初始化默认值。

### 2.9 pair_comparison — 两两对比结果（ADR-013）

| 字段 | 类型 | 说明 |
|---|---|---|
| id | INTEGER PK | |
| winner_identity_id | FK → face_identity | 用户认为更好的一张 |
| loser_identity_id | FK → face_identity | 另一张 |
| created_at | TEXT | |

索引：`(winner_identity_id)`、`(loser_identity_id)`。
约束：winner ≠ loser；允许同一对多次对比（训练时全部参与、等权）。

## 3. 保留与清理策略

| 数据 | 策略 |
|---|---|
| `data/frames/{video_id}/` | 抽帧临时目录，该视频处理完成后整目录删除 |
| video 置 `missing` | 保留行与分数历史；提供可选清理（M7） |
| face/identity 重复处理 | 重跑前按 video_id 级联删除，保证幂等 |
| user_rating | 全量保留（审计与再训练） |
| pair_comparison | 全量保留（偏好训练数据，ADR-013） |
| personalizer 版本 | 全部保留于 `data/models/personalizer/`，可回滚 |
