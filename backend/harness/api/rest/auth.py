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

import threading
import time
from collections import deque
from typing import Any, cast

from fastapi import APIRouter, Request
from pydantic import BaseModel

from harness.api.errors import APIError
from harness.kernel.services import ServiceRegistry

router = APIRouter(tags=["auth"])

# ── SEC-28：登录暴力破解限速（按 IP+用户名 滑动窗口） ──────────────
# 登录端点若无限流，攻击者可离线/在线爆破口令。这里做一个进程内滑动窗口：
# 同一 (IP, 用户名) 在窗口内失败次数超阈值即返回 429，锁定一小段时间。
# 本地单用户工具并发极低，进程内内存窗口足够；多副本部署需换共享存储（Redis），
# 但本产品默认单进程，此处够用。窗口与阈值均为宽松值，避免误伤正常用户。
_LOGIN_WINDOW_SECONDS = 300  # 5 分钟滑动窗口
_LOGIN_MAX_ATTEMPTS = 10  # 窗口内同一 (IP, 用户名) 允许的最大尝试次数
_LOGIN_LOCKOUT_SECONDS = 300  # 触发后锁定时长（同窗口量级）
_login_attempts: dict[str, deque[float]] = {}
# SEC-28：限速计数器用**线程锁**而非 asyncio.Lock。限速的三个辅助函数都是纯内存
# 字典操作，改为同步函数后（早先版本误写成 async 却被同步调用，协程从未执行导致
# 限速永不生效）在单事件循环内本已原子；线程锁仅为防御若未来把端点放进线程池执行。
_login_lock = threading.Lock()


def _get_login_lock() -> threading.Lock:
    """限速计数器的临界区锁（线程级）。"""
    return _login_lock


def _client_ip(request: Request) -> str:
    """取客户端 IP（无信息时占位 unknown）。"""
    try:
        if request.client and request.client.host:
            return request.client.host
    except Exception:  # pragma: no cover
        pass
    return "unknown"


def _login_key(request: Request, username: str) -> str:
    """限速计数键：(客户端 IP, 用户名) 归一化后组合。"""
    return f"{_client_ip(request)}|{username.strip().lower()}"


def _check_login_rate_limit(request: Request, username: str) -> None:
    """在调用认证之前判定是否已超限（只看已有失败计数，不新增）。

    只统计**失败**尝试（见 _record_login_failure），因此这里不 append，
    避免成功登录也累积计数导致共享出口 IP 的正常用户互相误伤。
    """
    key = _login_key(request, username)
    now = time.monotonic()
    with _get_login_lock():
        dq = _login_attempts.get(key)
        if not dq:
            return
        # 丢弃窗口外的旧记录
        cutoff = now - _LOGIN_WINDOW_SECONDS
        while dq and dq[0] < cutoff:
            dq.popleft()
        if len(dq) >= _LOGIN_MAX_ATTEMPTS:
            raise APIError(
                "RATE_LIMITED",
                "登录尝试过于频繁，请稍后再试",
                429,
            )


def _record_login_failure(request: Request, username: str) -> None:
    """认证失败后累加一次尝试（滑动窗口）。"""
    key = _login_key(request, username)
    now = time.monotonic()
    with _get_login_lock():
        dq = _login_attempts.setdefault(key, deque())
        cutoff = now - _LOGIN_WINDOW_SECONDS
        while dq and dq[0] < cutoff:
            dq.popleft()
        dq.append(now)


def _reset_login_attempts(request: Request, username: str) -> None:
    """认证成功后清零该来源的失败计数（用户凭正确密码可自愈解锁）。"""
    key = _login_key(request, username)
    with _get_login_lock():
        _login_attempts.pop(key, None)


def reset_login_rate_limit() -> None:
    """清空全部限速计数（测试隔离 / 运维手动解锁）。

    生产运行无需调用（限速窗口会自动滑动过期）。暴露此入口主要供测试在
    用例之间复位，避免前一用例触发的锁定污染后续用例。
    """
    with _get_login_lock():
        _login_attempts.clear()


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
    # SEC-28：路由重建时清空登录限速计数（不同测试应用/重载互不串扰）。
    _login_attempts.clear()

    @router.get("/api/auth/status", summary="认证状态")
    async def auth_status() -> dict[str, Any]:
        svc = _get_auth_service(registry)
        return {"enabled": bool(svc and svc.enabled)}

    @router.post("/api/auth/login", summary="登录")
    async def login(request: Request, body: LoginPayload) -> dict[str, Any]:
        svc = _get_auth_service(registry)
        if svc is None or not svc.enabled:
            # 未启用认证时不提供登录
            raise APIError("AUTH_DISABLED", "认证未启用", 400)
        # SEC-28：先限速（防爆破），再认证。只统计**失败**尝试（见下），
        # 因此这里对已存在的失败计数做阈值判定，但不会因成功登录累积误伤。
        _check_login_rate_limit(request, body.username)
        result = svc.login(body.username, body.password)
        if result is None:
            # 统一错误消息：无论"用户不存在"还是"密码错误"都同样措辞，
            # 配合 authenticate 内的哑哈希陪跑，消除用户名枚举侧信道。
            # 仅**失败**才累加计数：成功登录不计数，避免共享出口 IP 的正常
            # 用户互相触发限速。
            _record_login_failure(request, body.username)
            raise APIError("INVALID_CREDENTIALS", "用户名或密码错误", 401)
        # 登录成功：清零该来源的失败计数（爆破锁定后用户用正确密码可自愈）。
        _reset_login_attempts(request, body.username)
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
