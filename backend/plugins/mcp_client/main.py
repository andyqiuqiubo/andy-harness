"""MCP 客户端插件。

激活时连接 `mcp.json` 中配置的 MCP server，把每个远端工具包装成
`ToolPlugin` 注册进 ToolRegistry，使 AI 可像调用本地工具一样调用它们。
"""

from __future__ import annotations

import logging
from typing import Any

from harness.kernel.context import PluginContext
from harness.kernel.contracts.base import BasePlugin, PluginManifest
from harness.kernel.contracts.tool import ToolPlugin
from harness.kernel.services import ServiceRegistry
from harness.modules.mcp_client.service import (
    MCPClientService,
    MCPToolSpec,
)

logger = logging.getLogger("harness.plugins.mcp_client")

# 名字以这些前缀开头的远端工具视为只读查询（大多数 MCP 工具是查询类），
# 从而不触发人工确认；其余保持保守的 write。
_READ_PREFIXES = (
    "get",
    "query",
    "list",
    "search",
    "read",
    "fetch",
    "describe",
    "find",
    "show",
    "count",
    "stat",
    "lookup",
)


class MCPTool(ToolPlugin):
    """把单个 MCP 远端工具包装成本地 ToolPlugin。"""

    def __init__(
        self,
        services: ServiceRegistry,
        spec: MCPToolSpec,
        service: MCPClientService,
        server_description: str = "",
    ) -> None:
        self._services = services
        self._spec = spec
        self._mcp = service
        self._server_description = server_description

    @property
    def tool_name(self) -> str:
        # 用 server__tool 命名空间避免与本地工具冲突
        return f"mcp__{self._spec.server}__{self._spec.name}"

    @property
    def risk_level(self) -> str:
        # MCP 工具行为不可先验：仅对名字明显是查询类的取 read，其余保守按 write
        name = (self._spec.name or "").lower()
        return "read" if name.startswith(_READ_PREFIXES) else "write"

    @property
    def description(self) -> str:
        # 头部带上 server 级说明（用户在 mcp.json 里写的中文能力描述 + 参数约定），
        # 远端自带描述通常只有一句英文，不足以让模型判断该不该用、参数怎么填。
        head = f"[MCP:{self._spec.server}]"
        if self._server_description:
            head = f"{head} {self._server_description}"
        return (
            f"{head}\n远端工具 {self._spec.name}（由 MCP server '{self._spec.server}' 提供）：{self._spec.description}"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return self._spec.input_schema

    async def execute(self, args: dict[str, Any]) -> str:
        try:
            return await self._mcp.call_tool(self._spec.server, self._spec.name, args or {})
        except Exception as e:  # noqa: BLE001
            return f"调用 MCP 工具失败: {e}"


class MCPClientPlugin(BasePlugin):
    """MCP 客户端插件。"""

    manifest: PluginManifest
    _ctx: PluginContext | None = None

    def __init__(self) -> None:
        self._ctx = None
        self._service: MCPClientService | None = None
        self._registered: list[str] = []

    async def activate(self, ctx: PluginContext) -> None:
        self._ctx = ctx
        if ctx.services.has(MCPClientService):
            self._service = ctx.services.get(MCPClientService)
        else:
            self._service = MCPClientService()
            ctx.services.register(MCPClientService, self._service, owner=self.plugin_id)

        tool_specs_by_server = await self._service.connect_all()
        total = 0
        from harness.engine.tool_registry import ToolRegistry

        if not ctx.services.has(ToolRegistry):
            ctx.services.register(ToolRegistry, ToolRegistry(), owner=self.plugin_id)
        tool_registry = ctx.services.get(ToolRegistry)

        server_descriptions = {name: cfg.description for name, cfg in self._service.get_configs().items()}
        for server, specs in tool_specs_by_server.items():
            server_desc = server_descriptions.get(server, "")
            for spec in specs:
                tool = MCPTool(ctx.services, spec, self._service, server_desc)
                tool_registry.register(tool, owner=self.plugin_id)
                self._registered.append(tool.tool_name)
                total += 1
        ctx.logger.info("MCP 客户端已注册 %d 个远端工具", total)

    async def deactivate(self, ctx: PluginContext) -> None:
        from harness.engine.tool_registry import ToolRegistry

        try:
            tool_registry = ctx.services.get(ToolRegistry)
            for name in self._registered:
                tool_registry.unregister(name)
        except Exception:  # noqa: BLE001
            pass
        self._registered.clear()
        if self._service is not None:
            await self._service.disconnect_all()
        ctx.logger.info("MCP 客户端已停用")
