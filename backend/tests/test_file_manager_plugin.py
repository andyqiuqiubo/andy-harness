"""tool_file_manager 插件测试 —— 重点覆盖 SEC-22（默认根目录收敛到受管工作区）。"""

import os

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
        "tool_file_manager",
        events=EventBus(),
        services=services,
        hooks=HookManager(),
    )


def _make_plugin():
    """构造一个带 manifest 的 FileManagerPlugin（供 activate 用）。"""
    from marketplace.tool_file_manager.main import FileManagerPlugin

    plugin = FileManagerPlugin()
    plugin.manifest = type("M", (), {"id": "tool_file_manager", "core": False})()  # type: ignore[attr-defined]
    return plugin


class TestDefaultBaseDir:
    """SEC-22：默认受控根目录不再回退到 Path.cwd()。"""

    def test_default_base_dir_is_managed_workspace(self) -> None:
        """默认根落在用户级受管工作区 ~/.andy-harness/workspaces/files。"""
        from marketplace.tool_file_manager.main import _default_base_dir

        base = _default_base_dir()
        norm = str(base).replace("\\", "/").lower()
        assert ".andy-harness" in norm
        assert norm.endswith("/workspaces/files")

    def test_default_base_dir_not_cwd(self, tmp_path, monkeypatch) -> None:
        """即使 cwd 是某个源码树目录，默认根也不等于 cwd。"""
        from marketplace.tool_file_manager.main import _default_base_dir

        monkeypatch.chdir(tmp_path)
        assert _default_base_dir().resolve() != tmp_path.resolve()

    @pytest.mark.asyncio
    async def test_activate_empty_config_uses_managed_base(self, ctx: PluginContext) -> None:
        """配置留空时，注册的工具其 base 是受管工作区，而非进程 cwd。"""
        plugin = _make_plugin()
        # ctx.config 默认空 → base_dir 为空字符串
        await plugin.activate(ctx)
        try:
            from marketplace.tool_file_manager.main import _default_base_dir

            tool = ctx.services.get(ToolRegistry).get("file_manager")
            assert tool is not None
            # 关键断言：默认根是受管工作区，绝不落在进程 cwd（源码树/密钥所在）
            assert tool._base == _default_base_dir().resolve()
            assert tool._base != os.getcwd().encode().decode()
        finally:
            await plugin.deactivate(ctx)

    @pytest.mark.asyncio
    async def test_activate_explicit_base_dir_respected(self, ctx: PluginContext, tmp_path) -> None:
        """显式配置 base_dir 时仍按配置收敛（受控根可自定义）。"""
        target = tmp_path / "custom_ws"
        ctx.config.set("base_dir", str(target))
        plugin = _make_plugin()
        await plugin.activate(ctx)
        try:
            tool = ctx.services.get(ToolRegistry).get("file_manager")
            assert tool._base == target.resolve()
        finally:
            await plugin.deactivate(ctx)
