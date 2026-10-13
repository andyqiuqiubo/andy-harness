"""出站 SSRF 护栏测试（SEC-11 / SEC-12）。

验证：
1. `check_url` 对云元数据 / 回环 / 内网 / 非 http(s) 协议一律拒绝；
2. `safe_fetch_text` **逐跳**校验重定向（首跳白名单 → 302 到元数据地址必须被拦）；
3. web_fetch 工具与 MCP SSE 传输已接入护栏。
"""

from __future__ import annotations

import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from harness.infra import net_guard


@pytest.fixture(autouse=True)
def _no_dns_dependence(monkeypatch: pytest.MonkeyPatch) -> None:
    """默认不放开私有地址（保持生产默认 fail-closed）。"""
    monkeypatch.delenv("HARNESS_ALLOW_PRIVATE_URL", raising=False)


class TestCheckUrl:
    FORBIDDEN = [
        "http://169.254.169.254/latest/meta-data/",  # 云元数据
        "http://127.0.0.1:8080/admin",  # 回环
        "http://localhost:8080/admin",
        "http://10.0.0.5/internal",
        "http://192.168.1.1/",
        "http://172.16.0.1/",
        "http://0.0.0.0/",
        "file:///etc/passwd",
        "ftp://internal/x",
        "gopher://internal/x",
    ]

    @pytest.mark.parametrize("url", FORBIDDEN)
    def test_forbidden(self, url: str) -> None:
        assert net_guard.check_url(url) is not None, f"应被拒绝: {url}"

    def test_public_host_allowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """公网域名（解析到公网 IP）放行。"""
        monkeypatch.setattr(
            net_guard.socket,
            "getaddrinfo",
            lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 443))],
        )
        assert net_guard.check_url("https://example.com/path") is None

    def test_loopback_allowed_when_opted_in(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """显式放开（HARNESS_ALLOW_PRIVATE_URL=1）后允许内网自建源。"""
        monkeypatch.setenv("HARNESS_ALLOW_PRIVATE_URL", "1")
        assert net_guard.check_url("http://127.0.0.1:9000/mcp") is None

    def test_dns_failure_is_fail_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """解析失败不得放行（DNS rebinding 场景下无从判断 → 拒绝）。"""

        def boom(*_a: object, **_k: object) -> None:
            raise OSError("dns down")

        monkeypatch.setattr(net_guard.socket, "getaddrinfo", boom)
        assert net_guard.check_url("https://whatever.example") is not None


class _RedirectHandler(BaseHTTPRequestHandler):
    """首跳 302 到云元数据地址（模拟「白名单域名跳内网」绕过）。"""

    target = "http://169.254.169.254/latest/meta-data/"

    def do_GET(self) -> None:  # noqa: N802
        self.send_response(302)
        self.send_header("Location", self.target)
        self.end_headers()

    def log_message(self, *_a: object) -> None:
        return None


def _start_redirect_server() -> tuple[ThreadingHTTPServer, str]:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _RedirectHandler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    port = srv.server_address[1]
    return srv, f"http://127.0.0.1:{port}/start"


class TestSafeFetchTextRedirects:
    async def test_each_redirect_hop_is_validated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """回归：`follow_redirects=True` 只校验首跳，302 到元数据即可绕过。"""
        seen: list[str] = []

        def spy(url: str) -> str | None:
            seen.append(url)
            if "169.254.169.254" in url:
                return "拒绝访问私有/保留地址: 169.254.169.254"
            return None

        monkeypatch.setattr(net_guard, "check_url", spy)

        srv, url = _start_redirect_server()
        try:
            with pytest.raises(ValueError, match="私有/保留地址"):
                await net_guard.safe_fetch_text(url, timeout=5.0)
        finally:
            srv.shutdown()
            srv.server_close()

        assert any("169.254.169.254" in u for u in seen), f"未校验重定向目标: {seen}"

    async def test_redirect_loop_is_bounded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """重定向链必须有跳数上限（防环、防耗尽）。"""
        monkeypatch.setattr(net_guard, "check_url", lambda url: None)
        port_holder: dict[str, int] = {}

        class _Loop(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{port_holder['port']}/next")
                self.end_headers()

            def log_message(self, *_a: object) -> None:
                return None

        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port_holder["port"] = int(s.getsockname()[1])
        s.close()
        srv = ThreadingHTTPServer(("127.0.0.1", port_holder["port"]), _Loop)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            with pytest.raises(ValueError, match="重定向次数超过上限"):
                await net_guard.safe_fetch_text(
                    f"http://127.0.0.1:{port_holder['port']}/x", timeout=5.0
                )
        finally:
            srv.shutdown()
            srv.server_close()


class TestCallSites:
    async def test_web_fetch_blocks_metadata(self) -> None:
        """web_fetch 工具不得抓取云元数据地址。"""
        from plugins.tool_web_fetch.main import WebFetchTool

        out = await WebFetchTool().execute({"url": "http://169.254.169.254/latest/meta-data/"})
        assert "安全策略拒绝" in out

    async def test_mcp_sse_blocks_loopback(self) -> None:
        """MCP SSE 传输：默认拒绝回环地址（配置被污染时兜底）。"""
        from harness.modules.mcp_client.service import MCPServerConfig, MCPSSEConnection

        conn = MCPSSEConnection(MCPServerConfig(name="bad", type="sse", url="http://127.0.0.1:9/sse"))
        with pytest.raises(RuntimeError, match="安全策略拒绝"):
            await conn._open()

    async def test_feishu_webhook_blocks_internal(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """飞书群机器人 webhook 地址指向内网时被拒（防把会话内容推到内网）。"""
        from plugins.integration_hub.feishu_provider import FeishuIntegration

        provider = FeishuIntegration()
        provider._webhook_url = "http://192.168.0.10:8080/hook"
        res = await provider._send_webhook({"text": "hello"})
        assert res.ok is False
        assert "安全策略拒绝" in (res.error or "")
