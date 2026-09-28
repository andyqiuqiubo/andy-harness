"""长期记忆工具插件 —— memory_save / memory_search。

让模型能够主动保存"值得记住"的跨会话事实（用户偏好、项目约定、反复用到的
信息），并在需要时检索。配合 AgentLoop 的长期记忆注入，实现跨会话记忆。
"""

from __future__ import annotations

import logging
from typing import Any

from harness.kernel.context import PluginContext
from harness.kernel.contracts.base import BasePlugin, PluginManifest
from harness.kernel.contracts.tool import ToolPlugin
from harness.kernel.services import ServiceRegistry
from harness.modules.memory_manager.service import (
    SCOPE_GLOBAL,
    SCOPE_SESSION,
    MemoryService,
)

logger = logging.getLogger("harness.tools.memory")


class MemorySaveTool(ToolPlugin):
    """保存长期记忆。"""

    def __init__(self, services: ServiceRegistry) -> None:
        self._services = services

    @property
    def tool_name(self) -> str:
        return "memory_save"

    @property
    def risk_level(self) -> str:
        return "write"

    @property
    def needs_session(self) -> bool:
        """session 作用域的记忆需要当前会话 id。"""
        return True

    @property
    def description(self) -> str:
        return (
            "把值得跨会话记住的事实保存为长期记忆（用户偏好、项目约定、"
            "反复需要的信息等）。\n"
            "何时使用：用户明确说「记住…」，或你发现某个偏好/事实以后还会用到时。\n"
            "scope=global 表示对所有会话可见（默认）；scope=session 仅当前会话可见。\n"
            "同一个 key 再次保存会覆盖旧值（幂等），因此请用稳定的 key。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "key": {
                    "type": "string",
                    "description": "记忆标识（稳定、简短，如 user.language / project.stack）",
                },
                "value": {
                    "type": "string",
                    "description": "记忆内容",
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "可选标签，便于检索",
                },
                "scope": {
                    "type": "string",
                    "enum": [SCOPE_GLOBAL, SCOPE_SESSION],
                    "description": "作用域，默认 global（跨会话）",
                },
            },
            "required": ["key", "value"],
        }

    async def execute(self, args: dict[str, Any]) -> str:
        key = str(args.get("key") or "").strip()
        value = args.get("value")
        if not key:
            return "错误: 缺少 key 参数"
        if value is None or str(value).strip() == "":
            return "错误: 缺少或空的 value 参数"

        if not self._services.has(MemoryService):
            return "错误: 长期记忆服务不可用（memory_manager 插件未激活）"
        service: MemoryService = self._services.get(MemoryService)

        scope = str(args.get("scope") or SCOPE_GLOBAL)
        session_id = str(args.get("session_id") or "")
        if scope == SCOPE_SESSION and not session_id:
            return "错误: session 作用域需要当前会话（未注入 session_id）"

        record = service.save(
            key=key,
            value=str(value),
            scope=scope,
            session_id=session_id,
            tags=args.get("tags"),
        )
        return (
            f"已记住（{record.scope}）: {record.key} = {record.value}"
            f"（id={record.id}）"
        )


class MemorySearchTool(ToolPlugin):
    """检索长期记忆。"""

    def __init__(self, services: ServiceRegistry) -> None:
        self._services = services

    @property
    def tool_name(self) -> str:
        return "memory_search"

    @property
    def risk_level(self) -> str:
        return "read"

    @property
    def description(self) -> str:
        return (
            "检索此前保存的长期记忆。\n"
            "何时使用：需要回忆用户偏好、项目约定或以前保存过的事实；"
            "不确定是否记得某事时也可先搜一下。\n"
            "query 留空则返回最近的记忆。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "关键词（匹配 key / value / tags），留空返回最近记忆",
                },
                "limit": {
                    "type": "integer",
                    "description": "返回条数上限，默认 10",
                },
            },
        }

    async def execute(self, args: dict[str, Any]) -> str:
        if not self._services.has(MemoryService):
            return "错误: 长期记忆服务不可用（memory_manager 插件未激活）"
        service: MemoryService = self._services.get(MemoryService)

        query = str(args.get("query") or "")
        try:
            limit = int(args.get("limit") or 10)
        except (TypeError, ValueError):
            limit = 10
        results = service.search(query, limit=max(1, min(limit, 50)))
        if not results:
            return "未找到相关记忆。"

        lines = [f"找到 {len(results)} 条记忆："]
        for m in results:
            tags = f" [{'/'.join(m.tags)}]" if m.tags else ""
            scope = f"({m.scope})"
            lines.append(f"- {scope} {m.key} = {m.value}{tags}")
        return "\n".join(lines)


class MemoryToolsPlugin(BasePlugin):
    """长期记忆工具插件。"""

    manifest: PluginManifest
    _ctx: PluginContext | None = None

    def __init__(self) -> None:
        self._ctx = None

    async def activate(self, ctx: PluginContext) -> None:
        """激活：把两个工具注册进 ToolRegistry。"""
        self._ctx = ctx

        from harness.engine.tool_registry import ToolRegistry

        if not ctx.services.has(ToolRegistry):
            ctx.services.register(ToolRegistry, ToolRegistry(), owner=self.plugin_id)
        tool_registry = ctx.services.get(ToolRegistry)

        tool_registry.register(MemorySaveTool(ctx.services), owner=self.plugin_id)
        tool_registry.register(MemorySearchTool(ctx.services), owner=self.plugin_id)
        ctx.logger.info("memory_save / memory_search 工具已注册")

    async def deactivate(self, ctx: PluginContext) -> None:
        """停用：注销工具。"""
        from harness.engine.tool_registry import ToolRegistry

        try:
            tool_registry = ctx.services.get(ToolRegistry)
            tool_registry.unregister_all(self.plugin_id)
        except Exception:
            pass
        ctx.logger.info("记忆工具已注销")
