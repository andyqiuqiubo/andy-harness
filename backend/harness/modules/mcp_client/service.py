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
                "clientInfo": {"name": "andy-harness", "version": "0.1.0"},
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
                    input_schema=t.get("inputSchema")
                    or {"type": "object", "properties": {}},
                )
            )
        return specs

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> str:
        """调用远端工具，返回拼接后的文本内容。"""
        result = await self._request(
            "tools/call", {"name": tool_name, "arguments": arguments or {}}
        )
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
                raise RuntimeError(
                    f"MCP 请求超时（{method}，server={self.config.name}）"
                ) from e

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
    """stdio 传输：MCP server 子进程。"""

    def __init__(self, config: MCPServerConfig) -> None:
        super().__init__(config)
        self._proc: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None

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
        try:
            self._proc = await asyncio.create_subprocess_exec(
                cfg.command,
                *cfg.args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
        except (FileNotFoundError, OSError) as e:
            raise RuntimeError(f"无法启动 MCP server '{cfg.name}': {e}") from e
        self._reader_task = asyncio.ensure_future(self._read_loop())

    async def _send(self, payload: dict[str, Any]) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise RuntimeError("MCP stdio 连接未建立")
        line = json.dumps(payload, ensure_ascii=False) + "\n"
        self._proc.stdin.write(line.encode("utf-8"))
        await self._proc.stdin.drain()

    async def _read_loop(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        try:
            while not self._closed:
                raw = await self._proc.stdout.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                await self._dispatch(msg)
        except asyncio.CancelledError:
            pass
        except Exception as e:  # noqa: BLE001
            logger.debug("MCP 读取循环结束: %s", e)
        finally:
            self._fail_pending(f"MCP stdio 通道已关闭（server={self.config.name}）")

    async def disconnect(self) -> None:
        self._closed = True
        if self._reader_task is not None:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._reader_task = None
        if self._proc is not None:
            try:
                if self._proc.returncode is None:
                    self._proc.terminate()
                    await asyncio.wait_for(self._proc.wait(), timeout=5)
            except (TimeoutError, ProcessLookupError, Exception):  # noqa: BLE001
                try:
                    self._proc.kill()
                except Exception:  # noqa: BLE001
                    pass
            self._proc = None


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
            await asyncio.wait_for(self._endpoint_ready.wait(), timeout=20.0)
        except TimeoutError as e:
            await self.disconnect()
            raise RuntimeError(
                f"MCP server '{cfg.name}' 未在 20s 内下发 SSE endpoint"
            ) from e

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
        cfg = self.config
        try:
            async with self._client.stream(
                "GET",
                cfg.url,
                headers=self._headers({"Accept": "text/event-stream"}),
            ) as resp:
                resp.raise_for_status()
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
                logger.info(
                    "MCP SSE endpoint（server=%s）: %s", self.config.name, self._post_url
                )
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
        resp = await self._client.post(
            post_url,
            json=payload,
            headers=self._headers(
                {
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream",
                    # 强制每请求新连接：避免复用 keep-alive 连接导致网关会话失效
                    "Connection": "close",
                }
            ),
        )
        if resp.status_code >= 400:
            raise RuntimeError(
                f"MCP SSE POST 失败（{resp.status_code}）: {resp.text[:200]}"
            )
        # 兼容 streamable-http：POST 直接返回 JSON-RPC 响应
        ctype = resp.headers.get("content-type", "")
        if "application/json" in ctype:
            try:
                msg = resp.json()
            except Exception:  # noqa: BLE001
                msg = None
            if isinstance(msg, dict) and ("result" in msg or "error" in msg):
                await self._dispatch(msg)

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
        """从配置文件加载 server 配置（不连接）。"""
        for path in self._config_paths:
            if os.path.exists(path):
                try:
                    with open(path, encoding="utf-8") as f:
                        data = json.load(f)
                except (json.JSONDecodeError, OSError) as e:
                    logger.warning("读取 MCP 配置失败 %s: %s", path, e)
                    continue
                servers = data.get("mcpServers", data) if isinstance(data, dict) else {}
                if not isinstance(servers, dict):
                    continue
                self._configs.clear()
                for name, raw in servers.items():
                    if not isinstance(raw, dict):
                        continue
                    raw = dict(raw)
                    raw.setdefault("name", name)
                    try:
                        self._configs[name] = parse_server_config(raw)
                    except ValueError as e:
                        logger.warning("跳过无效 MCP 配置 %s: %s", name, e)
                logger.info("已从 %s 加载 %d 个 MCP server", path, len(self._configs))
                return self._configs
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

    def add_server(self, cfg: MCPServerConfig) -> None:
        """添加一个 server 配置并持久化。"""
        if cfg.name in self._servers:
            raise RuntimeError(f"server '{cfg.name}' 已连接，请先断开")
        self._configs[cfg.name] = cfg
        self._save()

    def remove_server(self, name: str) -> None:
        """移除一个 server 配置并持久化。"""
        self._configs.pop(name, None)
        self._save()

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

    async def call_tool(
        self, server_name: str, tool_name: str, arguments: dict[str, Any]
    ) -> str:
        conn = self._servers.get(server_name)
        if conn is None:
            raise RuntimeError(f"MCP server '{server_name}' 未连接")
        return await conn.call_tool(tool_name, arguments)

    def is_connected(self, server_name: str) -> bool:
        return server_name in self._servers
