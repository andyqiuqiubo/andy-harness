"""REST API —— MCP 客户端管理。

列出已配置的 MCP server 与工具、刷新重连、增删 server 配置。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from harness.api.deps import require_admin
from harness.api.errors import APIError
from harness.kernel.services import ServiceRegistry
from harness.modules.mcp_client.service import (
    MCPClientService,
    parse_server_config,
)

logger = logging.getLogger("harness.api.mcp")

router = APIRouter(prefix="/api/mcp", tags=["mcp"])

# MCP 市场目录（内置可安装 server 描述）：<backend>/mcp_marketplace/<id>.json
_MCP_MARKETPLACE_DIR = Path(__file__).resolve().parent.parent.parent.parent / "mcp_marketplace"


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

    @router.post("/servers", summary="新增 MCP server 配置并重连", dependencies=[Depends(require_admin)])
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

    @router.delete("/servers/{name}", summary="移除 MCP server 配置", dependencies=[Depends(require_admin)])
    async def delete_server(name: str) -> dict[str, Any]:
        svc = _get_mcp_service(registry)
        if svc is None:
            raise APIError("MCP_SERVICE_UNAVAILABLE", "MCP 客户端未启用", 503)
        await svc.disconnect_server(name)
        svc.remove_server(name)
        return {"removed": name}

    # ── MCP 市场 ──────────────────────────────────────

    @router.get("/marketplace", summary="列出 MCP 市场")
    async def list_mcp_marketplace() -> list[dict[str, Any]]:
        """列出 MCP 市场中可安装的 server（含已安装标记）。

        市场包是描述文件而非代码：安装即写入 mcp.json 并即时连接。
        """
        svc = _get_mcp_service(registry)
        installed = set(svc.get_configs().keys()) if svc else set()

        items: list[dict[str, Any]] = []
        if _MCP_MARKETPLACE_DIR.is_dir():
            for pkg in sorted(_MCP_MARKETPLACE_DIR.glob("*.json")):
                try:
                    meta = json.loads(pkg.read_text(encoding="utf-8"))
                except Exception:  # noqa: BLE001
                    continue
                if not isinstance(meta, dict):
                    continue
                name = str(meta.get("name") or pkg.stem)
                items.append(
                    {
                        "package_id": pkg.stem,
                        "name": name,
                        "type": meta.get("type", "sse"),
                        "url": meta.get("url", ""),
                        "command": meta.get("command", ""),
                        "args": meta.get("args", []),
                        "description": meta.get("description", ""),
                        "long_description": meta.get("long_description", ""),
                        "auth_required": bool(meta.get("auth_required", False)),
                        "installed": name in installed,
                    }
                )
        return items

    @router.post("/marketplace/{package_id}/install", summary="从 MCP 市场安装", dependencies=[Depends(require_admin)])
    async def install_mcp_marketplace(package_id: str) -> dict[str, Any]:
        """把市场描述写入配置并立即连接。"""
        svc = _get_mcp_service(registry)
        if svc is None:
            raise APIError("MCP_SERVICE_UNAVAILABLE", "MCP 客户端未启用", 503)

        pkg = _MCP_MARKETPLACE_DIR / f"{package_id}.json"
        if not pkg.is_file():
            raise APIError("MCP_MARKETPLACE_NOT_FOUND", f"MCP 市场中不存在: {package_id}", 404)
        try:
            meta = json.loads(pkg.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            raise APIError("MCP_MARKETPLACE_INVALID", f"市场描述无效: {e}", 500) from e

        try:
            cfg = parse_server_config(meta)
        except ValueError as e:
            raise APIError("MCP_CONFIG_INVALID", str(e), 400) from e

        if cfg.name in svc.get_configs():
            raise APIError("MCP_SERVER_EXISTS", f"该 MCP server 已安装: {cfg.name}", 409)

        svc.add_server(cfg)
        try:
            tools = await svc.connect_server(cfg)
        except Exception as e:  # noqa: BLE001
            # 配置已写入（用户可稍后在列表里刷新重连），如实返回未连上
            logger.error("连接新增的 MCP server '%s' 失败: %s", cfg.name, e)
            return {
                "name": cfg.name,
                "installed": True,
                "connected": False,
                "error": str(e),
            }
        logger.info("已从 MCP 市场安装: %s", cfg.name)
        return {
            "name": cfg.name,
            "installed": True,
            "connected": True,
            "tools": [t.name for t in tools],
        }

    @router.delete(
        "/marketplace/{package_id}",
        summary="卸载 MCP 市场安装的 server",
        dependencies=[Depends(require_admin)],
    )
    async def uninstall_mcp_marketplace(package_id: str) -> dict[str, Any]:
        svc = _get_mcp_service(registry)
        if svc is None:
            raise APIError("MCP_SERVICE_UNAVAILABLE", "MCP 客户端未启用", 503)

        pkg = _MCP_MARKETPLACE_DIR / f"{package_id}.json"
        if not pkg.is_file():
            raise APIError("MCP_MARKETPLACE_NOT_FOUND", f"MCP 市场中不存在: {package_id}", 404)
        try:
            meta = json.loads(pkg.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            meta = {}

        name = str(meta.get("name") or package_id)
        if name not in svc.get_configs():
            raise APIError("MCP_SERVER_NOT_FOUND", f"该 MCP server 未安装: {name}", 404)

        await svc.disconnect_server(name)
        svc.remove_server(name)
        logger.info("已卸载 MCP 市场安装的 server: %s", name)
        return {"removed": name}
