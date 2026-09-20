"""访问口令中间件（R6，ADR-026）。

纯 ASGI 中间件（避免 BaseHTTPMiddleware 包裹 StreamingResponse 的缓冲/
取消语义问题）。规则见 app/api/auth.py 模块 docstring：

- 未设置口令 → 全放行；
- CORS 预检（OPTIONS）放行（CORSMiddleware 在其内层处理）；
- 放行清单：`/api/auth/*`、`/api/health`、非 `/api` 前缀的静态 SPA 资源；
- 其余 `/api/*` 与 `/docs` `/openapi.json` `/redoc` 须持有效会话 Cookie，
  否则 401 `{code:"UNAUTHORIZED"}`（前端据此弹登录层）。
"""
from __future__ import annotations

from starlette.types import ASGIApp, Receive, Scope, Send

from app.api.auth import session_token

# 登录与健康检查端点保持开放（容器健康检查 / 登录层自举用）
_OPEN_API_PATHS = {"/api/auth/status", "/api/auth/login", "/api/health"}
_PROTECTED_DOC_PATHS = {"/docs", "/openapi.json", "/redoc"}


def _cookie_token(scope: Scope) -> str:
    """从原始 ASGI scope 提取 sophos_session Cookie（不构造 Request 对象）。"""
    wanted = b"cookie"
    for key, value in scope.get("headers") or []:
        if key.lower() == wanted:
            for part in value.decode("latin-1").split(";"):
                name, _, val = part.strip().partition("=")
                if name == "sophos_session":
                    return val
    return ""


class AuthMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        from app.config import settings

        if (scope["type"] != "http" or scope.get("method") == "OPTIONS"
                or not settings.access_password or not self._needs_auth(scope.get("path", ""))):
            await self.app(scope, receive, send)
            return
        import hmac as _hmac

        if _hmac.compare_digest(_cookie_token(scope),
                                session_token(settings.access_password)):
            await self.app(scope, receive, send)
            return
        body = ('{"code":"UNAUTHORIZED","message":"需要访问密码，请刷新页面重新登录"}'
                .encode("utf-8"))
        await send({
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json; charset=utf-8"),
                (b"content-length", str(len(body)).encode()),
            ],
        })
        await send({"type": "http.response.body", "body": body})

    @staticmethod
    def _needs_auth(path: str) -> bool:
        if path.startswith("/api"):
            return path not in _OPEN_API_PATHS
        return path in _PROTECTED_DOC_PATHS
