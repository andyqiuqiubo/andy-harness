"""MCP REST API 测试。

通过 MCP_CONFIG_PATH 指向临时配置文件，验证插件启动连接、
REST 端点列出 server/工具、refresh 重连。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(__file__))

ECHO_SERVER = os.path.join(os.path.dirname(__file__), "mcp_echo_server.py")
PYTHON = sys.executable


@pytest.fixture
def mcp_config_file():
    fd, path = tempfile.mkstemp(suffix=".json", prefix="mcp_test_")
    os.close(fd)
    cfg = {"mcpServers": {"echo": {"command": PYTHON, "args": [ECHO_SERVER]}}}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f)
    previous = os.environ.get("MCP_CONFIG_PATH")
    os.environ["MCP_CONFIG_PATH"] = path
    yield path
    # 还原（而不是直接 pop）：否则会回落到项目里的 mcp.json
    if previous is None:
        os.environ.pop("MCP_CONFIG_PATH", None)
    else:
        os.environ["MCP_CONFIG_PATH"] = previous
    try:
        os.remove(path)
    except OSError:
        pass


def test_mcp_rest_endpoints(mcp_config_file) -> None:
    from harness.main import app

    with TestClient(app) as c:
        # 插件在启动时已连接 echo server
        r = c.get("/api/mcp/servers")
        assert r.status_code == 200
        servers = r.json()["servers"]
        assert any(s["name"] == "echo" and s["connected"] for s in servers)

        r = c.get("/api/mcp/tools")
        assert r.status_code == 200
        tool_names = {t["registered_as"] for t in r.json()["tools"]}
        assert {"mcp__echo__echo", "mcp__echo__add"} <= tool_names

        # refresh 重连不影响工具可用
        r = c.post("/api/mcp/refresh")
        assert r.status_code == 200
        assert "echo" in r.json()["connected"]


def test_mcp_unconfigured_returns_empty(tmp_path) -> None:
    """配置里没有任何 server 时返回空列表而非报错。"""
    empty_cfg = tmp_path / "empty_mcp.json"
    empty_cfg.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
    previous = os.environ.get("MCP_CONFIG_PATH")
    os.environ["MCP_CONFIG_PATH"] = str(empty_cfg)
    from harness.main import app

    try:
        with TestClient(app) as c:
            r = c.get("/api/mcp/servers")
            assert r.status_code == 200
            assert r.json()["servers"] == []
    finally:
        if previous is None:
            os.environ.pop("MCP_CONFIG_PATH", None)
        else:
            os.environ["MCP_CONFIG_PATH"] = previous
