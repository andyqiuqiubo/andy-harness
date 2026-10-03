"""REST API —— 工具权限与人工确认策略路由。"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from harness.api.deps import require_admin
from harness.kernel.services import ServiceRegistry
from harness.modules.permission_manager.service import (
    VALID_ACTIONS,
    VALID_MODES,
    PermissionService,
)

logger = logging.getLogger("harness.api.permissions")

router = APIRouter(prefix="/api/permissions", tags=["permissions"])

_services: ServiceRegistry | None = None


class PermissionUpdate(BaseModel):
    """更新权限策略请求。"""

    mode: str | None = Field(default=None, description="全局策略模式")
    overrides: dict[str, str] | None = Field(default=None, description="单工具覆盖：tool_name -> auto/confirm/deny")


class ToolOverrideRequest(BaseModel):
    """单工具覆盖请求。"""

    action: str | None = Field(default=None, description="auto / confirm / deny，null 表示清除覆盖")


def setup_permission_routes(services: ServiceRegistry) -> None:
    """注册权限路由。"""
    global _services
    _services = services


def _get_service() -> PermissionService | None:
    """获取 PermissionService，不可用返回 None。"""
    if _services is None or not _services.has(PermissionService):
        return None
    service: PermissionService = _services.get(PermissionService)
    return service


def _build_state(service: PermissionService) -> dict[str, Any]:
    """构造权限状态完整表示（GET 与 PUT 共用，保证前端无需重新拉取即可渲染）。"""
    return {
        "available": True,
        "mode": service.get_mode(),
        "modes": list(VALID_MODES),
        "actions": list(VALID_ACTIONS),
        "overrides": service.get_overrides(),
        "tools": service.list_tool_risks(),
    }


@router.get("", summary="获取权限策略与工具风险清单")
async def get_permissions() -> dict[str, Any]:
    """返回当前策略模式、单工具覆盖，以及每个工具的等级与生效动作。"""
    service = _get_service()
    if service is None:
        return {"available": False, "mode": None, "overrides": {}, "tools": []}
    return _build_state(service)


@router.put("", summary="更新权限策略", dependencies=[Depends(require_admin)])
async def update_permissions(req: PermissionUpdate) -> dict[str, Any]:
    """更新全局模式与/或单工具覆盖。返回与 GET 一致的完整状态。"""
    service = _get_service()
    if service is None:
        raise HTTPException(status_code=503, detail="权限服务不可用")

    if req.mode is not None and not service.set_mode(req.mode):
        raise HTTPException(status_code=400, detail=f"无效的策略模式: {req.mode}，可选: {list(VALID_MODES)}")

    if req.overrides is not None:
        for tool_name, action in req.overrides.items():
            if action is not None and action not in VALID_ACTIONS:
                raise HTTPException(
                    status_code=400,
                    detail=f"无效的覆盖动作: {action}，可选: {list(VALID_ACTIONS)}",
                )
            service.set_override(tool_name, action)

    return _build_state(service)


@router.put("/{tool_name}", summary="设置单工具覆盖", dependencies=[Depends(require_admin)])
async def set_tool_override(tool_name: str, req: ToolOverrideRequest) -> dict[str, Any]:
    """为单个工具设置 auto / confirm / deny，传 null 清除覆盖。返回完整状态。"""
    service = _get_service()
    if service is None:
        raise HTTPException(status_code=503, detail="权限服务不可用")
    if req.action is not None and req.action not in VALID_ACTIONS:
        raise HTTPException(
            status_code=400,
            detail=f"无效的覆盖动作: {req.action}，可选: {list(VALID_ACTIONS)}",
        )
    service.set_override(tool_name, req.action)
    return _build_state(service)
