"""工作目录管理（M2）。存 kv_setting.work_dirs；目录必须存在。"""
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.services.kv import get_work_dirs, set_work_dirs

router = APIRouter(prefix="/workdirs", tags=["workdirs"])


class WorkDirIn(BaseModel):
    path: str


@router.get("")
def list_workdirs(db: Session = Depends(get_db)) -> dict:
    return {"workdirs": get_work_dirs(db)}


@router.post("", status_code=201)
def add_workdir(body: WorkDirIn, db: Session = Depends(get_db)) -> dict:
    if not Path(body.path).is_dir():
        raise HTTPException(status_code=400, detail={
            "code": "DIR_NOT_FOUND",
            "message": f"directory not found: {body.path}",
        })
    dirs = get_work_dirs(db)
    if body.path not in dirs:
        dirs = set_work_dirs(db, dirs + [body.path])
    return {"workdirs": dirs}


@router.delete("")
def remove_workdir(path: str, db: Session = Depends(get_db)) -> dict:
    dirs = get_work_dirs(db)
    if path not in dirs:
        raise HTTPException(status_code=404, detail={
            "code": "NOT_FOUND",
            "message": f"workdir not registered: {path}",
        })
    dirs = set_work_dirs(db, [d for d in dirs if d != path])
    return {"ok": True, "workdirs": dirs}
