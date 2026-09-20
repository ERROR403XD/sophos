# 辅助脚本（按批次补充）

| 脚本 | 用途 | 批次 |
|---|---|---|
| `download_models.py` | 下载 ONNX 模型到 `data/models/`（det_10g / genderage / mobilefacenet / beauty_scut），附校验与镜像源支持 | M3 |
| `run_scan.py` | 命令行触发一次扫描+处理（免起 WebUI 的调试入口） | M2 |
| `backup_db.py` | SQLite 在线备份（`VACUUM INTO`） | M7 |
| `reset_video.py` | 清空某视频的 face/identity/score 并重置 pending（调试用） | M3 |

脚本均以项目根为工作目录调用：`python scripts/<name>.py`。
