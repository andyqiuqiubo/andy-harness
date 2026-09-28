"""subagent 插件 —— 子代理委派服务。"""

from __future__ import annotations

from harness.kernel.context import PluginContext
from harness.kernel.contracts.base import BasePlugin, PluginManifest
from harness.modules.subagent.service import SubagentService, SubagentServiceImpl


class SubagentPlugin(BasePlugin):
    """子代理插件。"""

    manifest: PluginManifest
    _ctx: PluginContext | None = None
    _service: SubagentServiceImpl | None = None

    def __init__(self) -> None:
        self._ctx = None
        self._service = None

    async def activate(self, ctx: PluginContext) -> None:
        """激活：注册 SubagentService。"""
        self._ctx = ctx
        self._service = SubagentServiceImpl(ctx.services, ctx.hooks)
        ctx.services.register(SubagentService, self._service, owner=self.plugin_id)
        ctx.logger.info("subagent 已激活")

    async def deactivate(self, ctx: PluginContext) -> None:
        """停用：注销服务。"""
        self._service = None
        ctx.logger.info("subagent 已停用")
