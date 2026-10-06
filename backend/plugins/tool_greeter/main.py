"""tool_greeter

由 DevKit 脚手架生成：一个最小可用的 ToolPlugin 插件包。
"""

from __future__ import annotations

from typing import Any

from harness.kernel.context import PluginContext
from harness.kernel.contracts.base import BasePlugin, PluginManifest
from harness.kernel.contracts.tool import ToolPlugin


class GreeterToolPlugin(ToolPlugin):
    @property
    def tool_name(self) -> str:
        return "tool_greeter"

    @property
    def description(self) -> str:
        return "tool_greeter：把这里的描述换成你的工具说明（Agent 依据它决定何时调用）。"

    @property
    def risk_level(self) -> str:
        return "read"  # read / write / dangerous，权限层按此管控

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "输入文本"},
            },
            "required": ["text"],
        }

    async def execute(self, args: dict[str, Any]) -> str:
        text = str(args.get("text", ""))
        return f"echo: {text}"


class GreeterPlugin(BasePlugin):
    """插件入口：把工具注册进 ToolRegistry。"""

    manifest: PluginManifest

    async def activate(self, ctx: PluginContext) -> None:
        from harness.engine.tool_registry import ToolRegistry

        if not ctx.services.has(ToolRegistry):
            ctx.services.register(ToolRegistry, ToolRegistry(), owner=self.plugin_id)
        ctx.services.get(ToolRegistry).register(GreeterToolPlugin(), owner=self.plugin_id)
        ctx.logger.info("tool_greeter 已激活")

    async def deactivate(self, ctx: PluginContext) -> None:
        ctx.logger.info("tool_greeter 已停用")
