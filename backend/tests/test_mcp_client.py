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


# ── per-server 选项：max_line_bytes / tool_allowlist ──


def test_parse_max_line_bytes_and_allowlist() -> None:
    """max_line_bytes 与 tool_allowlist 正常解析。"""
    cfg = parse_server_config(
        {
            "name": "big",
            "command": "node",
            "args": ["server.mjs"],
            "max_line_bytes": 8 * 1024 * 1024,
            "tool_allowlist": ["analyze_javascript_application", "inspect_pe_resources"],
        }
    )
    assert cfg.max_line_bytes == 8 * 1024 * 1024
    assert cfg.tool_allowlist == ["analyze_javascript_application", "inspect_pe_resources"]


def test_parse_max_line_bytes_rejects_out_of_range() -> None:
    """max_line_bytes 越界或非整数直接拒绝（护栏不被误配打穿）。"""
    for bad in (10, 10**9, "abc", "1024x"):
        with pytest.raises(ValueError):
            parse_server_config({"name": "x", "command": "c", "max_line_bytes": bad})


def test_parse_tool_allowlist_accepts_comma_string() -> None:
    """tool_allowlist 兼容逗号分隔字符串写法。"""
    cfg = parse_server_config(
        {"name": "x", "command": "c", "tool_allowlist": "echo, add ,"}
    )
    assert cfg.tool_allowlist == ["echo", "add"]


def test_parse_tool_allowlist_rejects_wrong_type() -> None:
    """tool_allowlist 既不是字符串也不是列表时拒绝。"""
    with pytest.raises(ValueError):
        parse_server_config({"name": "x", "command": "c", "tool_allowlist": 42})


def test_parse_request_timeout() -> None:
    """request_timeout 正常解析（数字/数字字符串）并做区间校验。"""
    assert parse_server_config(
        {"name": "slow", "command": "c", "request_timeout": 600}
    ).request_timeout == 600.0
    assert parse_server_config(
        {"name": "slow", "command": "c", "request_timeout": "330"}
    ).request_timeout == 330.0
    for bad in (1, 99999, "abc"):
        with pytest.raises(ValueError):
            parse_server_config({"name": "x", "command": "c", "request_timeout": bad})


async def test_tool_allowlist_filters_tools() -> None:
    """白名单只放行名单内工具（空名单不过滤，行为不变）。"""
    cfg = MCPServerConfig(
        name="echo-filtered",
        command=PYTHON,
        args=[ECHO_SERVER],
        tool_allowlist=["echo"],
    )
    conn = await asyncio.wait_for(_connect(cfg), timeout=20)
    try:
        assert [t.name for t in conn.tools] == ["echo"]
    finally:
        await conn.disconnect()


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


