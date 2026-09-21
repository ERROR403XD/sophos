"""应用配置（M2 实现）。

环境变量统一前缀 SOPHOS_，可用 backend/.env 文件加载（env_file 相对运行目录）。
取值依据见 docs/DATA_MODEL.md 与 docs/ARCHITECTURE.md。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SOPHOS_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # ---- 基础路径 ----
    data_dir: Path = Path("./data")          # db/thumbs/models/logs 根目录
    db_path: Path | None = None              # 缺省 -> {data_dir}/sophos.db

    # ---- 工作目录初始值：JSON 数组字符串或 os.pathsep(Windows 为 ;) 分隔 ----
    # 仅用于 kv_setting.work_dirs 首次播种；之后以 WebUI/API 配置为准
    work_dirs: str = ""

    # ---- 视频处理（M3 消费；R1/R2 调整） ----
    sample_interval_sec: float = 2.0         # 抽帧间隔
    min_face_size: int = 80                  # 最小人脸边长 px（R2：64→80，压超小路人脸）
    female_threshold: float = 0.5            # 样本级性别记录口径（不再做准入过滤，ADR-014）
    cluster_threshold: float = 0.68          # 视频内聚类余弦阈值
    det_threshold: float = 0.5               # 人脸检测置信度阈值

    # ---- R1(P1/P2/P3)：面容准入与聚类（ADR-014） ----
    female_identity_threshold: float = 0.60  # identity 级女性裁决阈值（单样本 identity 用更严的 0.65，见 pipeline）
    min_quality: float = 0.30                # 样本质量门（R3 v3：清晰度×尺寸×曝光×对比度四因子；宁缺毋滥）
    max_yaw_deg: float = 80.0                # 姿态剔除阈值：只剔极端角度（probe_pose.py 定标后可调）
    max_pitch_deg: float = 40.0
    merge_threshold: float = 0.78            # 聚类后合并 pass 的组间余弦阈值（防碎片化；R4 第一层"高置信直接合并"）
    merge_review_threshold: float = 0.72     # R4 第二层"中等相似谨慎合并"下限：[review, merge_threshold) 区间
                                             # 须同时满足 双方≥2 干净样本、干净均值也过线、无同帧共现（宁可碎片不过合）
    merge_cooccur_tolerance_sec: float = 0.5 # R4 同帧共现判定时间容差：两组样本时间差 ≤ 此值视为同框 → 禁止合并
    merge_singleton_threshold: float = 0.74  # R6 第三层"单样本同人合并"下限：双方均单样本、
                                             # 余弦 ∈ [此值, merge_threshold)、无同帧共现 → 合并
                                             # （单样本无投票修正，证据弱一档须比谨慎层 0.72 更严；0=关闭）
    max_samples_per_identity: int = 16       # mean_embedding 参与样本上限（修剪近重复帧）
    allow_side_rep: bool = False             # R2：rep 为侧脸（yaw>45°）的 identity 不入评分库（用户验收反馈）

    # ---- R4(P2)：diverse pair 选择（仅排序信号，不落库、不产生人物关系） ----
    pair_sim_clearly_different: float = 0.40  # mean_embedding 余弦低于此值 → 明显不同人物（高置信）
    pair_sim_likely_same: float = 0.60        # 高于此值 → 很像/可能同一人（跨视频同人，降级不禁止）
    # ---- R5 大规模（ADR-024）：pair 选对在超大面容库（数十万 identity）下的抽样规模 ----
    pair_sample_size: int = 500               # 候选抽样上限；库不超此值时保持全量精确语义

    # ---- R5：处理分批 + 面容上限（可在 WebUI 设置卡片运行时覆盖，见 services/runtime_settings） ----
    process_batch_size: int = 4               # process 任务每批视频数（链式分批，批间可插入其他任务）
    max_faces_per_person: int = 5             # 同一视频同一人最多保留面容数（0 = 不限制）
    cap_merge_floor: float = 0.50             # 上限 pass 强制合并的余弦下限：低于此值视为不同人，永不强并

    # ---- R2：CLIP 第二意见（性别一致同意 + 侧脸剔除；模型由 scripts/export_clip_gender.py 生成） ----
    clip_enabled: bool = True                # 模型文件缺失时自动降级为仅 genderage（警告一次）
    clip_gender_min: float = 0.50            # identity 级 CLIP 女性均值下限（与 genderage 均值须同时过）
    clip_profile_max: float = 0.95           # 样本级 CLIP 侧脸概率上限。注意：紧脸裁剪上该 prompt 对
                                             # 区分度差（正面也常判 profile，实测），仅拦近乎背面
                                             # 的剪影；性别漏网由一致同意门控兜住（E01 实测分离完美）

    # ---- 模型文件（models_dir 缺省 = data_dir/models；测试可指向真实模型目录）----
    models_dir: Path | None = None
    model_det: str = "det_500m.onnx"         # SCRFD；可选 det_10g.onnx（更准）
    model_emb: str = "w600k_mbf.onnx"        # MobileFaceNet 512d
    model_gender: str = "genderage.onnx"     # gender 0=女 1=男

    # ---- R6：访问口令（空 = 不启用鉴权；部署在不可信局域网时务必设置）----
    access_password: str = ""

    # ---- R5.2：WAL 日志模式（仅当 data_dir 在本地 SSD 时启用！）----
    # SMB 网络盘上 WAL 依赖的共享内存不可靠（M2 实测决策），保持默认关闭；
    # DB/缩略图迁到本地 SSD 后开启可显著改善读写并发（读不再阻塞写）。
    db_wal: bool = False

    # ---- ffmpeg（空 = 自动定位：PATH > tools/ffmpeg*/bin）----
    ffmpeg_exe: str = ""

    # ---- R1(P4)：播放三档（ADR-015） ----
    transcode_enabled: bool = True           # false 时 remux/transcode 档退回 415
    transcode_preset: str = "veryfast"       # x264 preset（veryfast/balanced 取舍）
    transcode_max_concurrency: int = 1       # ffmpeg 管道并发上限（防 CPU 占满影响处理任务）
    transcode_max_height: int = 1080         # R9：转码输出分辨率封顶（0=保持源分辨率）。
                                             # 4K 源实时转码单路即打满 CPU → 整机/Web 服务失去响应

    def final_models_dir(self) -> Path:
        return self.models_dir if self.models_dir is not None else self.data_dir / "models"

    # ---- 聚合/训练 ----
    topk: int = 3                            # 视频综合分取 top-K 面容
    auto_train_every: int = 30               # 自动训练触发的新增评分条数（R6.3 起为运行时设置的默认值）

    # ---- R6.3：自动化（默认关闭；WebUI 设置卡片可改，见 services/automation）----
    auto_scan_enabled: bool = False          # 每天定时自动扫描工作目录
    auto_scan_time: str = "03:00"            # 自动扫描每日触发时刻（HH:mm 本地时间，错过当天补跑一次）
    auto_process_enabled: bool = False       # 自动处理面容（发现 pending 视频即起处理链，与自动扫描联动）

    # ---- R6.3：外部视频分析（services/analyzer，产物 data/analyze/{token}/）----
    analyze_keep: int = 20                   # 保留最近 N 个分析结果，超出自动清理

    # ---- 任务 ----
    job_wait_timeout_sec: float = 3600.0     # 单任务兜底超时（暂未启用，M3 复核）

    def final_db_path(self) -> Path:
        return self.db_path if self.db_path is not None else self.data_dir / "sophos.db"

    def parse_work_dirs(self) -> list[str]:
        raw = self.work_dirs.strip()
        if not raw:
            return []
        if raw.startswith("["):
            try:
                return [str(p) for p in json.loads(raw)]
            except json.JSONDecodeError:
                pass
        return [p for p in (s.strip() for s in raw.split(os.pathsep)) if p]


settings = Settings()
