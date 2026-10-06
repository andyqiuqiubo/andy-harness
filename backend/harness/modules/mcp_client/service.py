"""轻量 MCP（Model Context Protocol）客户端。

同时支持两种传输，零第三方依赖（仅用标准库 + 项目已有的 httpx）：

- **stdio**：启动 MCP server 子进程，用换行分隔的 JSON-RPC 2.0 走 stdin/stdout。
- **sse**：HTTP + Server-Sent Events —— 先 `GET <url>` 建立事件流，服务器通过
  `event: endpoint` 下发 POST 地址；请求 POST 到该地址，响应经事件流回传。
  同时兼容「POST 直接返回 JSON-RPC 响应」的 streamable-http 形态。

两种传输共享协议逻辑（握手 / tools.list / tools.call / 请求-响应配对），
仅传输细节不同。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import subprocess
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin

logger = logging.getLogger("harness.mcp")

# MCP 协议版本（2024-11-05 为广泛兼容版本）
PROTOCOL_VERSION = "2024-11-05"

DEFAULT_CONFIG_PATHS = (
    "mcp.json",
    os.path.expanduser("~/.andy-harness/mcp.json"),
)

# 传输类型：stdio / sse（http 家族归入 sse 实现）
STDIO = "stdio"
SSE = "sse"
_HTTP_LIKE = {SSE, "http", "streamable-http", "streamable_http"}

# 等待服务端下发 SSE endpoint 的最长时间（超时后退回直接 POST 模式）。
# 抽成模块常量便于测试把它调小，无需真的等 20 秒。
_OPEN_ENDPOINT_TIMEOUT = 20.0


def _default_config_paths() -> tuple[str, ...]:
    """配置路径：优先使用环境变量 MCP_CONFIG_PATH（支持多个以 ; 分隔）。"""
    env = os.environ.get("MCP_CONFIG_PATH")
    if env:
        return tuple(p for p in env.split(";") if p)
    return DEFAULT_CONFIG_PATHS


@dataclass
class MCPToolSpec:
    """单个 MCP 工具的描述。"""

    server: str
    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass
class MCPServerConfig:
    """MCP server 配置。

    - stdio：`command` + `args`
    - sse：`url`
    """

    name: str
    type: str = STDIO
    command: str = ""
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    description: str = ""
    enabled: bool = True


# ────────────────────────── 协议基类 ──────────────────────────


class BaseMCPConnection(ABC):
    """传输无关的 MCP 连接：握手 / 工具清单 / 请求配对。"""

    def __init__(self, config: MCPServerConfig) -> None:
        self.config = config
        self._tools: list[MCPToolSpec] = []
        self._next_id = 0
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._lock = asyncio.Lock()
        self._closed = False

    @property
    def name(self) -> str:
        return self.config.name

    @property
    def tools(self) -> list[MCPToolSpec]:
        return list(self._tools)

    async def connect(self) -> list[MCPToolSpec]:
        """建立传输 → 握手 → 拉取工具清单。"""
        if self._connected:
            return self._tools
        # 允许断开后重连：复位关闭标志与遗留的待决请求
        self._closed = False
        self._pending.clear()
        await self._open()
        await self._handshake()
        self._tools = await self._list_tools()
        return self._tools

    @property
    def _connected(self) -> bool:
        """是否已建立传输（子类可覆写）。"""
        return False

    # ── 传输细节（子类实现） ──────────────────────────

    @abstractmethod
    async def _open(self) -> None:
        """建立传输通道（stdio 起进程 / sse 建事件流）。"""

    @abstractmethod
    async def _send(self, payload: dict[str, Any]) -> None:
        """把一条 JSON-RPC 消息发出去。"""

    @abstractmethod
    async def disconnect(self) -> None:
        """关闭传输。"""

    # ── 协议逻辑 ──────────────────────────────────────

    async def _handshake(self) -> None:
        init = await self._request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "andy-harness", "version": "1.0.0"},
            },
        )
        # 发送 initialized 通知（无需响应）
        await self._notify("notifications/initialized", {})
        if isinstance(init, dict) and "serverInfo" in init:
            logger.info(
                "MCP server '%s' 已连接: %s",
                self.config.name,
                init.get("serverInfo"),
            )

    async def _list_tools(self) -> list[MCPToolSpec]:
        result = await self._request("tools/list", {})
        raw_tools = result.get("tools", []) if isinstance(result, dict) else []
        specs: list[MCPToolSpec] = []
        for t in raw_tools:
            if not isinstance(t, dict):
                continue
            specs.append(
                MCPToolSpec(
                    server=self.config.name,
                    name=str(t.get("name", "")),
                    description=str(t.get("description", "")),
                    input_schema=t.get("inputSchema") or {"type": "object", "properties": {}},
                )
            )
        return specs

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> str:
        """调用远端工具，返回拼接后的文本内容。"""
        result = await self._request("tools/call", {"name": tool_name, "arguments": arguments or {}})
        if not isinstance(result, dict):
            return str(result)
        if result.get("isError"):
            content = result.get("content", [])
            return "MCP 工具错误: " + self._extract_text(content)
        return self._extract_text(result.get("content", []))

    @staticmethod
    def _extract_text(content: Any) -> str:
        """从 MCP content 数组中抽取文本。"""
        if not isinstance(content, list):
            return str(content)
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "text":
                    parts.append(str(item.get("text", "")))
                elif "data" in item:
                    parts.append(str(item.get("data", "")))
                else:
                    parts.append(json.dumps(item, ensure_ascii=False))
            else:
                parts.append(str(item))
        return "\n".join(parts)

    async def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        async with self._lock:
            self._next_id += 1
            req_id = self._next_id
            payload = {
                "jsonrpc": "2.0",
                "id": req_id,
                "method": method,
                "params": params,
            }
            loop = asyncio.get_event_loop()
            future: asyncio.Future[dict[str, Any]] = loop.create_future()
            self._pending[req_id] = future
            await self._send(payload)
            try:
                return await asyncio.wait_for(future, timeout=60)
            except TimeoutError as e:
                self._pending.pop(req_id, None)
                raise RuntimeError(f"MCP request timeout: {method} server={self.config.name}") from e

    async def _notify(self, method: str, params: dict[str, Any]) -> None:
        """发送通知（无需响应）。

        通知是 fire-and-forget：部分 server（如某些 SSE 网关）对不带 id 的通知
        会返回 4xx，这里必须容忍——否则整个握手会因此失败。
        """
        payload = {"jsonrpc": "2.0", "method": method, "params": params}
        async with self._lock:
            try:
                await self._send(payload)
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "MCP 通知发送失败（已忽略，server=%s）: %s — %s",
                    self.config.name,
                    method,
                    e,
                )

    async def _dispatch(self, msg: dict[str, Any]) -> None:
        """把收到的 JSON-RPC 响应配给对应请求。"""
        if "id" in msg and ("result" in msg or "error" in msg):
            req_id = msg["id"]
            fut = self._pending.pop(req_id, None)
            if fut is not None and not fut.done():
                if "error" in msg:
                    fut.set_exception(RuntimeError(f"MCP 错误: {msg['error']}"))
                else:
                    fut.set_result(msg.get("result", {}))
        # 通知类消息（无 id）此处忽略

    def _fail_pending(self, reason: str) -> None:
        """通道断开时让所有待决请求立即失败，而不是等 60s 超时。"""
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(RuntimeError(reason))
        self._pending.clear()


# ────────────────────────── stdio 传输 ──────────────────────────


class MCPServerConnection(BaseMCPConnection):
    """stdio 传输：MCP server 子进程。

    Windows 上 ``uvicorn --reload`` 的事件循环是 SelectorEventLoop，**不支持**
    :func:`asyncio.create_subprocess_exec`（抛裸 NotImplementedError 且消息为空）。
    因此这里不用 asyncio 子进程，改为「同步 :class:`subprocess.Popen` +
    后台阻塞读线程 + :func:`asyncio.run_coroutine_threadsafe` 回投主循环」，
    与事件循环种类无关；写入走线程避免阻塞主循环。
    """

    def __init__(self, config: MCPServerConfig) -> None:
        super().__init__(config)
        self._proc: subprocess.Popen[bytes] | None = None
        self._reader_thread: threading.Thread | None = None
        self._owner_loop: asyncio.AbstractEventLoop | None = None

    @property
    def _connected(self) -> bool:
        return self._proc is not None

    async def _open(self) -> None:
        cfg = self.config
        if not cfg.command:
            raise RuntimeError(f"MCP server '{cfg.name}' 缺少 command（stdio 传输）")
        env = dict(os.environ)
        for k, v in cfg.env.items():
            env[k] = v
        self._owner_loop = asyncio.get_running_loop()
        try:
            self._proc = await asyncio.to_thread(
                subprocess.Popen,
                [cfg.command, *cfg.args],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
            )
        except (FileNotFoundError, OSError) as e:
            raise RuntimeError(f"无法启动 MCP server '{cfg.name}': {e}") from e
        self._reader_thread = threading.Thread(
            target=self._read_loop_sync,
            name=f"mcp-stdio-{cfg.name}",
            daemon=True,
        )
        self._reader_thread.start()

    def _write_sync(self, data: bytes) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise RuntimeError("MCP stdio 连接未建立")
        proc.stdin.write(data)
        proc.stdin.flush()

    async def _send(self, payload: dict[str, Any]) -> None:
        line = json.dumps(payload, ensure_ascii=False) + "\n"
        await asyncio.to_thread(self._write_sync, line.encode("utf-8"))

    def _read_loop_sync(self) -> None:
        """后台线程：阻塞读子进程 stdout，逐行回投主循环分发。"""
        proc = self._proc
        loop = self._owner_loop
        if proc is None or proc.stdout is None or loop is None:
            return
        try:
            while not self._closed:
                raw = proc.stdout.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                try:
                    asyncio.run_coroutine_threadsafe(self._dispatch(msg), loop)
                except RuntimeError:
                    return  # 主循环已关闭（应用退出），读线程自行结束
        except OSError:
            pass
        finally:
            try:
                loop.call_soon_threadsafe(
                    self._fail_pending,
                    f"MCP stdio 通道已关闭（server={self.config.name}）",
                )
            except RuntimeError:
                pass

    @staticmethod
    def _terminate_sync(proc: subprocess.Popen[bytes]) -> None:
        """terminate → 等 5s → kill（与原 asyncio 版语义一致）。"""
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass

    async def disconnect(self) -> None:
        self._closed = True
        proc = self._proc
        if proc is not None:
            self._proc = None
            await asyncio.to_thread(self._terminate_sync, proc)
        # 读线程在 EOF / _closed 后自行退出（daemon，不阻塞进程退出）


# ────────────────────────── SSE / HTTP 传输 ──────────────────────────


class MCPSSEConnection(BaseMCPConnection):
    """SSE（HTTP + Server-Sent Events）传输。

    流程：
    1. `GET <url>` 建立事件流（Accept: text/event-stream）；
    2. 服务器发 `event: endpoint` + 相对/绝对 POST 地址；
    3. 请求 POST 到该地址；响应作为 `message` 事件从事件流回传。
    兼容 POST 直接返回 JSON-RPC 响应的 streamable-http 形态。
    """

    def __init__(self, config: MCPServerConfig) -> None:
        super().__init__(config)
        self._client: Any = None
        self._read_task: asyncio.Task[None] | None = None
        self._post_url: str | None = None
        self._endpoint_ready = asyncio.Event()
        # 是否处于「直接 POST」模式（服务端不支持 GET 建流的 streamable-http）
        self._direct_post = False
        # streamable-http 会话 ID（initialize 响应下发，后续请求需回传）
        self._session_id: str | None = None

    @property
    def _connected(self) -> bool:
        return self._client is not None

    async def _open(self) -> None:
        import httpx

        cfg = self.config
        if not cfg.url:
            raise RuntimeError(f"MCP server '{cfg.name}' 缺少 url（sse 传输）")
        self._endpoint_ready.clear()
        self._post_url = None
        # read=None：SSE 长连接不能有读超时
        # max_keepalive_connections=0：每个 POST 都用新连接。某些 SSE 网关
        # （如 youth-mcp）在复用 keep-alive 连接时会 404 并使会话失效。
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=15.0, read=None, write=30.0, pool=15.0),
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=0),
            follow_redirects=True,
        )
        self._read_task = asyncio.ensure_future(self._read_sse())
        # 等服务器下发 POST 端点（最多 20s）
        try:
            await asyncio.wait_for(self._endpoint_ready.wait(), timeout=_OPEN_ENDPOINT_TIMEOUT)
        except TimeoutError:
            # GET 事件流已建立但始终没有 endpoint 事件 —— 这是 streamable-http
            # server 的典型行为（GET 只用于服务端推送，请求一律走 POST）。
            # 不报错，直接退回「直接 POST」模式；原事件流任务保留，仍可接收通知。
            logger.info(
                "MCP server '%s' 未在 %ss 内下发 SSE endpoint，改用直接 POST 模式",
                cfg.name,
                _OPEN_ENDPOINT_TIMEOUT,
            )
            self._direct_post = True
            self._post_url = cfg.url
        if not self._post_url:
            # 兜底：极端情况下仍无可用端点
            await self.disconnect()
            raise RuntimeError(f"MCP server '{cfg.name}' 未获得可用的 POST endpoint")

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers: dict[str, str] = {
            "User-Agent": "andy-harness-mcp/0.1",
            **self.config.headers,
        }
        if extra:
            headers.update(extra)
        return headers

    async def _read_sse(self) -> None:
        assert self._client is not None
        import httpx

        cfg = self.config
        try:
            async with self._client.stream(
                "GET",
                cfg.url,
                headers=self._headers({"Accept": "text/event-stream"}),
            ) as resp:
                try:
                    resp.raise_for_status()
                except httpx.HTTPStatusError as e:
                    # 服务端不支持 GET 建流（纯 streamable-http 的 server 会回 405）
                    # → 退回「直接 POST」模式：把配置里的 URL 直接当 JSON-RPC 端点。
                    # 这样同一份配置既能连旧版 SSE server，也能连新版 streamable-http server。
                    logger.info(
                        "MCP server '%s' 不支持 GET 事件流(HTTP %s)，退回直接 POST 模式",
                        cfg.name,
                        e.response.status_code,
                    )
                    self._direct_post = True
                    self._post_url = cfg.url
                    return
                event: str | None = None
                data_lines: list[str] = []
                async for line in resp.aiter_lines():
                    if self._closed:
                        break
                    if line == "":
                        if event is not None or data_lines:
                            await self._handle_event(event, "\n".join(data_lines))
                        event = None
                        data_lines = []
                        continue
                    if line.startswith(":"):
                        continue  # 注释/心跳
                    if line.startswith("event:"):
                        event = line[len("event:") :].strip()
                    elif line.startswith("data:"):
                        data_lines.append(line[len("data:") :].lstrip())
                if event is not None or data_lines:
                    await self._handle_event(event, "\n".join(data_lines))
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning("MCP SSE 流结束（server=%s）: %s", cfg.name, e)
        finally:
            self._endpoint_ready.set()  # 避免 connect 一直等
            self._fail_pending(f"MCP SSE 流已关闭（server={cfg.name}）")

    async def _handle_event(self, event: str | None, data: str) -> None:
        if event == "endpoint":
            target = data.strip()
            if target:
                self._post_url = urljoin(self.config.url, target)
                logger.info("MCP SSE endpoint（server=%s）: %s", self.config.name, self._post_url)
            self._endpoint_ready.set()
            return
        if event in ("ping", "keepalive"):
            return
        payload = data.strip()
        if not payload:
            return
        try:
            msg = json.loads(payload)
        except json.JSONDecodeError:
            return
        if isinstance(msg, dict):
            await self._dispatch(msg)

    async def _send(self, payload: dict[str, Any]) -> None:
        assert self._client is not None, "MCP SSE 连接未建立"
        post_url = self._post_url
        if not post_url:
            raise RuntimeError("MCP SSE 尚未获得 POST endpoint")
        post_headers = self._headers(
            {
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                # 强制每请求新连接：避免复用 keep-alive 连接导致网关会话失效
                "Connection": "close",
            }
        )
        # streamable-http：会话 ID 需在后续请求中回传
        if self._session_id:
            post_headers["Mcp-Session-Id"] = self._session_id
        resp = await self._client.post(
            post_url,
            json=payload,
            headers=post_headers,
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"MCP SSE POST 失败（{resp.status_code}）: {resp.text[:200]}")
        # 记录会话 ID（标准头为 Mcp-Session-Id，大小写不敏感读取）
        session_id = resp.headers.get("mcp-session-id")
        if session_id:
            self._session_id = session_id
        # 兼容 streamable-http：POST 直接返回 JSON-RPC 响应
        ctype = resp.headers.get("content-type", "")
        if "application/json" in ctype:
            try:
                msg = resp.json()
            except Exception:  # noqa: BLE001
                msg = None
            if isinstance(msg, dict) and ("result" in msg or "error" in msg):
                await self._dispatch(msg)
            return
        # streamable-http 另一种常见形态：POST 响应体本身就是 SSE 事件流
        # （`event: message` + `data: {...}`），需就地解析并分发。
        body = resp.text
        if "text/event-stream" in ctype or body.lstrip().startswith(("event:", "data:")):
            await self._parse_inline_sse(body)

    async def _parse_inline_sse(self, body: str) -> None:
        """解析 POST 响应体内的 SSE 文本并分发其中的 JSON-RPC 消息。"""
        event: str | None = None
        data_lines: list[str] = []

        async def flush() -> None:
            nonlocal event, data_lines
            if event is not None or data_lines:
                await self._handle_event(event, "\n".join(data_lines))
            event = None
            data_lines = []

        for raw_line in body.splitlines():
            line = raw_line.rstrip("\r")
            if line == "":
                await flush()
                continue
            if line.startswith(":"):
                continue  # 注释/心跳
            if line.startswith("event:"):
                event = line[len("event:") :].strip()
            elif line.startswith("data:"):
                data_lines.append(line[len("data:") :].lstrip())
        await flush()

    async def disconnect(self) -> None:
        self._closed = True
        if self._read_task is not None:
            self._read_task.cancel()
            try:
                await self._read_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._read_task = None
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:  # noqa: BLE001
                pass
            self._client = None
        self._post_url = None
        self._direct_post = False
        self._session_id = None


def make_connection(config: MCPServerConfig) -> BaseMCPConnection:
    """按配置类型创建对应传输的连接。"""
    if (config.type or STDIO).lower() in _HTTP_LIKE:
        return MCPSSEConnection(config)
    return MCPServerConnection(config)


def parse_server_config(raw: dict[str, Any]) -> MCPServerConfig:
    """从配置字典解析单个 server 配置（兼容 Claude Desktop 风格）。"""
    raw_type = str(raw.get("type") or "").strip().lower()
    url = str(raw.get("url") or "").strip()
    command = str(raw.get("command") or "").strip()

    # 推断类型：显式 type 优先；否则有 url 视为 sse，有 command 视为 stdio
    if raw_type in _HTTP_LIKE:
        cfg_type = SSE
    elif raw_type == STDIO or raw_type == "":
        cfg_type = SSE if (url and not command) else STDIO
    else:
        cfg_type = raw_type

    if cfg_type == STDIO and not command:
        raise ValueError(f"MCP server '{raw.get('name')}' 缺少 command")
    if cfg_type == SSE and not url:
        raise ValueError(f"MCP server '{raw.get('name')}' 缺少 url")

    if isinstance(raw.get("args"), str):
        args = shlex.split(raw["args"])
    else:
        args = list(raw.get("args") or [])

    # 支持 enabled / disabled 两种写法（disabled 为 Claude 风格）
    if "disabled" in raw:
        enabled = not bool(raw.get("disabled"))
    else:
        enabled = bool(raw.get("enabled", True))

    return MCPServerConfig(
        name=str(raw.get("name") or url or command),
        type=cfg_type,
        command=command,
        args=[str(a) for a in args],
        env={str(k): str(v) for k, v in (raw.get("env") or {}).items()},
        url=url,
        headers={str(k): str(v) for k, v in (raw.get("headers") or {}).items()},
        description=str(raw.get("description") or ""),
        enabled=enabled,
    )


# ────────────────────────── 服务 ──────────────────────────


class MCPClientService:
    """MCP 客户端服务：管理多个 server 连接与工具清单。"""

    def __init__(self, config_paths: tuple[str, ...] | None = None) -> None:
        self._config_paths = config_paths or _default_config_paths()
        self._servers: dict[str, BaseMCPConnection] = {}
        self._configs: dict[str, MCPServerConfig] = {}

    def load_config(self) -> dict[str, MCPServerConfig]:
        """从配置文件加载 server 配置（不连接）。

        多路径**合并**语义：依次读取 ``self._config_paths`` 中的每一个存在
        的配置文件，同名 server 由后读到的（优先级更低）路径**不覆盖**先读到
        的高优先级配置。

        旧实现在命中第一个存在的文件后即 ``clear()`` + ``return``，导致用户级
        ``mcp.json`` 一旦存在，项目内置路径下的 server 会被整体丢弃；随后
        ``add_server → _save()`` 又把这份残缺配置写回主路径，丢失被固化。
        """
        loaded_any = False
        merged: dict[str, MCPServerConfig] = {}
        for path in self._config_paths:
            if not os.path.exists(path):
                continue
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
            except (json.JSONDecodeError, OSError) as e:
                logger.warning("读取 MCP 配置失败 %s: %s", path, e)
                continue
            servers = data.get("mcpServers", data) if isinstance(data, dict) else {}
            if not isinstance(servers, dict):
                continue
            count = 0
            for name, raw in servers.items():
                if not isinstance(raw, dict):
                    continue
                raw = dict(raw)
                raw.setdefault("name", name)
                try:
                    cfg = parse_server_config(raw)
                except ValueError as e:
                    logger.warning("跳过无效 MCP 配置 %s: %s", name, e)
                    continue
                if name in merged:
                    logger.debug("MCP server %s 已由更高优先级配置提供，跳过 %s", name, path)
                    continue
                merged[name] = cfg
                count += 1
            loaded_any = True
            logger.info("已从 %s 加载 %d 个 MCP server", path, count)
        self._configs = merged
        if not loaded_any:
            logger.info("未找到 MCP 配置文件（已探查 %s）", self._config_paths)
        return self._configs

    def get_configs(self) -> dict[str, MCPServerConfig]:
        return dict(self._configs)

    def _primary_path(self) -> str:
        """选择用于持久化配置的路径：已有的优先，否则取默认首个。"""
        for path in self._config_paths:
            if os.path.exists(path):
                return path
        return self._config_paths[0]

    def _save(self) -> None:
        """把当前 _configs 写回主配置文件。"""
        path = self._primary_path()
        servers: dict[str, Any] = {}
        for name, cfg in self._configs.items():
            if (cfg.type or STDIO).lower() in _HTTP_LIKE:
                servers[name] = {
                    "type": "sse",
                    "url": cfg.url,
                    "headers": cfg.headers,
                    "description": cfg.description,
                    "disabled": not cfg.enabled,
                }
            else:
                servers[name] = {
                    "command": cfg.command,
                    "args": cfg.args,
                    "env": cfg.env,
                    "description": cfg.description,
                    "disabled": not cfg.enabled,
                }
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"mcpServers": servers}, f, ensure_ascii=False, indent=2)

    def add_server(self, cfg: MCPServerConfig, *, overwrite: bool = False) -> None:
        """添加一个 server 配置并持久化。

        Args:
            cfg: server 配置。
            overwrite: 允许覆盖同名配置。默认 False——同名已存在时抛
                ``RuntimeError``（路由映射为 409），避免静默覆盖掉用户已有的
                URL / command / headers 配置。

        Raises:
            RuntimeError: 同名 server 已连接，或同名配置已存在且未允许覆盖。
        """
        if cfg.name in self._servers and not overwrite:
            raise RuntimeError(f"server '{cfg.name}' 已连接，请先断开")
        if cfg.name in self._configs and not overwrite:
            raise RuntimeError(f"server '{cfg.name}' 配置已存在")
        self._configs[cfg.name] = cfg
        self._save()

    def remove_server(self, name: str) -> bool:
        """移除一个 server 配置并持久化。

        Returns:
            是否真实删除了配置（False 表示原本就不存在，调用方可据此返回 404）。
        """
        existed = name in self._configs
        self._configs.pop(name, None)
        if existed:
            self._save()
        return existed

    async def connect_server(self, cfg: MCPServerConfig) -> list[MCPToolSpec]:
        """连接单个 server（已连接则返回其现有工具清单）。"""
        existing = self._servers.get(cfg.name)
        if existing is not None:
            return existing.tools
        conn = make_connection(cfg)
        self._servers[cfg.name] = conn
        try:
            return await conn.connect()
        except Exception:
            # 连接失败不留残连接
            self._servers.pop(cfg.name, None)
            raise

    async def disconnect_server(self, name: str) -> None:
        """断开并移除单个 server 连接（不修改持久化配置）。"""
        conn = self._servers.pop(name, None)
        if conn is not None:
            await conn.disconnect()

    async def connect_all(self) -> dict[str, list[MCPToolSpec]]:
        """连接所有 enabled 的 server 并返回每组的工具清单。"""
        if not self._configs:
            self.load_config()
        result: dict[str, list[MCPToolSpec]] = {}
        for name, cfg in self._configs.items():
            if not cfg.enabled:
                continue
            try:
                result[name] = await self.connect_server(cfg)
            except Exception as e:  # noqa: BLE001
                logger.error("连接 MCP server '%s' 失败: %s", name, e)
        return result

    async def disconnect_all(self) -> None:
        for conn in self._servers.values():
            await conn.disconnect()
        self._servers.clear()

    def list_tools(self) -> list[MCPToolSpec]:
        tools: list[MCPToolSpec] = []
        for conn in self._servers.values():
            tools.extend(conn.tools)
        return tools

    async def call_tool(self, server_name: str, tool_name: str, arguments: dict[str, Any]) -> str:
        conn = self._servers.get(server_name)
        if conn is None:
            raise RuntimeError(f"MCP server '{server_name}' 未连接")
        return await conn.call_tool(tool_name, arguments)

    def is_connected(self, server_name: str) -> bool:
        return server_name in self._servers
