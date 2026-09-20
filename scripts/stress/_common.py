"""压测工装公共模块（STRESS_LOG）：目录大小守卫 + settings 引导。

测试库约定（用户 2026-09-18 指定）：
- 测试目录 D:\\SophosStress\\（clips 片源 / data-* 独立数据目录 / virtual 虚拟路径）；
- **总占用上限 1TB**，ensure_cap() 在每个生成/播种步骤前后校验，超限立即中止；
- 压测通过并取得成效前**保留测试库**（不自动清理）。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

CAP_GB = 1000.0          # 用户指定的测试目录总大小上限
WARN_RATIO = 0.9         # 超过 90% 即拒绝继续写入
STRESS_ROOT = Path("D:/SophosStress")


def dir_size_bytes(path: Path) -> int:
    total = 0
    for dirpath, _dirnames, filenames in os.walk(path):
        for name in filenames:
            try:
                total += (Path(dirpath) / name).stat().st_size
            except OSError:
                pass
    return total


def ensure_cap(*paths: Path, cap_gb: float = CAP_GB) -> int:
    """校验各测试目录总占用不超上限；超限抛 RuntimeError（中止写入）。"""
    total = sum(dir_size_bytes(p) for p in paths if Path(p).exists())
    limit = cap_gb * (1 << 30)
    if total > limit * WARN_RATIO:
        raise RuntimeError(
            f"stress dirs exceed cap: {total / (1 << 30):.1f} GB > "
            f"{cap_gb * WARN_RATIO:.0f} GB (90% of {cap_gb:.0f} GB); aborting")
    return total


def bootstrap(data_dir: str | Path, models_dir: str | None = None) -> None:
    """在任何 app.* 导入之前设置环境变量（pydantic-settings 环境变量优先于 .env）。"""
    os.environ["SOPHOS_DATA_DIR"] = str(data_dir).replace("\\", "/")
    os.environ["SOPHOS_DB_PATH"] = (Path(data_dir) / "sophos.db").as_posix()
    if models_dir:
        os.environ["SOPHOS_MODELS_DIR"] = str(models_dir).replace("\\", "/")
    backend = Path(__file__).resolve().parents[2] / "backend"
    if str(backend) not in sys.path:
        sys.path.insert(0, str(backend))
