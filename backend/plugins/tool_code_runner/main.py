"""tool-code-runner 插件 —— 代码执行工具。

执行 Python / Shell 代码，返回 stdout/stderr/exit_code。
通过 ToolRegistry 注册，面向 SandboxService 编程，不感知后端实现。
"""

from __future__ import annotations

import logging
from typing import Any

from harness.kernel.context import PluginContext
from harness.kernel.contracts.base import BasePlugin, PluginManifest
from harness.modules.sandbox_manager.service import SandboxService, SandboxServiceImpl

logger = logging.getLogger("harness.tool.code_runner")


class CodeRunnerTool:
    """代码执行工具。"""

    @property
    def tool_name(self) -> str:
        """工具名称。"""
        return "code_runner"

    @property
    def description(self) -> str:
        """工具描述。"""
        return "执行 Python 或 Shell 代码。参数: code (代码内容), language (python/shell)"

    @property
    def parameters_schema(self) -> dict[str, Any]:
        """参数 JSON Schema。"""
        return {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": "要执行的代码",
                },
                "language": {
                    "type": "string",
                    "enum": ["python", "shell"],
                    "default": "python",
                    "description": "编程语言",
                },
            },
            "required": ["code"],
        }

    def __init__(self, sandbox: SandboxService) -> None:
        self._sandbox = sandbox

    async def execute(self, args: dict[str, Any]) -> str:
        """执行代码。"""
        code = args.get("code", "")
        language = args.get("language", "python")
        session_id = args.get("session_id", "default")

        if not code:
            return "错误: 未提供代码"

        result = await self._sandbox.execute(
            code=code,
            language=language,
            session_id=session_id,
        )

        parts: list[str] = []
        if result.blocked:
            parts.append(f"[被拦截] {result.blocked_reason}")
        if result.timed_out:
            parts.append("[超时] 进程被终止")
        if result.stdout:
            parts.append(f"[stdout]\n{result.stdout}")
        if result.stderr:
            parts.append(f"[stderr]\n{result.stderr}")
        parts.append(f"[exit_code] {result.exit_code}")
        parts.append(f"[耗时] {result.duration_ms}ms")

        if result.files_created:
            parts.append(f"[生成文件] {', '.join(result.files_created)}")

        return "\n".join(parts) if parts else "[无输出]"


class CodeRunnerPlugin(BasePlugin):
    """代码执行插件。"""

    manifest: PluginManifest
    _ctx: PluginContext | None = None
    _sandbox: SandboxService | None = None
    _tool: CodeRunnerTool | None = None

    def __init__(self) -> None:
        self._ctx = None
        self._sandbox = None
        self._tool = None

    async def activate(self, ctx: PluginContext) -> None:
        """激活：创建沙箱服务并注册工具。"""
        self._ctx = ctx

        # 从插件配置读取 timeout 和 max_output
        timeout = ctx.config.get("timeout", 30)
        max_output = ctx.config.get("max_output", 10000)

        # 注册 SandboxService（传入 EventBus 和配置参数）
        self._sandbox = SandboxServiceImpl(
            events=ctx.events,
            timeout=timeout,
            max_output=max_output,
        )
        ctx.services.register(SandboxService, self._sandbox, owner=self.plugin_id)

        # 获取 ToolRegistry 并注册工具
        from harness.engine.tool_registry import ToolRegistry

        if not ctx.services.has(ToolRegistry):
            ctx.services.register(ToolRegistry, ToolRegistry(), owner=self.plugin_id)
        tool_registry = ctx.services.get(ToolRegistry)

        self._tool = CodeRunnerTool(self._sandbox)
        tool_registry.register(self._tool, owner=self.plugin_id)

        ctx.logger.info("tool-code-runner 已激活")

    async def deactivate(self, ctx: PluginContext) -> None:
        """停用。"""
        from harness.engine.tool_registry import ToolRegistry

        try:
            tool_registry = ctx.services.get(ToolRegistry)
            tool_registry.unregister("code_runner")
        except Exception:
            pass
        self._sandbox = None
        self._tool = None
        ctx.logger.info("tool-code-runner 已停用")
