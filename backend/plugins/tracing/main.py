"""tracing 插件 —— 运行轨迹。

激活时：
1. 注册 `SpanService`（span 落库 + 按 trace 聚合）；
2. 订阅事件总线 topic `trace.span`，把引擎发布的 span 持久化。
"""

from __future__ import annotations

from typing import Any

from harness.infra.database import Database
from harness.kernel.context import PluginContext
from harness.kernel.contracts.base import BasePlugin, PluginManifest
from harness.modules.tracing.service import SpanService, SpanServiceImpl

# 引擎发布 payload 中的已知字段；其余键统一收进 meta
_KNOWN_FIELDS = {
    "trace_id",
    "parent_id",
    "session_id",
    "name",
    "kind",
    "status",
    "duration_ms",
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "input_preview",
    "output_preview",
    "error",
}


class TracingPlugin(BasePlugin):
    """运行轨迹插件。"""

    manifest: PluginManifest
    _ctx: PluginContext | None = None
    _service: SpanServiceImpl | None = None
    _sub_id: str | None = None

    def __init__(self) -> None:
        self._ctx = None
        self._service = None
        self._sub_id = None

    async def activate(self, ctx: PluginContext) -> None:
        """激活：注册服务并订阅 trace.span 事件。"""
        self._ctx = ctx
        try:
            db = ctx.services.get(Database)
        except Exception:
            db = Database()
        self._service = SpanServiceImpl(db)
        ctx.services.register(SpanService, self._service, owner=self.plugin_id)
        self._sub_id = await ctx.events.subscribe("trace.span", self._on_trace_span, owner=self.plugin_id)
        ctx.logger.info("tracing 已激活（订阅 topic: trace.span）")

    async def deactivate(self, ctx: PluginContext) -> None:
        """停用：注销订阅（loader 也会按 owner 清理）。"""
        self._service = None
        self._sub_id = None
        ctx.logger.info("tracing 已停用")

    async def _on_trace_span(self, data: dict[str, Any]) -> None:
        """把引擎发布的一条 span 落库。"""
        if self._service is None:
            return
        try:

            def _int(key: str) -> int:
                try:
                    return int(data.get(key) or 0)
                except (TypeError, ValueError):
                    return 0

            meta = {k: v for k, v in data.items() if k not in _KNOWN_FIELDS}
            self._service.record(
                trace_id=str(data.get("trace_id") or "unknown"),
                parent_id=data.get("parent_id"),
                session_id=str(data.get("session_id") or ""),
                name=str(data.get("name") or "span"),
                kind=str(data.get("kind") or "span"),
                status=str(data.get("status") or "ok"),
                duration_ms=_int("duration_ms"),
                prompt_tokens=_int("prompt_tokens"),
                completion_tokens=_int("completion_tokens"),
                total_tokens=_int("total_tokens"),
                input_preview=str(data.get("input_preview") or ""),
                output_preview=str(data.get("output_preview") or ""),
                error=str(data.get("error") or ""),
                meta=meta,
            )
        except Exception as e:  # noqa: BLE001
            if self._ctx is not None:
                self._ctx.logger.warning("记录 trace span 失败: %s", e)
