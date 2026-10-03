"""MCP streamable-http（POST-only）传输回归测试。

真实远端（如 libgen `https://mcp.jmrp.io/libgen`）已从旧版 SSE 迁移到
**只支持 POST 的 streamable-http**：`GET <url>` 会返回 405。

回归保护：客户端原本强制先 GET 建流拿 `event: endpoint`，拿不到就抛
「MCP SSE 尚未获得 POST endpoint」。现在会在 GET 建流失败时
**退回直接 POST 模式**把配置 URL 当 JSON-RPC 端点，并解析 POST 响应体里
内联的 SSE 文本（`event: message` + `data: {...}`）。

这些用例用本地测试服务器模拟该形态，完全离线。
"""

from __future__ import annotations

import asyncio
import socket
import threading
import time
from collections.abc import Iterator
from http.server import ThreadingHTTPServer

import pytest

from harness.modules.mcp_client import service as mcp_service
from harness.modules.mcp_client.service import (
    MCPServerConfig,
    MCPSSEConnection,
    make_connection,
)
from tests.mcp_streamable_http_server import _LOCK, SEEN_HEADERS, Handler


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = int(s.getsockname()[1])
    s.close()
    return port


def _start(handler_cls, port: int) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer(("127.0.0.1", port), handler_cls)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


@pytest.fixture
def server_url() -> Iterator[str]:
    """在后台线程启动 streamable-http 测试服务器。"""
    port = _free_port()
    httpd = _start(Handler, port)
    with _LOCK:
        SEEN_HEADERS.clear()
    yield f"http://127.0.0.1:{port}/"
    httpd.shutdown()
    httpd.server_close()


class _HangingGetHandler(Handler):
    """GET 能建流但**永不发送 endpoint** —— 模拟 DeepWiki 等 streamable-http 服务。

    这类服务把 GET 仅用于服务端推送，请求一律走 POST，客户端若一直等 endpoint 就会失败。
    """

    def do_GET(self):  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        try:
            for _ in range(200):
                self.wfile.write(b": keepalive\n\n")  # 注释心跳，不是 endpoint 事件
                self.wfile.flush()
                time.sleep(0.1)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass


@pytest.fixture
def hanging_get_url(monkeypatch) -> Iterator[str]:
    """启动「GET 建流但不给 endpoint」的服务器，并把等待超时调到极短。"""
    monkeypatch.setattr(mcp_service, "_OPEN_ENDPOINT_TIMEOUT", 0.5)
    port = _free_port()
    httpd = _start(_HangingGetHandler, port)
    yield f"http://127.0.0.1:{port}/"
    httpd.shutdown()
    httpd.server_close()


def _run(coro):
    return asyncio.run(asyncio.wait_for(coro, timeout=30))


class TestStreamableHttpTransport:
    """GET 不被支持时，应自动退回直接 POST 并能完成握手与工具调用。"""

    def test_get_is_rejected_by_server(self, server_url: str) -> None:
        """前置条件：该 server 确实拒接 GET（与线上 libgen 行为一致）。"""
        import httpx

        resp = httpx.get(server_url, timeout=10)
        assert resp.status_code == 405
        assert resp.headers.get("allow", "").upper() == "POST"

    def test_connect_lists_tools(self, server_url: str) -> None:
        """GET 405 时仍能完成握手并拉到工具清单。"""
        cfg = MCPServerConfig(name="streamable", type="sse", url=server_url)
        conn = make_connection(cfg)

        tools = _run(conn.connect())
        names = {t.name for t in tools}

        assert names == {"echo", "add"}
        # 确认走的是「直接 POST」回退路径
        assert isinstance(conn, MCPSSEConnection)
        assert conn._direct_post is True
        assert conn._post_url == server_url

    def test_call_tool_round_trip(self, server_url: str) -> None:
        """工具调用能拿到远端真实返回值。"""
        cfg = MCPServerConfig(name="streamable", type="sse", url=server_url)
        conn = make_connection(cfg)

        async def scenario() -> tuple[str, str]:
            await conn.connect()
            echo = await conn.call_tool("echo", {"text": "hi"})  # type: ignore[attr-defined]
            added = await conn.call_tool("add", {"a": 2, "b": 3})  # type: ignore[attr-defined]
            await conn.disconnect()
            return str(echo), str(added)

        echo, added = _run(scenario())
        assert echo == "echo: hi"
        assert added == "sum: 5"

    def test_session_id_is_propagated(self, server_url: str) -> None:
        """initialize 下发的 Mcp-Session-Id 应在后续请求中回传。"""
        cfg = MCPServerConfig(name="streamable", type="sse", url=server_url)
        conn = make_connection(cfg)

        async def scenario() -> None:
            await conn.connect()
            await conn.disconnect()

        _run(scenario())
        with _LOCK:
            seen = list(SEEN_HEADERS)

        # tools/list 必须带上会话 ID，否则部分 streamable-http 服务会拒绝
        tools_calls = [h for h in seen if h["method"] == "tools/list"]
        assert tools_calls, f"未观察到 tools/list 请求: {seen}"
        assert tools_calls[0]["mcp-session-id"] == "test-session-1"

    def test_get_stream_without_endpoint_falls_back(self, hanging_get_url: str) -> None:
        """GET 建流成功但始终没有 endpoint 时，也应退回直接 POST。

        线上对应 DeepWiki（https://mcp.deepwiki.com/mcp）：GET 返回 200 事件流
        但从不下发 endpoint，原实现会空等 20s 后直接失败。
        """
        cfg = MCPServerConfig(name="hanging", type="streamable-http", url=hanging_get_url)
        conn = make_connection(cfg)

        tools = _run(conn.connect())
        names = {t.name for t in tools}

        assert names == {"echo", "add"}
        assert conn._direct_post is True
