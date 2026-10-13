"""IP Lookup 工具插件 —— 查询 IP 地理位置。"""

from __future__ import annotations

import json
import logging
from typing import Any

from harness.infra.crypto import redact_secrets
from harness.infra.net_guard import safe_fetch_text
from harness.kernel.context import PluginContext
from harness.kernel.contracts.base import BasePlugin, PluginManifest
from harness.kernel.contracts.tool import ToolPlugin

logger = logging.getLogger("harness.tools.ip_lookup")


class IpLookupTool(ToolPlugin):
    @property
    def tool_name(self) -> str:
        return "ip_lookup"

    @property
    def risk_level(self) -> str:
        return "read"

    @property
    def description(self) -> str:
        return "查询 IP 地址的地理位置和运营商信息。当用户询问某个 IP 归属地、IP 定位时使用。"

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
            # 走统一 SSRF 护栏：校验 host + 逐跳重定向 + 响应体积上限，避免用户可控
            # 路径段 / 第三方 302 跳转到内网或云元数据地址（SEC-34 / P2 #40）。
            _final_url, raw = await safe_fetch_text(url, timeout=10, max_bytes=64 * 1024)
            data = json.loads(raw)
        except ValueError as e:
            # SSRF 拦截 / 重定向超限 / 响应超限：明确告知模型被安全策略拒绝
            logger.warning("ip_lookup 请求被安全策略拒绝: %s (%s)", redact_secrets(url), e)
            return f"错误: 该地址被安全策略拒绝（{e}）。请换用公开可访问的地址。"
        except json.JSONDecodeError as e:
            return f"查询失败: 响应解析失败（{e}）"
        except Exception as e:
            return f"查询失败: {redact_secrets(str(e))}"
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
