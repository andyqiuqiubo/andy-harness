"""认证中间件（E12）。

启用认证后，对 `/api/**` 强制 Bearer token；以下路径豁免：
- `GET /api/health` 健康检查；
- `GET /api/auth/status` 认证状态；
- `POST /api/auth/login` 登录；
- `/api/channels/inbound/...` 渠道入站（由渠道密钥保护）；
- `OPTIONS` CORS 预检。

未启用认证或无 AuthService 时完全放行（本地单用户行为不变）。
WebSocket 不走 HTTP 中间件，在端点内按 `?token=` 校验。
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response
from starlette.types import ASGIApp

# 精确豁免的路径。
_EXACT_PUBLIC = {
    "/api/health",
    "/api/auth/status",
    "/api/auth/login",
}
# 前缀豁免的路径（渠道入站，另由 X-Channel-Secret 保护）。
_PREFIX_PUBLIC = ("/api/channels/inbound/",)


def _unauthorized(message: str = "未认证或登录已过期，请重新登录") -> JSONResponse:
    """构造统一格式的 401 响应。"""
    return JSONResponse(
        status_code=401,
        content={
            "code": "UNAUTHORIZED",
            "message": message,
            "detail": None,
            "trace_id": str(uuid.uuid4()),
        },
    )


class AuthMiddleware(BaseHTTPMiddleware):
    """Bearer token 认证中间件。"""

    def __init__(self, app: ASGIApp, get_auth_service: Callable[[], Any]) -> None:
        super().__init__(app)
        self._get_auth_service = get_auth_service

    async def dispatch(self, request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        path = request.url.path
        if (
            not path.startswith("/api/")
            or request.method == "OPTIONS"
            or path in _EXACT_PUBLIC
            or path.startswith(_PREFIX_PUBLIC)
        ):
            return await call_next(request)

        auth_service = self._get_auth_service()
        # 服务未装配 / 认证未启用 → 放行（本地单用户）。
        if auth_service is None or not getattr(auth_service, "enabled", False):
            return await call_next(request)

        header = request.headers.get("Authorization", "")
        token = header[7:].strip() if header.startswith("Bearer ") else ""
        if not token:
            return _unauthorized()
        user = auth_service.user_from_token(token)
        if user is None:
            return _unauthorized("登录已失效，请重新登录")
        request.state.user = user.to_dict()
        request.state.token = token
        return await call_next(request)


def _json(raw: object) -> str:
    """统一 JSON 序列化（供测试 / 复用）。"""
    return json.dumps(raw, ensure_ascii=False)
