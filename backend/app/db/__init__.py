"""数据库层（M2 实现）。

- models.py：SQLAlchemy 2.0 声明式模型，九张表一次建齐
  （video / face / face_identity / face_score / user_rating / pair_comparison /
   video_score / job / kv_setting），字段定义见 docs/DATA_MODEL.md。
- session.py：engine（SQLite，busy timeout=30s）与 get_db 依赖；init_engine 幂等。
- 说明：不启用 WAL（项目目录位于 SMB 网络盘，WAL 依赖共享内存不可靠）；
  不启用 PRAGMA foreign_keys（级联删除由应用层负责，见 ARCHITECTURE.md §3.1）。
"""
