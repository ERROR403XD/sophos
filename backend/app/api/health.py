"""健康检查（M2）。"""
from fastapi import APIRouter

from app import APP_VERSION
from app.db import session as db_session_mod

router = APIRouter()


@router.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "version": APP_VERSION,
        "db": "ready" if db_session_mod.SessionLocal is not None else "not-initialized",
    }
