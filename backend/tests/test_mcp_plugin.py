"""MCP 客户端插件集成测试。

验证插件能把 MCP server 的工具注册进 ToolRegistry，且注册后的工具可被执行。
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from harness.engine.tool_registry import ToolRegistry  # noqa: E402
from harness.kernel.context import EventBus, HookManager, PluginContext  # noqa: E402
from harness.kernel.contracts.base import PluginManifest  # noqa: E402
from harness.kernel.services import ServiceRegistry  # noqa: E402
from harness.modules.mcp_client.service import MCPClientService  # noqa: E402
from plugins.mcp_client.main import MCPClientPlugin  # noqa: E402


def _manifest() -> PluginManifest:
    return PluginManifest(
        id="mcp_client",
        name="MCP Client",
        version="0.1.0",
        type="service",
        entry="plugins.mcp_client.main:MCPClientPlugin",
    )


ECHO_SERVER = os.path.join(os.path.dirname(__file__), "mcp_echo_server.py")
PYTHON = sys.executable


def _make_ctx() -> PluginContext:
    return PluginContext(
        plugin_id="mcp_client",
        events=EventBus(),
        services=ServiceRegistry(),
        hooks=HookManager(),
    )


async def test_plugin_registers_mcp_tools(tmp_path) -> None:
    """插件激活后，MCP 工具出现在 ToolRegistry。"""
    cfg_file = tmp_path / "mcp.json"
    cfg_file.write_text(
        json.dumps({"mcpServers": {"echo": {"command": PYTHON, "args": [ECHO_SERVER]}}}),
        encoding="utf-8",
    )
    services = ServiceRegistry()
    services.register(ToolRegistry, ToolRegistry(), owner="test")
    services.register(MCPClientService, MCPClientService(config_paths=(str(cfg_file),)), owner="test")

    ctx = PluginContext(
        plugin_id="mcp_client",
        events=EventBus(),
        services=services,
        hooks=HookManager(),
    )
    plugin = MCPClientPlugin()
    plugin.manifest = _manifest()
    await plugin.activate(ctx)
    try:
        tr: ToolRegistry = services.get(ToolRegistry)
        names = {t["name"] for t in tr.list_tools()}
        assert "mcp__echo__echo" in names
        assert "mcp__echo__add" in names

        tool = tr.get("mcp__echo__echo")
        out = await tool.execute({"text": "集成测试"})
        assert "集成测试" in out
    finally:
        await plugin.deactivate(ctx)


async def test_plugin_deactivate_unregisters(tmp_path) -> None:
    """停用后 MCP 工具从 ToolRegistry 移除并断开连接。"""
    cfg_file = tmp_path / "mcp.json"
    cfg_file.write_text(
        json.dumps({"mcpServers": {"echo": {"command": PYTHON, "args": [ECHO_SERVER]}}}),
        encoding="utf-8",
    )
    services = ServiceRegistry()
    services.register(ToolRegistry, ToolRegistry(), owner="test")
    svc = MCPClientService(config_paths=(str(cfg_file),))
    services.register(MCPClientService, svc, owner="test")
    ctx = _make_ctx()
    ctx.services = services
    plugin = MCPClientPlugin()
    plugin.manifest = _manifest()
    await plugin.activate(ctx)
    assert "mcp__echo__echo" in {t["name"] for t in services.get(ToolRegistry).list_tools()}
    await plugin.deactivate(ctx)
    names = {t["name"] for t in services.get(ToolRegistry).list_tools()}
    assert "mcp__echo__echo" not in names
    assert not svc.is_connected("echo")
