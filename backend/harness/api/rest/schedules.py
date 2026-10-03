"""REST API —— 定时任务（计划任务）。

列表 / 新建 / 更新 / 删除 / 立即执行 / 运行历史 / 可选项（MCP、Skills、工具、模型）。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from harness.api.errors import APIError
from harness.kernel.services import ServiceRegistry
from harness.modules.scheduler.service import VALID_TYPES, SchedulerService

logger = logging.getLogger("harness.api.schedules")

router = APIRouter(prefix="/api/schedules", tags=["schedules"])


# 以下模型必须定义在路由装饰器之前（from __future__ import annotations 下
# 装饰器运行时会解析注解，定义在后面的模型会因 ForwardRef 未解析而 422）。
class ScheduleSpecModel(BaseModel):
    """调度规则。"""

    type: str = "daily"
    time: str = "09:00"
    weekdays: list[int] = Field(default_factory=list)
    interval_minutes: int = 60
    run_at: str = ""


class TaskCreate(BaseModel):
    """新建定时任务。"""

    name: str
    description: str = ""
    enabled: bool = True
    schedule: ScheduleSpecModel = Field(default_factory=ScheduleSpecModel)
    prompt: str = ""
    mcp_servers: list[str] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    provider_id: str = ""
    model: str = ""
    max_iterations: int = 8
    timeout_seconds: int = 300


class TaskUpdate(BaseModel):
    """更新定时任务（仅提供的字段会被更新）。"""

    name: str | None = None
    description: str | None = None
    enabled: bool | None = None
    schedule: ScheduleSpecModel | None = None
    prompt: str | None = None
    mcp_servers: list[str] | None = None
    skills: list[str] | None = None
    tools: list[str] | None = None
    provider_id: str | None = None
    model: str | None = None
    max_iterations: int | None = None
    timeout_seconds: int | None = None


def _get_service(registry: ServiceRegistry) -> SchedulerService:
    try:
        svc = registry.get(SchedulerService)
    except Exception as e:
        raise APIError("SCHEDULER_UNAVAILABLE", "定时任务服务未启用", 503) from e
    if not isinstance(svc, SchedulerService):
        raise APIError("SCHEDULER_UNAVAILABLE", "定时任务服务未启用", 503)
    return svc


def setup_schedule_routes(registry: ServiceRegistry) -> None:
    """注册定时任务路由。"""

    @router.get("/options", summary="可选项：MCP / Skills / 工具 / 模型")
    async def options() -> dict[str, Any]:
        result: dict[str, Any] = {
            "mcp_servers": [],
            "skills": [],
            "tools": [],
            "providers": [],
        }

        # MCP servers
        try:
            from harness.modules.mcp_client.service import MCPClientService

            mcp = registry.get(MCPClientService)
            tools = mcp.list_tools()
            for name, cfg in mcp.get_configs().items():
                result["mcp_servers"].append(
                    {
                        "name": name,
                        "type": cfg.type,
                        "connected": mcp.is_connected(name),
                        "tool_count": len([t for t in tools if t.server == name]),
                        "description": cfg.description,
                    }
                )
        except Exception:  # noqa: BLE001
            pass

        # Skills
        try:
            from harness.modules.skill_manager.service import SkillService

            skill_service = registry.get(SkillService)
            result["skills"] = [
                {
                    "name": m.name,
                    "description": m.description,
                    "enabled": m.enabled,
                    "source": m.source,
                }
                for m in skill_service.list_skills()
            ]
        except Exception:  # noqa: BLE001
            pass

        # 非 MCP 工具（排除 use_skill / task 这类由系统托管或会递归的）
        try:
            from harness.engine.tool_registry import ToolRegistry

            tool_registry = registry.get(ToolRegistry)
            skip = {"use_skill", "task", "read_artifact"}
            for info in tool_registry.list_tools():
                name = info["name"]
                if name.startswith("mcp__") or name in skip:
                    continue
                tool = tool_registry.get(name)
                result["tools"].append(
                    {
                        "name": name,
                        "description": str(getattr(tool, "description", ""))[:200],
                        "risk": str(getattr(tool, "risk_level", "write")),
                    }
                )
        except Exception:  # noqa: BLE001
            pass

        # Provider / models
        try:
            from harness.modules.model_manager.provider_registry import ProviderRegistry

            for info in registry.get(ProviderRegistry).list_providers():
                if not info.get("enabled", True):
                    continue
                result["providers"].append(
                    {
                        "id": info.get("id", ""),
                        "name": info.get("name", ""),
                        "models": info.get("models", []),
                        "ready": bool(info.get("has_api_key")),
                    }
                )
        except Exception:  # noqa: BLE001
            pass

        return result

    @router.get("", summary="列出定时任务")
    async def list_tasks() -> dict[str, Any]:
        svc = _get_service(registry)
        tasks = svc.list_tasks()
        return {"available": True, "tasks": [t.to_dict() for t in tasks], "count": len(tasks)}

    @router.post("", summary="新建定时任务")
    async def create_task(body: TaskCreate) -> dict[str, Any]:
        svc = _get_service(registry)
        if not body.name.strip():
            raise APIError("SCHEDULE_INVALID", "任务名称不能为空", 400)
        if not body.prompt.strip():
            raise APIError("SCHEDULE_INVALID", "任务内容（prompt）不能为空", 400)
        if body.schedule.type not in VALID_TYPES:
            raise APIError("SCHEDULE_INVALID", f"无效的调度类型: {body.schedule.type}", 400)
        task = svc.create_task(body.model_dump())
        return {"task": task.to_dict()}

    @router.patch("/{task_id}", summary="更新定时任务")
    async def update_task(task_id: str, body: TaskUpdate) -> dict[str, Any]:
        svc = _get_service(registry)
        data = body.model_dump(exclude_none=True)
        if "schedule" in data and data["schedule"].get("type") not in VALID_TYPES:
            raise APIError("SCHEDULE_INVALID", "无效的调度类型", 400)
        updated = svc.update_task(task_id, data)
        if updated is None:
            raise APIError("SCHEDULE_NOT_FOUND", f"任务不存在: {task_id}", 404)
        return {"task": updated.to_dict()}

    @router.delete("/{task_id}", summary="删除定时任务")
    async def delete_task(task_id: str) -> dict[str, Any]:
        svc = _get_service(registry)
        if not svc.delete_task(task_id):
            raise APIError("SCHEDULE_NOT_FOUND", f"任务不存在: {task_id}", 404)
        return {"removed": task_id}

    @router.post("/{task_id}/run", summary="立即执行一次（同步等待结果）")
    async def run_now(task_id: str) -> dict[str, Any]:
        from harness.modules.scheduler.runner import TaskRunner

        svc = _get_service(registry)
        task = svc.get_task(task_id)
        if task is None:
            raise APIError("SCHEDULE_NOT_FOUND", f"任务不存在: {task_id}", 404)
        try:
            runner = registry.get(TaskRunner)
        except Exception as e:
            raise APIError("SCHEDULER_UNAVAILABLE", "任务执行器未启用", 503) from e
        outcome = await runner.run_task(task, trigger="manual")
        refreshed = svc.get_task(task_id)
        return {
            "outcome": outcome.to_dict(),
            "task": refreshed.to_dict() if refreshed else None,
        }

    @router.get("/{task_id}/runs", summary="运行历史")
    async def list_runs(task_id: str, limit: int = 20) -> dict[str, Any]:
        svc = _get_service(registry)
        if svc.get_task(task_id) is None:
            raise APIError("SCHEDULE_NOT_FOUND", f"任务不存在: {task_id}", 404)
        runs = svc.list_runs(task_id, limit=max(1, min(limit, 100)))
        return {"runs": [r.to_dict() for r in runs], "count": len(runs)}

    @router.delete("/{task_id}/runs", summary="清空运行历史")
    async def clear_runs(task_id: str) -> dict[str, Any]:
        svc = _get_service(registry)
        if svc.get_task(task_id) is None:
            raise APIError("SCHEDULE_NOT_FOUND", f"任务不存在: {task_id}", 404)
        return {"removed": svc.clear_runs(task_id)}
