"""tool-code-runner 插件测试。"""

import pytest

from harness.engine.tool_registry import ToolRegistry
from harness.kernel.context import PluginContext
from harness.kernel.eventbus import EventBus
from harness.kernel.hooks import HookManager
from harness.kernel.services import ServiceRegistry


@pytest.fixture
def ctx() -> PluginContext:
    """创建带 ToolRegistry 的 PluginContext。"""
    services = ServiceRegistry()
    services.register(ToolRegistry, ToolRegistry(), owner="test")
    return PluginContext(
        "tool_code_runner",
        events=EventBus(),
        services=services,
        hooks=HookManager(),
    )


class TestCodeRunnerPlugin:
    """CodeRunnerPlugin 测试。"""

    @pytest.mark.asyncio
    async def test_activate_registers_tool(self, ctx: PluginContext) -> None:
        """激活后工具注册到 ToolRegistry。"""
        from plugins.tool_code_runner.main import CodeRunnerPlugin

        plugin = CodeRunnerPlugin()
        plugin.manifest = type("M", (), {"id": "tool_code_runner", "core": False})()  # type: ignore[attr-defined]

        await plugin.activate(ctx)

        tool_registry = ctx.services.get(ToolRegistry)
        assert tool_registry.has("code_runner")

        await plugin.deactivate(ctx)

    @pytest.mark.asyncio
    async def test_execute_python(self, ctx: PluginContext) -> None:
        """执行 Python 代码。"""
        from plugins.tool_code_runner.main import CodeRunnerPlugin

        plugin = CodeRunnerPlugin()
        plugin.manifest = type("M", (), {"id": "tool_code_runner", "core": False})()  # type: ignore[attr-defined]

        await plugin.activate(ctx)

        tool_registry = ctx.services.get(ToolRegistry)
        tool = tool_registry.get("code_runner")

        result = await tool.execute({
            "code": "print(2 + 3)",
            "language": "python",
            "session_id": "test-session",
        })

        assert "5" in result
        assert "exit_code" in result

        await plugin.deactivate(ctx)

    @pytest.mark.asyncio
    async def test_execute_blocked_code(self, ctx: PluginContext) -> None:
        """危险代码被拦截。"""
        from plugins.tool_code_runner.main import CodeRunnerPlugin

        plugin = CodeRunnerPlugin()
        plugin.manifest = type("M", (), {"id": "tool_code_runner", "core": False})()  # type: ignore[attr-defined]

        await plugin.activate(ctx)

        tool_registry = ctx.services.get(ToolRegistry)
        tool = tool_registry.get("code_runner")

        result = await tool.execute({
            "code": "rm -rf /",
            "language": "shell",
            "session_id": "test-blocked",
        })

        assert "被拦截" in result

        await plugin.deactivate(ctx)

    @pytest.mark.asyncio
    async def test_deactivate_unregisters_tool(self, ctx: PluginContext) -> None:
        """停用后工具注销。"""
        from plugins.tool_code_runner.main import CodeRunnerPlugin

        plugin = CodeRunnerPlugin()
        plugin.manifest = type("M", (), {"id": "tool_code_runner", "core": False})()  # type: ignore[attr-defined]

        await plugin.activate(ctx)
        tool_registry = ctx.services.get(ToolRegistry)
        assert tool_registry.has("code_runner")

        await plugin.deactivate(ctx)
        assert not tool_registry.has("code_runner")