# ── SEC-15：MCP 客户端资源管理 ────────────────────────
async def test_stderr_flood_does_not_deadlock(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """回归：stderr=PIPE 无消费者时，子进程写满 64KB 管道缓冲区即阻塞挂死，
    表现为握手超时。修复后 stderr 由后台线程持续抽干，握手应正常完成。"""
    from harness.modules.mcp_client import service as mcs

    monkeypatch.setattr(mcs, "_REQUEST_TIMEOUT", 10.0)  # 失败时快速暴露，不拖 60s
    script = tmp_path / "noisy_server.py"
    # 一个「噪音」server：先往 stderr 灌 200KB（远超 64KB 管道缓冲区），
    # 再正常应答 initialize / tools/list（通知类消息无 id，需跳过）。
    script.write_text(
        "import json, sys\n"
        'sys.stderr.write("E" * 200000 + "\\n")\n'
        "sys.stderr.flush()\n"
        "def handle(req):\n"
        '    m = req.get("method")\n'
        '    if m == "initialize":\n'
        '        return {"serverInfo": {"name": "noisy"}}\n'
        '    if m == "tools/list":\n'
        '        return {"tools": []}\n'
        "    return {}\n"
        "while True:\n"
        "    line = sys.stdin.readline()\n"
        "    if not line:\n"
        "        break\n"
        "    req = json.loads(line)\n"
        '    if "id" not in req:\n'
        "        continue\n"
        '    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": req["id"],'
        ' "result": handle(req)}) + "\\n")\n'
        "    sys.stdout.flush()\n",
        encoding="utf-8",
    )
    conn = mcs.MCPServerConnection(mcs.MCPServerConfig(name="noisy", command=PYTHON, args=[str(script)]))
    try:
        tools = await asyncio.wait_for(conn.connect(), timeout=20)
        assert tools == []
    finally:
        await conn.disconnect()


async def test_read_loop_skips_oversized_line(monkeypatch: pytest.MonkeyPatch) -> None:
    """回归：`readline()` 无上限，单行超长输出会把内存打满。
    修复后超长行被丢弃，后续正常行仍能被解析分发。"""
    import io
    import json as _json

    from harness.modules.mcp_client import service as mcs

    # 32B 垃圾行需被丢弃，随后 46B 的合法响应行必须仍被解析
    monkeypatch.setattr(mcs, "_MAX_LINE_BYTES", 64)
    monkeypatch.setattr(mcs, "_MAX_SKIP_BYTES", 4096)

    conn = mcs.MCPServerConnection(mcs.MCPServerConfig(name="fake", command="x"))
    loop = asyncio.get_running_loop()
    fut: asyncio.Future[dict] = loop.create_future()
    conn._pending[1] = fut

    payload = _json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"tools": []}}).encode()

    class FakeProc:
        stdout = io.BytesIO(b"A" * 500 + b"\n" + payload + b"\n")
        stderr = io.BytesIO(b"")

    conn._proc = FakeProc()  # type: ignore[assignment]
    conn._owner_loop = loop
    await asyncio.to_thread(conn._read_loop_sync)
    assert fut.done() and not fut.cancelled()
    assert fut.result() == {"tools": []}


async def test_connect_failure_terminates_child(monkeypatch: pytest.MonkeyPatch) -> None:
    """回归：握手失败时只 pop 连接不 disconnect，子进程会残留（僵尸累积）。"""
    from harness.modules.mcp_client import service as mcs

    async def boom_handshake(self) -> None:  # type: ignore[no-untyped-def]
        raise RuntimeError("handshake boom")

    monkeypatch.setattr(mcs.MCPServerConnection, "_handshake", boom_handshake)

    seen: list[mcs.MCPServerConnection] = []
    procs: list[object] = []
    orig_disconnect = mcs.MCPServerConnection.disconnect

    async def spy_disconnect(self) -> None:  # type: ignore[no-untyped-def]
        seen.append(self)
        if self._proc is not None:
            procs.append(self._proc)
        await orig_disconnect(self)

    monkeypatch.setattr(mcs.MCPServerConnection, "disconnect", spy_disconnect)

    cfg = MCPServerConfig(
        name="boom",
        command=PYTHON,
        args=["-c", "import time; time.sleep(60)"],
    )
    svc = MCPClientService(config_paths=())
    with pytest.raises(RuntimeError, match="handshake boom"):
        await svc.connect_server(cfg)
    assert "boom" not in svc._servers
    assert seen, "连接失败必须触发 disconnect"
    assert procs, "应捕获到已启动的子进程"
    for proc in procs:
        assert proc.poll() is not None, "握手失败后子进程必须已被终止（不留僵尸）"


async def test_disconnect_all_continues_on_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """单个 server 断开失败不得中断其余清理（否则泄漏级联放大）。"""
    from harness.modules.mcp_client import service as mcs

    calls: list[str] = []

    class FakeConn(mcs.BaseMCPConnection):
        def __init__(self, name: str, fail: bool) -> None:
            super().__init__(mcs.MCPServerConfig(name=name))
            self._fail = fail

        async def _open(self) -> None:
            return None

        async def _send(self, payload: dict) -> None:
            return None

        async def disconnect(self) -> None:
            calls.append(self.config.name)
            if self._fail:
                raise RuntimeError("disconnect boom")

    svc = MCPClientService(config_paths=())
    svc._servers["a"] = FakeConn("a", fail=True)  # type: ignore[assignment]
    svc._servers["b"] = FakeConn("b", fail=False)  # type: ignore[assignment]
    await svc.disconnect_all()
    assert calls == ["a", "b"], "清理必须覆盖全部连接"
    assert svc._servers == {}
