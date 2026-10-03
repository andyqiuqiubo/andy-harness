"""MCP SSE（HTTP + Server-Sent Events）传输测试。

通过启动 `tests/mcp_sse_echo_server.py`（纯标准库实现的 SSE MCP server）
验证：SSE 握手、tools/list、tools/call、配置解析（含 disabled / 类型推断）。
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator

import pytest

from harness.modules.mcp_client.service import (
    MCPClientService,
    MCPServerConfig,
    MCPSSEConnection,
    parse_server_config,
)

SSE_SERVER = os.path.join(os.path.dirname(__file__), "mcp_sse_echo_server.py")
PYTHON = sys.executable


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = int(s.getsockname()[1])
    s.close()
    return port


@pytest.fixture(scope="module")
def sse_url() -> Iterator[str]:
    """启动本地 SSE echo server，返回其 /sse 地址。"""
    port = _free_port()
    proc = subprocess.Popen(  # noqa: S603
        [PYTHON, SSE_SERVER, str(port)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    deadline = time.time() + 15
    ready = False
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                ready = True
                break
        except OSError:
            if proc.poll() is not None:
                break
            time.sleep(0.2)
    if not ready:
        proc.terminate()
        out = proc.stdout.read().decode("utf-8", "replace") if proc.stdout else ""
        pytest.fail(f"SSE echo server 未能启动: {out}")
    yield f"http://127.0.0.1:{port}/sse"
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except Exception:  # noqa: BLE001
        proc.kill()


class TestParseSSEConfig:
    """配置解析：SSE / disabled / 类型推断。"""

    def test_parse_sse_with_disabled(self) -> None:
        cfg = parse_server_config(
            {
                "name": "youth-mcp",
                "url": "http://58.214.245.86:18081/sse/ads/ce16b709",
                "type": "sse",
                "description": "WorkBuddy 广告数据 MCP - 用户 zhouj",
                "disabled": False,
            }
        )
        assert cfg.type == "sse"
        assert cfg.enabled is True
        assert cfg.url == "http://58.214.245.86:18081/sse/ads/ce16b709"
        assert cfg.description.startswith("WorkBuddy 广告数据")

    def test_disabled_true_maps_to_disabled(self) -> None:
        cfg = parse_server_config({"name": "x", "url": "http://h/sse", "type": "sse", "disabled": True})
        assert cfg.enabled is False

    def test_type_inferred_from_url(self) -> None:
        cfg = parse_server_config({"name": "x", "url": "http://h/sse"})
        assert cfg.type == "sse"

    def test_type_stdio_by_default(self) -> None:
        cfg = parse_server_config({"name": "x", "command": "python", "args": ["a.py"]})
        assert cfg.type == "stdio"
        assert cfg.command == "python"

    def test_missing_url_or_command_raises(self) -> None:
        with pytest.raises(ValueError):
            parse_server_config({"name": "x", "type": "sse"})
        with pytest.raises(ValueError):
            parse_server_config({"name": "x", "type": "stdio"})


class TestSSEConnection:
    """SSE 连接：握手 / 列表 / 调用 / 重连 / 断连失败。"""

    async def test_connect_list_and_call(self, sse_url: str) -> None:
        conn = MCPSSEConnection(MCPServerConfig(name="sse-echo", type="sse", url=sse_url))
        tools = await asyncio.wait_for(conn.connect(), timeout=25)
        try:
            assert {t.name for t in tools} >= {"echo", "add"}
            assert "7" in await conn.call_tool("add", {"a": 3, "b": 4})
            assert "你好" in await conn.call_tool("echo", {"text": "你好"})
            err = await conn.call_tool("nope", {})
            assert "unknown" in err.lower()
        finally:
            await conn.disconnect()

    async def test_reconnect_after_disconnect(self, sse_url: str) -> None:
        conn = MCPSSEConnection(MCPServerConfig(name="sse-echo", type="sse", url=sse_url))
        await asyncio.wait_for(conn.connect(), timeout=25)
        await conn.disconnect()
        tools = await asyncio.wait_for(conn.connect(), timeout=25)
        try:
            assert {t.name for t in tools} >= {"echo", "add"}
        finally:
            await conn.disconnect()

    async def test_bad_url_raises_fast(self) -> None:
        # 端口无人监听 → 应在超时内抛错，而不是挂死
        conn = MCPSSEConnection(MCPServerConfig(name="dead", type="sse", url="http://127.0.0.1:1/sse"))
        with pytest.raises(Exception):  # noqa: B017,PT011
            await asyncio.wait_for(conn.connect(), timeout=25)


class TestSSEService:
    """MCPClientService 走配置文件连接 SSE server。"""

    async def test_connect_all_via_config_file(self, sse_url: str, tmp_path) -> None:
        cfg_file = tmp_path / "mcp.json"
        cfg_file.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "sse-echo": {
                            "type": "sse",
                            "url": sse_url,
                            "description": "本地 SSE 测试",
                            "disabled": False,
                        },
                        "off": {
                            "type": "sse",
                            "url": sse_url,
                            "disabled": True,
                        },
                    }
                }
            ),
            encoding="utf-8",
        )
        svc = MCPClientService(config_paths=(str(cfg_file),))
        specs = await svc.connect_all()
        try:
            # disabled 的 server 不会被连接
            assert "sse-echo" in specs
            assert "off" not in specs
            assert {t.name for t in svc.list_tools()} >= {"echo", "add"}
        finally:
            await svc.disconnect_all()
