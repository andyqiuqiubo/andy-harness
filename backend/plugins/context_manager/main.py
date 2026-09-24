"""context-manager 插件 —— 上下文管理服务。

注册 ContextService 到 ServiceRegistry，供 AgentEngine 调用。
策略可从配置选择，运行时可切换。
"""

from __future__ import annotations

import logging

from harness.kernel.context import PluginContext
from harness.kernel.contracts.base import BasePlugin, PluginManifest
from harness.modules.context_manager.service import (
    ContextService,
    ContextServiceImpl,
    SlidingWindowStrategy,
    SummaryCompressionStrategy,
)

logger = logging.getLogger("harness.plugin.context_manager")


class ContextManagerPlugin(BasePlugin):
    """上下文管理插件。"""

    manifest: PluginManifest
    _ctx: PluginContext | None = None
    _service: ContextServiceImpl | None = None

    def __init__(self) -> None:
        self._ctx = None
        self._service = None

    async def activate(self, ctx: PluginContext) -> None:
        """激活：根据配置选择策略并注册 ContextService。"""
        self._ctx = ctx

        # 从配置选择策略
        strategy_name = ctx.config.get("strategy", "sliding_window")
        max_messages = ctx.config.get("max_messages", 20)

        if strategy_name == "summary_compression":
            strategy = SummaryCompressionStrategy()
        else:
            strategy = SlidingWindowStrategy(max_messages=max_messages)

        # 系统提示词模板
        system_prompt = ctx.config.get("system_prompt", "")

        self._service = ContextServiceImpl(
            services=ctx.services,
            strategy=strategy,
            system_prompt_template=system_prompt,
        )

        ctx.services.register(ContextService, self._service, owner=self.plugin_id)
        ctx.logger.info("context-manager 已激活（策略=%s）", strategy_name)

    async def deactivate(self, ctx: PluginContext) -> None:
        """停用。"""
        self._service = None
        ctx.logger.info("context-manager 已停用")
