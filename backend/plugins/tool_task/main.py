"""task 工具插件 —— 派生上下文干净的子代理。

主代理用 `task` 把一个独立的子任务（例如"读一堆文件后给结论""抓取并汇总
若干网页"）交给子代理去跑。子代理在独立会话中执行，**只把最终摘要回传**，
因此主上下文不会被大量中间探索过程污染。
"""

from __future__ import annotations

import logging
from typing import Any

from harness.kernel.context import PluginContext
from harness.kernel.contracts.base import BasePlugin, PluginManifest
from harness.kernel.contracts.tool import ToolPlugin
from harness.kernel.services import ServiceRegistry
from harness.modules.subagent.service import SubagentService

logger = logging.getLogger("harness.tools.task")

# 回传摘要的长度上限（超长部分交由 offload 钩子处理）
_MAX_SUMMARY_CHARS = 4000


class TaskTool(ToolPlugin):
    """子代理委派工具。"""

    def __init__(self, services: ServiceRegistry) -> None:
        self._services = services

    @property
    def tool_name(self) -> str:
        return "task"

    @property
    def risk_level(self) -> str:
        """子代理内会再次走权限/确认，此处按 write 处理。"""
        return "write"

    @property
    def description(self) -> str:
        return (
            "派生一个上下文干净的子代理，在**独立会话**中完成一个自包含的子任务，"
            "并只把最终结论摘要返回给你。\n"
            "何时使用：子任务需要大量中间步骤/读取多份材料，而你只关心最终结论；"
            "或想把探索过程与主对话隔离，避免上下文膨胀。\n"
            "用法：prompt 里写清子任务的目标与期望产出；"
            "可用 allow_tools 限定子代理可用的工具名（不传则用除 task 外的全部工具）。\n"
            "注意：子代理看不到主对话历史，prompt 必须自包含。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "子任务的完整描述（自包含，含目标与期望产出）",
                },
                "allow_tools": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "可选，限定子代理可用的工具名列表",
                },
                "max_iterations": {
                    "type": "integer",
                    "description": "子代理最大工具迭代数，默认 6",
                },
            },
            "required": ["prompt"],
        }

    async def execute(self, args: dict[str, Any]) -> str:
        prompt = str(args.get("prompt") or "").strip()
        if not prompt:
            return "错误: 缺少 prompt 参数"

        if not self._services.has(SubagentService):
            return "错误: 子代理服务不可用（subagent 插件未激活）"
        service: SubagentService = self._services.get(SubagentService)

        allow_tools = args.get("allow_tools")
        if allow_tools is not None and not isinstance(allow_tools, list):
            allow_tools = None
        try:
            max_iterations = int(args.get("max_iterations") or 6)
        except (TypeError, ValueError):
            max_iterations = 6

        result = await service.run_task(
            prompt,
            allow_tools=allow_tools,
            max_iterations=max_iterations,
        )
        if result.error:
            return f"子代理执行失败: {result.error}"

        content = result.content or "（子代理未返回内容）"
        if len(content) > _MAX_SUMMARY_CHARS:
            content = content[:_MAX_SUMMARY_CHARS] + " …（已截断）"
        return f"[子代理已完成] 迭代 {result.iterations} 次，调用工具 {result.tool_calls} 次。结论如下：\n{content}"


class TaskPlugin(BasePlugin):
    """task 工具插件。"""

    manifest: PluginManifest
    _ctx: PluginContext | None = None

    def __init__(self) -> None:
        self._ctx = None

    async def activate(self, ctx: PluginContext) -> None:
        """激活：注册 task 工具。"""
        self._ctx = ctx

        from harness.engine.tool_registry import ToolRegistry

        if not ctx.services.has(ToolRegistry):
            ctx.services.register(ToolRegistry, ToolRegistry(), owner=self.plugin_id)
        tool_registry = ctx.services.get(ToolRegistry)

        tool_registry.register(TaskTool(ctx.services), owner=self.plugin_id)
        ctx.logger.info("task 工具已注册")

    async def deactivate(self, ctx: PluginContext) -> None:
        """停用：注销工具。"""
        from harness.engine.tool_registry import ToolRegistry

        try:
            tool_registry = ctx.services.get(ToolRegistry)
            tool_registry.unregister_all(self.plugin_id)
        except Exception:
            pass
        ctx.logger.info("task 工具已注销")
