"""运行时设置 API（R5，ADR-022）：分批大小 / 每人面容上限等 WebUI 可调项。

GET  返回合并后的全部白名单设置（默认值 ← .env ← kv 覆盖）；
PUT  校验并保存覆盖值（立即对后续任务生效），非法键/类型/范围 → 400。
"""
from fastapi import APIRouter, Depends, HTTPException
from fastapi import Body
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.services import runtime_settings

router = APIRouter(prefix="/settings", tags=["settings"])


@router.get("")
def read_settings(db: Session = Depends(get_db)) -> dict:
    return {"settings": runtime_settings.get_all(db)}


@router.put("")
def update_settings(payload: dict = Body(...),
                    db: Session = Depends(get_db)) -> dict:
    try:
        merged = runtime_settings.set_values(db, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={  # noqa: B904
            "code": "INVALID_SETTING", "message": str(exc),
        })
    return {"settings": merged}
