"""pytest 共享 fixture（M2）。

- _test_settings: 将全局 settings 指向 tmp 目录并初始化引擎（每测试独立 DB）
- db: 直接可用的 SQLAlchemy session（不走 HTTP）
- client: TestClient（进入时触发 lifespan：建表/清理遗留任务/启动 worker）

运行：在 backend/ 目录执行 `python -m pytest`。
"""
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def _test_settings(tmp_path):
    from app.config import settings
    from app.db.session import init_engine

    settings.data_dir = tmp_path / "data"
    settings.db_path = tmp_path / "data" / "test.db"
    settings.work_dirs = ""
    # 模型用真实目录（未下载时相关测试自行 skip）；数据目录保持 tmp 隔离
    settings.models_dir = Path(__file__).resolve().parent.parent / "data" / "models"
    # Windows 下让 pytest 路径与中文/非 ASCII 兼容
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    init_engine(settings)
    return settings


@pytest.fixture()
def db(_test_settings):
    from app.db import session as db_session_mod

    with db_session_mod.SessionLocal() as session:
        yield session


@pytest.fixture()
def client(_test_settings):
    from app.main import app

    with TestClient(app) as c:  # lifespan：init_engine + reset_stale + seed + worker
        yield c
