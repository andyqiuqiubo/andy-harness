"""REST API —— 认证、当前用户与用户管理（E12）。

- GET  /api/auth/status    认证是否启用（公开）
- POST /api/auth/login     用户名密码登录，返回 token（公开）
- POST /api/auth/logout    登出（无状态，客户端删除 token 即可）
- GET  /api/auth/me        当前登录用户
- GET  /api/users          列出用户（管理员）
- POST /api/users          创建用户（管理员）
- DELETE /api/users/{id}   删除用户（管理员）
"""

from __future__ import annotations

from typing import Any, cast

from fastapi import APIRouter, Request
from pydantic import BaseModel

from harness.api.errors import APIError
from harness.kernel.services import ServiceRegistry

router = APIRouter(tags=["auth"])


class LoginPayload(BaseModel):
    """登录请求。"""

    username: str
    password: str


class CreateUserPayload(BaseModel):
    """管理员创建用户请求。"""

    username: str
    password: str
    is_admin: bool = False


def _get_auth_service(registry: ServiceRegistry) -> Any:
    """获取 AuthService（未装配返回 None）。"""
    from harness.modules.auth_manager.service import AuthService

    if not registry.has(AuthService):
        return None
    return registry.get(AuthService)


def _require_admin(request: Request) -> dict[str, Any]:
    """取当前用户并要求管理员，否则 403。"""
    user: dict[str, Any] | None = getattr(request.state, "user", None)
    if not isinstance(user, dict) or not user.get("is_admin"):
        raise APIError("FORBIDDEN", "需要管理员权限", 403)
    return user


def setup_auth_routes(registry: ServiceRegistry) -> None:
    """注册认证与用户管理路由。

    模块级 router 会被多个测试应用复用：先清理此前注册的闭包，避免
    旧应用（不同 ServiceRegistry）的路由优先匹配。
    """
    router.routes.clear()

    @router.get("/api/auth/status", summary="认证状态")
    async def auth_status() -> dict[str, Any]:
        svc = _get_auth_service(registry)
        return {"enabled": bool(svc and svc.enabled)}

    @router.post("/api/auth/login", summary="登录")
    async def login(body: LoginPayload) -> dict[str, Any]:
        svc = _get_auth_service(registry)
        if svc is None or not svc.enabled:
            # 未启用认证时不提供登录
            raise APIError("AUTH_DISABLED", "认证未启用", 400)
        result = svc.login(body.username, body.password)
        if result is None:
            raise APIError("INVALID_CREDENTIALS", "用户名或密码错误", 401)
        return cast(dict[str, Any], result)

    @router.post("/api/auth/logout", summary="登出")
    async def logout() -> dict[str, str]:
        # 无状态 token：服务端无需注销，由客户端删除。
        return {"status": "ok"}

    @router.get("/api/auth/me", summary="当前用户")
    async def me(request: Request) -> dict[str, Any]:
        user = getattr(request.state, "user", None)
        if not user:
            raise APIError("UNAUTHORIZED", "未认证", 401)
        return cast(dict[str, Any], user)

    @router.get("/api/users", summary="列出用户")
    async def list_users(request: Request) -> list[dict[str, Any]]:
        _require_admin(request)
        svc = _get_auth_service(registry)
        return [u.to_dict() for u in svc.list_users()]

    @router.post("/api/users", summary="创建用户")
    async def create_user(request: Request, body: CreateUserPayload) -> dict[str, Any]:
        _require_admin(request)
        svc = _get_auth_service(registry)
        try:
            user = svc.create_user(body.username, body.password, is_admin=body.is_admin)
        except ValueError as e:
            raise APIError("USER_INVALID", str(e), 400)
        return cast(dict[str, Any], user.to_dict())

    @router.delete("/api/users/{user_id}", summary="删除用户")
    async def delete_user(request: Request, user_id: str) -> dict[str, str]:
        _require_admin(request)
        current = request.state.user
        if current and current.get("id") == user_id:
            raise APIError("USER_INVALID", "不能删除当前登录用户", 400)
        svc = _get_auth_service(registry)
        try:
            if not svc.delete_user(user_id):
                raise APIError("USER_NOT_FOUND", f"用户不存在: {user_id}", 404)
        except ValueError as e:
            raise APIError("USER_INVALID", str(e), 400)
        return {"status": "deleted"}
