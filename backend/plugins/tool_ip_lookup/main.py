"""IP Lookup 工具插件 —— 查询 IP 地理位置。"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from harness.kernel.context import PluginContext
from harness.kernel.contracts.base import BasePlugin, PluginManifest
from harness.kernel.contracts.tool import ToolPlugin

logger = logging.getLogger("harness.tools.ip_lookup")

class IpLookupTool(ToolPlugin):
    @property
    def tool_name(self) -> str:
        return "ip_lookup"

    @property
    def description(self) -> str:
        return (
            "查询 IP 地址的地理位置和运营商信息。"
            "当用户询问某个 IP 归属地、IP 定位时使用。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "ip": {
                    "type": "string",
                    "description": "要查询的 IP 地址(不填则查询当前出口IP)",
                },
            },
        }

    async def execute(self, args: dict[str, Any]) -> str:
        ip = args.get("ip", "").strip()
        url = f"http://ip-api.com/json/{ip}?lang=zh-CN" if ip else "http://ip-api.com/json/?lang=zh-CN"
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(url)
                data = resp.json()
            if data.get("status") != "success":
                return f"查询失败: {data.get('message', '未知错误')}"
            lines = [
                f"IP: {data.get('query', ip)}",
                f"国家: {data.get('country', '')} {data.get('countryCode', '')}",
                f"省份: {data.get('regionName', '')}",
                f"城市: {data.get('city', '')}",
                f"运营商: {data.get('isp', '')}",
                f"时区: {data.get('timezone', '')}",
                f"坐标: {data.get('lat', '')}, {data.get('lon', '')}",
            ]
            return "\n".join(lines)
        except Exception as e:
            return f"查询失败: {e}"

class IpLookupPlugin(BasePlugin):
    manifest: PluginManifest
    _ctx: PluginContext | None = None

    def __init__(self) -> None:
        self._ctx = None

    async def activate(self, ctx: PluginContext) -> None:
        self._ctx = ctx
        from harness.engine.tool_registry import ToolRegistry
        if not ctx.services.has(ToolRegistry):
            ctx.services.register(ToolRegistry, ToolRegistry(), owner=self.plugin_id)
        ctx.services.get(ToolRegistry).register(IpLookupTool(), owner=self.plugin_id)
        ctx.logger.info("IP Lookup 工具已注册")

    async def deactivate(self, ctx: PluginContext) -> None:
        from harness.engine.tool_registry import ToolRegistry
        try:
            ctx.services.get(ToolRegistry).unregister_all(self.plugin_id)
        except Exception:
            pass