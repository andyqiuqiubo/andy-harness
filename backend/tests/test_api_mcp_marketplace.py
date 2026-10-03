"""MCP 市场 REST API 测试。

- 列表用例断言内置市场里所有服务均为「免鉴权」，且字段完整（不联网）。
- 安装/卸载用例把市场目录与使用中的 MCP 配置都指向临时路径，
  用本地 stdio echo server 完成一次真实连接，避免污染真实 mcp.json。
"""

from __future__ import annotations

import json
import os
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(__file__))

ECHO_SERVER = os.path.join(os.path.dirname(__file__), "mcp_echo_server.py")
PYTHON = sys.executable

# 内置 MCP 市场的免鉴权服务（hosted + stdio，全部无需账号/密钥）
EXPECTED_PACKAGES = {
    "libgen",
    "deepwiki",
    "context7",
    "cloudflare-docs",
    "gitmcp",
}


@pytest.fixture
def mcp_client(tmp_path):
    """创建测试客户端，MCP 配置指向临时文件（避免污染真实 mcp.json）。"""
    cfg = tmp_path / "mcp_for_market.json"
    cfg.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
    previous = os.environ.get("MCP_CONFIG_PATH")
    os.environ["MCP_CONFIG_PATH"] = str(cfg)
    try:
        from harness.main import app

        with TestClient(app) as c:
            yield c
    finally:
        if previous is None:
            os.environ.pop("MCP_CONFIG_PATH", None)
        else:
            os.environ["MCP_CONFIG_PATH"] = previous


class TestMcpMarketplaceList:
    """市场列表（只读，不联网）。"""

    def test_lists_five_no_auth_servers(self, mcp_client: TestClient) -> None:
        resp = mcp_client.get("/api/mcp/marketplace")
        assert resp.status_code == 200
        items = resp.json()
        ids = {i["package_id"] for i in items}
        assert EXPECTED_PACKAGES <= ids, f"缺失: {EXPECTED_PACKAGES - ids}"

    def test_items_are_complete_and_no_auth(self, mcp_client: TestClient) -> None:
        """每个条目字段完整，且明确标注无需鉴权。"""
        resp = mcp_client.get("/api/mcp/marketplace")
        by_id = {i["package_id"]: i for i in resp.json()}
        for pid in EXPECTED_PACKAGES:
            item = by_id[pid]
            assert item["name"], f"{pid} 缺少 name"
            assert item["url"], f"{pid} 缺少 url"
            assert item["description"], f"{pid} 缺少 description"
            assert item["long_description"], f"{pid} 缺少 long_description"
            assert item["auth_required"] is False, f"{pid} 不应需要鉴权"
            assert isinstance(item["installed"], bool)


class TestMcpMarketplaceInstall:
    """安装 / 卸载往返（用本地 echo server，避免依赖外网）。"""

    def test_install_and_uninstall_round_trip(
        self, mcp_client: TestClient, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from harness.api.rest import mcp as mcp_api

        market_dir = tmp_path / "market"
        market_dir.mkdir()
        (market_dir / "local-echo.json").write_text(
            json.dumps(
                {
                    "name": "local-echo",
                    "type": "stdio",
                    "command": PYTHON,
                    "args": [ECHO_SERVER],
                    "description": "本地 echo 测试服务器",
                    "long_description": "仅用于测试：回显文本、计算两数之和。",
                    "auth_required": False,
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(mcp_api, "_MCP_MARKETPLACE_DIR", market_dir)

        # 市场列表应只反映被替换后的目录 + 标记未安装
        items = mcp_client.get("/api/mcp/marketplace").json()
        assert [i["package_id"] for i in items] == ["local-echo"]
        assert items[0]["installed"] is False

        # 安装：写入配置并真实连上
        resp = mcp_client.post("/api/mcp/marketplace/local-echo/install")
        assert resp.status_code == 200
        data = resp.json()
        assert data["installed"] is True
        assert data["connected"] is True
        assert {"echo", "add"} <= set(data["tools"])

        # 已配置 server 列表里应出现它
        servers = mcp_client.get("/api/mcp/servers").json()["servers"]
        assert any(s["name"] == "local-echo" and s["connected"] for s in servers)

        # 重复安装应冲突
        again = mcp_client.post("/api/mcp/marketplace/local-echo/install")
        assert again.status_code == 409

        # 卸载
        resp2 = mcp_client.delete("/api/mcp/marketplace/local-echo")
        assert resp2.status_code == 200
        assert resp2.json()["removed"] == "local-echo"
        servers2 = mcp_client.get("/api/mcp/servers").json()["servers"]
        assert not any(s["name"] == "local-echo" for s in servers2)

        # 重复卸载应 404
        assert mcp_client.delete("/api/mcp/marketplace/local-echo").status_code == 404

    def test_install_unknown_package(self, mcp_client: TestClient) -> None:
        resp = mcp_client.post("/api/mcp/marketplace/no-such-package/install")
        assert resp.status_code == 404
