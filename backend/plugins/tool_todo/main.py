"""todo_write 工具插件 —— 创建/更新会话任务清单。

采用覆盖式写入：模型每轮提交**完整**列表，语义幂等、不易错乱。
"""

from __future__ import annotations

import logging
from typing import Any

from harness.kernel.context import PluginContext
from harness.kernel.contracts.base import BasePlugin, PluginManifest
from harness.kernel.contracts.tool import ToolPlugin
from harness.kernel.services import ServiceRegistry
from harness.modules.todo_manager.service import (
    STATUS_COMPLETED,
    STATUS_IN_PROGRESS,
    STATUS_PENDING,
    TodoService,
)

logger = logging.getLogger("harness.tools.todo")

STATUS_LABEL = {
    STATUS_PENDING: "待开始",
    STATUS_IN_PROGRESS: "进行中",
    STATUS_COMPLETED: "已完成",
}


class TodoWriteTool(ToolPlugin):
    """写入任务清单的工具。"""

    def __init__(self, services: ServiceRegistry) -> None:
        self._services = services

    @property
    def tool_name(self) -> str:
        return "todo_write"

    @property
    def risk_level(self) -> str:
        """仅修改本会话的待办列表，属写操作但影响面可控。"""
        return "write"

    @property
    def needs_session(self) -> bool:
        """待办列表归属会话，需要 AgentLoop 注入 session_id。"""
        return True

    @property
    def description(self) -> str:
        return (
            "创建或更新当前会话的任务清单（Todo 列表），用于拆解和跟踪多步骤任务。\n"
            "何时使用：任务包含 3 个以上步骤、需要长期推进、或用户明确要求做计划时。\n"
            "用法：每次传入**完整**的任务列表（会整体覆盖旧列表）；"
            "把正在做的那一项标为 in_progress，已完成的标为 completed。\n"
            "任务全部完成时，把最后一项标为 completed 并给出总结即可，无需清空列表。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "todos": {
                    "type": "array",
                    "description": "完整任务列表（覆盖式写入）",
                    "items": {
                        "type": "object",
                        "properties": {
                            "content": {
                                "type": "string",
                                "description": "任务内容，简洁明确",
                            },
                            "status": {
                                "type": "string",
                                "enum": ["pending", "in_progress", "completed"],
                                "description": "任务状态，默认 pending",
                            },
                            "id": {
                                "type": "string",
                                "description": "可选，稳定标识；省略则自动生成",
                            },
                        },
                        "required": ["content"],
                    },
                }
            },
            "required": ["todos"],
        }

    async def execute(self, args: dict[str, Any]) -> str:
        """覆盖写入任务清单并返回当前状态。"""
        raw_items = args.get("todos")
        if raw_items is None:
            return "错误: 缺少 todos 参数"
        if not isinstance(raw_items, list):
            return "错误: todos 必须是数组"

        if not self._services.has(TodoService):
            return "错误: Todo 服务不可用（todo_manager 插件未激活）"

        service: TodoService = self._services.get(TodoService)
        session_id = str(args.get("session_id", "") or "")
        if not session_id:
            return "错误: 无法确定当前会话（未注入 session_id）"

        saved = service.replace_todos(session_id, raw_items)
        if not saved:
            return "任务清单已清空。"

        lines = [f"任务清单已更新（共 {len(saved)} 项）："]
        for item in saved:
            mark = {
                STATUS_PENDING: "[ ]",
                STATUS_IN_PROGRESS: "[~]",
                STATUS_COMPLETED: "[x]",
            }.get(item.status, "[ ]")
            lines.append(f"{mark} {item.content}（{STATUS_LABEL.get(item.status, item.status)}）")
        summary = service.summary(session_id)
        lines.append(f"进度：已完成 {summary.get(STATUS_COMPLETED, 0)} / {len(saved)}")
        return "\n".join(lines)


class TodoPlugin(BasePlugin):
    """todo_write 工具插件。"""

    manifest: PluginManifest
    _ctx: PluginContext | None = None

    def __init__(self) -> None:
        self._ctx = None

    async def activate(self, ctx: PluginContext) -> None:
        """激活：注册工具到 ToolRegistry。"""
        self._ctx = ctx

        from harness.engine.tool_registry import ToolRegistry

        if not ctx.services.has(ToolRegistry):
            ctx.services.register(ToolRegistry, ToolRegistry(), owner=self.plugin_id)
        tool_registry = ctx.services.get(ToolRegistry)

        tool_registry.register(TodoWriteTool(ctx.services), owner=self.plugin_id)
        ctx.logger.info("todo_write 工具已注册")

    async def deactivate(self, ctx: PluginContext) -> None:
        """停用：注销工具。"""
        from harness.engine.tool_registry import ToolRegistry

        try:
            tool_registry = ctx.services.get(ToolRegistry)
            tool_registry.unregister_all(self.plugin_id)
        except Exception:
            pass
        ctx.logger.info("todo_write 工具已注销")
