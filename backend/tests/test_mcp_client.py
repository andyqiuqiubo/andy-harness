"""MCP 客户端测试。

通过启动 `tests/mcp_echo_server.py` 作为真实 MCP server 子进程，
验证 stdio 传输、协议握手、tools/list、tools/call 全链路。
"""

from __future__ import annotations

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))

from harness.modules.mcp_client.service import (  # noqa: E402
    MCPClientService,
    MCPServerConfig,
    parse_server_config,
)

ECHO_SERVER = os.path.join(os.path.dirname(__file__), "mcp_echo_server.py")
PYTHON = sys.executable


@pytest.fixture
def echo_config() -> MCPServerConfig:
    return MCPServerConfig(
        name="echo",
        command=PYTHON,
        args=[ECHO_SERVER],
    )


async def test_connect_and_list_tools(echo_config: MCPServerConfig) -> None:
    """连接 echo server 能列出两个工具。"""
    conn = await asyncio.wait_for(_connect(echo_config), timeout=20)
    try:
        names = {t.name for t in conn.tools}
        assert {"echo", "add"} <= names
    finally:
        await conn.disconnect()


async def _connect(cfg: MCPServerConfig):
    conn = __import__("harness.modules.mcp_client.service", fromlist=["MCPServerConnection"]).MCPServerConnection(cfg)
    await conn.connect()
    return conn


async def test_reconnect_after_disconnect(echo_config: MCPServerConfig) -> None:
    """断开后可对同一连接对象重连（回归：_closed 未复位会导致读取循环立即退出）。"""
    conn = await _connect(echo_config)
    try:
        assert {t.name for t in conn.tools} >= {"echo", "add"}
        await conn.disconnect()
        tools = await asyncio.wait_for(conn.connect(), timeout=20)
        assert {t.name for t in tools} >= {"echo", "add"}
        out = await conn.call_tool("add", {"a": 4, "b": 6})
        assert "10" in out
    finally:
        await conn.disconnect()


async def test_call_echo(echo_config: MCPServerConfig) -> None:
    """调用 echo 工具原样回显参数。"""
    conn = await _connect(echo_config)
    try:
        out = await conn.call_tool("echo", {"text": "你好", "n": 1})
        assert "你好" in out
    finally:
        await conn.disconnect()


async def test_call_add(echo_config: MCPServerConfig) -> None:
    """调用 add 工具做加法。"""
    conn = await _connect(echo_config)
    try:
        out = await conn.call_tool("add", {"a": 2, "b": 3})
        assert "5" in out
    finally:
        await conn.disconnect()


async def test_error_tool_returns_message(echo_config: MCPServerConfig) -> None:
    """调用不存在的工具返回错误信息而非抛异常。"""
    conn = await _connect(echo_config)
    try:
        out = await conn.call_tool("nope", {})
        assert "error" in out.lower() or "unknown" in out.lower()
    finally:
        await conn.disconnect()


async def test_missing_command_raises() -> None:
    """command 不存在时连接抛错。"""
    cfg = MCPServerConfig(name="bad", command="this_command_does_not_exist_xyz")
    with pytest.raises(RuntimeError):
        await _connect(cfg)


def test_parse_server_config_args_string() -> None:
    """args 支持字符串（按 shell 规则拆分）。"""
    cfg = parse_server_config({"name": "x", "command": "python", "args": "-u server.py --flag"})
    assert cfg.args == ["-u", "server.py", "--flag"]


def test_parse_server_config_missing_command() -> None:
    """缺少 command 时抛 ValueError。"""
    with pytest.raises(ValueError):
        parse_server_config({"name": "x"})


async def test_service_connect_all(tmp_path) -> None:
    """MCPClientService.connect_all 经配置文件连接并汇总工具。"""
    config = {
        "mcpServers": {
            "echo": {"command": PYTHON, "args": [ECHO_SERVER]},
        }
    }
    cfg_file = tmp_path / "mcp.json"
    cfg_file.write_text(__import__("json").dumps(config), encoding="utf-8")
    svc = MCPClientService(config_paths=(str(cfg_file),))
    specs = await svc.connect_all()
    try:
        assert "echo" in specs
        assert {t.name for t in svc.list_tools()} >= {"echo", "add"}
    finally:
        await svc.disconnect_all()


async def test_service_missing_config_no_error() -> None:
    """配置文件不存在时不抛错，返回空配置。"""
    svc = MCPClientService(config_paths=("/nonexistent/path/mcp.json",))
    configs = svc.load_config()
    assert configs == {}
