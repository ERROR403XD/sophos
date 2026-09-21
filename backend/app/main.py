"""Sophos 后端入口（M3 装配）。

装配内容：config → 数据库初始化（九张表）→ 遗留任务清理 → 工作目录播种
→ 任务处理器注册 → worker 启动 → API 路由 → thumbs 静态托管。
frontend/dist 静态托管在 M5 接入。
"""
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app import APP_VERSION
from app.api import (analyze, auth, faces, health, jobs as jobs_api, process,
                     ratings, scan, settings as settings_api,
                     thumbs as thumbs_api, train, videos, workdirs)
from app.api.middleware import AuthMiddleware
from app.config import settings
from app.db import session as db_session_mod
from app.db.session import init_engine
from app.services import automation
from app.services import hls as hls_svc
from app.services import jobs as jobsvc
from app.services import pipeline
from app.services.kv import seed_work_dirs_from_env

log = logging.getLogger("sophos")


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_engine(settings)
    # R9：同步端点/run_in_threadpool 共用的 anyio 线程池容量（默认 40）。播放
    # 相关同步路径单次最长等待秒级（HLS 起播预检、转码槽），快速退出/反复播放
    # 连发的请求会把 40 个线程占满 → **所有**同步端点排队（页面失去响应）。
    # 提高上限只是兜底（确定性修复见 hls.wait_playlist 快返 / 槽位即时归还 /
    # 登记锁外等槽），读多写少的本地服务可安全放大。
    try:
        import anyio.to_thread

        anyio.to_thread.current_default_thread_limiter().total_tokens = 64
    except Exception:  # noqa: BLE001 —— 拿不到限流器不影响主流程
        log.warning("cannot raise anyio thread limiter; keeping default", exc_info=True)
    thumbs_dir = Path(settings.data_dir) / "thumbs"
    thumbs_dir.mkdir(parents=True, exist_ok=True)
    hls_svc.cleanup_stale_dirs()  # R8：清上次进程遗留的 HLS 会话目录
    with db_session_mod.SessionLocal() as db:
        stale = jobsvc.reset_stale_jobs(db)
        seeded = seed_work_dirs_from_env(db, settings)
        if stale or seeded:
            log.info("startup: stale jobs reset=%s, workdirs seeded=%s", stale, seeded)
    pipeline.register_all()
    jobsvc.start_worker()
    automation.start_automation()  # R6.3：自动扫描/处理面容联动（默认关闭）
    yield
    automation.stop_automation()
    hls_svc.shutdown_all()  # R8：退出前杀掉存活的 HLS ffmpeg


app = FastAPI(title="Sophos", version=APP_VERSION, lifespan=lifespan)

app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])
# R6（ADR-026）：访问口令中间件。add_middleware 后添加者在最外层（先于 CORS
# 执行），因此中间件自身放行 OPTIONS 预检，交由内层 CORSMiddleware 处理。
app.add_middleware(AuthMiddleware)


@app.exception_handler(HTTPException)
async def http_exception_handler(_: Request, exc: HTTPException) -> JSONResponse:
    """统一错误体为 {code, message}（docs/API_DESIGN.md §1/§4）。"""
    if isinstance(exc.detail, dict):
        return JSONResponse(status_code=exc.status_code, content=exc.detail)
    return JSONResponse(status_code=exc.status_code,
                        content={"code": "ERROR", "message": str(exc.detail)})


for _router in (health.router, auth.router, workdirs.router, scan.router,
                process.router, jobs_api.router, videos.router, faces.router,
                ratings.router, train.router, settings_api.router,
                thumbs_api.router, analyze.router):
    app.include_router(_router, prefix="/api")

# frontend 构建产物托管（M5）；dist 不存在时跳过（纯后端模式）
_dist = Path(__file__).resolve().parents[2] / "frontend" / "dist"
if _dist.is_dir():
    app.mount("/", StaticFiles(directory=str(_dist), html=True), name="frontend")
else:
    log.info("frontend dist not found (%s); API-only mode", _dist)
