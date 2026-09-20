"""访问口令鉴权（R6，ADR-026）。

语义：单密码访问控制，`SOPHOS_ACCESS_PASSWORD` 为空 = 不启用（本机/可信
局域网旧行为）。启用后：

- 登录：`POST /api/auth/login` {password} → hmac.compare_digest 常量时间
  比较；通过则种 `sophos_session` HttpOnly Cookie（SameSite=Lax）。Cookie
  值 = sha256("sophos:" + password)——**由当前密码派生**，改密码即让所有
  旧会话失效，无需服务端会话存储。
- 保护范围（AuthMiddleware）：`/api/*`（除 `/api/auth/*`、`/api/health`）
  与 `/docs` `/openapi.json` `/redoc`。前端 SPA 静态资源放行（不含数据，
  页面自身 401 后弹登录层）；缩略图/视频流是 `<img>/<video>` 同源请求，
  浏览器自动携带 Cookie，无需逐端点改造。
- `GET /api/auth/status` → {required, authed}，前端用于决定是否弹登录层。
"""
from __future__ import annotations

import hashlib
import hmac

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from app.config import settings

router = APIRouter(prefix="/auth", tags=["auth"])

COOKIE_NAME = "sophos_session"


def session_token(password: str) -> str:
    """会话令牌 = sha256(固定盐 + 当前密码)。改密码 → 旧 Cookie 全部失效。"""
    return hashlib.sha256(f"sophos:{password}".encode("utf-8")).hexdigest()


def check_password(candidate: str) -> bool:
    return hmac.compare_digest(str(candidate), str(settings.access_password))


def is_authed(request: Request) -> bool:
    if not settings.access_password:
        return True
    cookie = request.cookies.get(COOKIE_NAME, "")
    return hmac.compare_digest(cookie, session_token(settings.access_password))


class LoginBody(BaseModel):
    password: str = ""


@router.get("/status")
def auth_status(request: Request) -> dict:
    return {"required": bool(settings.access_password), "authed": is_authed(request)}


@router.post("/login")
def login(body: LoginBody, response: Response) -> dict:
    if not settings.access_password:
        return {"ok": True}  # 未启用鉴权：登录动作无意义但无害
    if not check_password(body.password):
        raise HTTPException(status_code=401, detail={
            "code": "BAD_PASSWORD", "message": "访问密码错误",
        })
    response.set_cookie(
        COOKIE_NAME, session_token(settings.access_password),
        max_age=60 * 60 * 24 * 30,  # 30 天；改密码即失效
        httponly=True, samesite="lax",
    )
    return {"ok": True}


@router.post("/logout")
def logout(response: Response) -> dict:
    response.delete_cookie(COOKIE_NAME)
    return {"ok": True}
