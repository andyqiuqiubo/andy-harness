"""FastAPI 路由依赖（鉴权 / 上下文）。

集中放置可复用的 `Depends` 依赖，避免在多个路由模块里重复实现鉴权逻辑。
"""

from __future__ import annotations

from typing import Any

from fastapi import Request

from harness.api.errors import APIError


def require_admin(request: Request) -> dict[str, Any] | None:
    """要求当前登录用户为管理员；认证未启用时放行。

    判定依据：认证中间件仅在「认证启用且 token 有效」时给
    `request.state.user` 赋值。因此：

    - 认证未启用（本地单用户模式）：`request.state.user` 不存在 → 直接放行，
      保持与单用户默认行为一致；
    - 认证启用 + 管理员：放行；
    - 认证启用 + 非管理员：返回 403。

    用于保护「会改变系统状态」的管理面端点（插件 / MCP / 权限 / 设置写操作）。
    返回被注入的用户字典（管理员），未启用认证时返回 ``None``。
    """
    user: dict[str, Any] | None = getattr(request.state, "user", None)
    if user is None:
        return None
    if not isinstance(user, dict) or not user.get("is_admin"):
        raise APIError("FORBIDDEN", "需要管理员权限", 403)
    return user
