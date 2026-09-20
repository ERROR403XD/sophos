"""缩略图动态端点（R5 大规模改造，ADR-024）。

替代原 StaticFiles 平铺挂载：按 identity id 计算分片路径（旧平铺文件回落），
并下发长缓存头——rep_thumb 带 ?v={updated_at} 内容版本参数，identity 重建
（重处理）后 URL 变化自然失效缓存；id 不变内容不变 → immutable 长缓存，
评分/对比页二次访问零下载。
"""
from fastapi import APIRouter
from fastapi.responses import FileResponse

from app.services import thumbs

router = APIRouter(prefix="/thumbs", tags=["thumbs"])

_CACHE = "public, max-age=31536000, immutable"


@router.get("/{identity_id}.jpg")
def get_thumb(identity_id: int):
    p = thumbs.find_thumb(identity_id)
    if p is None:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail={
            "code": "THUMB_NOT_FOUND",
            "message": f"thumb for identity {identity_id} not found",
        })
    return FileResponse(p, media_type="image/jpeg",
                        headers={"Cache-Control": _CACHE})
