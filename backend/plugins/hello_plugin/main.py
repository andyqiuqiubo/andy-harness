"""hello-plugin —— 示例插件。

注册一个服务（HelloService）+ 发布/订阅事件 + 注册一个 hook。
"""

from __future__ import annotations

import logging
from typing import Any

from harness.kernel.context import PluginContext
from harness.kernel.contracts.base import BasePlugin, PluginManifest
from harness.kernel.contracts.hook import HookContext, HookResult

logger = logging.getLogger("harness.plugin.hello_plugin")


class HelloService:
    """示例服务：提供问候功能。"""

    def greet(self, name: str = "World") -> str:
        """返回问候语。"""
        return f"Hello, {name}! — from hello-plugin"


class HelloPlugin(BasePlugin):
    """示例插件。"""

    manifest: PluginManifest  # 由 PluginLoader 设置

    def __init__(self) -> None:
        self._ctx: PluginContext | None = None

    async def activate(self, ctx: PluginContext) -> None:
        """激活插件：注册服务 + 订阅事件 + 注册 hook。"""
        self._ctx = ctx

        # 1. 注册服务
        ctx.services.register(HelloService, HelloService(), owner=self.plugin_id)

        # 2. 订阅事件
        await ctx.events.subscribe(
            "message.created",
            self._on_message_created,
            owner=self.plugin_id,
        )

        # 3. 注册 hook
        ctx.hooks.register(
            "pre_model_call",
            self._pre_model_call_hook,
            owner=self.plugin_id,
        )

        # 4. 发布一个事件通知已激活
        await ctx.events.publish(
            "plugin.activated",
            {"plugin_id": self.plugin_id, "message": "hello-plugin 已激活"},
        )

        ctx.logger.info("hello-plugin 已激活")

    async def deactivate(self, ctx: PluginContext) -> None:
        """停用插件。"""
        ctx.logger.info("hello-plugin 已停用")
        self._ctx = None

    async def _on_message_created(self, data: dict[str, Any]) -> None:
        """事件处理器：收到 message.created 事件。"""
        ctx = self._ctx
        if ctx:
            ctx.logger.info("收到事件 message.created: %s", data.get("content", ""))

    async def _pre_model_call_hook(self, hook_ctx: HookContext) -> HookResult:
        """钩子处理器：pre_model_call。"""
        ctx = self._ctx
        if ctx:
            ctx.logger.info("钩子 pre_model_call 被触发")

        # 不改写数据，仅记录
        return HookResult(data=hook_ctx.data)
