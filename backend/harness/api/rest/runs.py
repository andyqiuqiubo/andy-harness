"""REST API —— 运行检查点与断点续跑（G4）。

提供 ``POST /api/runs/{run_id}/resume``：依据检查点从已持久化的会话上下文续跑，
用于服务重启 / 崩溃后的恢复。依赖 AgentLoop / ProviderRegistry，缺组件时优雅降级。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter

from harness.engine.tool_registry import ToolRegistry
from harness.kernel.hooks import HookManager
from harness.kernel.services import ServiceRegistry

logger = logging.getLogger("harness.api.runs")

router = APIRouter(prefix="/api/runs", tags=["runs"])

# 由 setup_runs_routes 注入的运行期组件
_services: ServiceRegistry | None = None
_hooks: HookManager | None = None
_tool_registry: ToolRegistry | None = None


def setup_runs_routes(
    services: ServiceRegistry,
    hooks: HookManager,
    tool_registry: ToolRegistry,
) -> None:
    """注册运行检查点路由所需的运行期组件。"""
    global _services, _hooks, _tool_registry
    _services = services
    _hooks = hooks
    _tool_registry = tool_registry


@router.post("/{run_id}/resume", summary="从检查点续跑一次运行")
async def resume_run(run_id: str) -> dict[str, Any]:
    """依据检查点续跑（断点续跑）。

    会话历史已在之前运行中即时持久化，续跑会基于该上下文让模型继续完成剩余工作。
    需存在已启用且已配置 Key 的 provider；无可用 provider 时返回 error 字段。
    """
    if _services is None or _hooks is None or _tool_registry is None:
        return {
            "run_id": run_id,
            "session_id": "",
            "content": "",
            "error": "运行期组件未就绪",
        }
    from harness.engine.checkpoint import resume_run as _resume_run

    result = await _resume_run(
        run_id=run_id,
        services=_services,
        hooks=_hooks,
        tool_registry=_tool_registry,
    )
    return result
