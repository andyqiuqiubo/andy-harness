"""REST API —— MCP 客户端管理。

列出已配置的 MCP server 与工具、刷新重连、增删 server 配置。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from harness.api.errors import APIError
from harness.kernel.services import ServiceRegistry
from harness.modules.mcp_client.service import (
    MCPClientService,
    parse_server_config,
)

logger = logging.getLogger("harness.api.mcp")

router = APIRouter(prefix="/api/mcp", tags=["mcp"])


def _get_mcp_service(registry: ServiceRegistry) -> MCPClientService | None:
    try:
        svc = registry.get(MCPClientService)
    except Exception:
        return None
    return svc if isinstance(svc, MCPClientService) else None


class ServerAdd(BaseModel):
    """新增 MCP server 配置。

    - stdio：`command` + `args`
    - sse：`url`（可带 `headers`）
    """

    name: str
    type: str = "stdio"
    command: str = ""
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    url: str = ""
    headers: dict[str, str] = Field(default_factory=dict)
    description: str = ""
    enabled: bool = True


def setup_mcp_routes(registry: ServiceRegistry) -> None:
    """注册 MCP 管理路由。"""

    @router.get("/servers", summary="列出已配置的 MCP server")
    async def list_servers() -> dict[str, Any]:
        svc = _get_mcp_service(registry)
        if svc is None:
            return {"available": False, "servers": [], "count": 0}
        servers = []
        for name, cfg in svc.get_configs().items():
            servers.append(
                {
                    "name": name,
                    "type": cfg.type,
                    "command": cfg.command,
                    "args": cfg.args,
                    "url": cfg.url,
                    "description": cfg.description,
                    "enabled": cfg.enabled,
                    "connected": svc.is_connected(name),
                    "tools": [t.name for t in svc.list_tools() if t.server == name],
                }
            )
        return {"available": True, "servers": servers, "count": len(servers)}

    @router.get("/tools", summary="列出所有已连接的 MCP 工具")
    async def list_tools() -> dict[str, Any]:
        svc = _get_mcp_service(registry)
        if svc is None:
            return {"available": False, "tools": [], "count": 0}
        tools = [
            {
                "server": t.server,
                "name": t.name,
                "registered_as": f"mcp__{t.server}__{t.name}",
                "description": t.description,
            }
            for t in svc.list_tools()
        ]
        return {"available": True, "tools": tools, "count": len(tools)}

    @router.post("/refresh", summary="重新读取配置并重连所有 server")
    async def refresh() -> dict[str, Any]:
        svc = _get_mcp_service(registry)
        if svc is None:
            raise APIError("MCP_SERVICE_UNAVAILABLE", "MCP 客户端未启用", 503)
        await svc.disconnect_all()
        svc.load_config()
        specs = await svc.connect_all()
        return {
            "connected": list(specs.keys()),
            "tool_count": sum(len(v) for v in specs.values()),
        }

    @router.post("/servers", summary="新增 MCP server 配置并重连")
    async def add_server(body: ServerAdd) -> dict[str, Any]:
        svc = _get_mcp_service(registry)
        if svc is None:
            raise APIError("MCP_SERVICE_UNAVAILABLE", "MCP 客户端未启用", 503)
        try:
            cfg = parse_server_config(body.model_dump())
        except ValueError as e:
            raise APIError("MCP_CONFIG_INVALID", str(e), 400) from e
        try:
            svc.add_server(cfg)
        except RuntimeError as e:
            raise APIError("MCP_SERVER_EXISTS", str(e), 409) from e
        try:
            tools = await svc.connect_server(cfg)
        except Exception as e:  # noqa: BLE001
            logger.error("连接新增的 MCP server '%s' 失败: %s", cfg.name, e)
            return {"name": cfg.name, "connected": False, "error": str(e)}
        return {"name": cfg.name, "connected": True, "tools": [t.name for t in tools]}

    @router.delete("/servers/{name}", summary="移除 MCP server 配置")
    async def delete_server(name: str) -> dict[str, Any]:
        svc = _get_mcp_service(registry)
        if svc is None:
            raise APIError("MCP_SERVICE_UNAVAILABLE", "MCP 客户端未启用", 503)
        await svc.disconnect_server(name)
        svc.remove_server(name)
        return {"removed": name}
