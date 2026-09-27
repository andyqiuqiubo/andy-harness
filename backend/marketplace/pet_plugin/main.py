"""pet-plugin —— 元气宠物（后端状态条目，从插件市场安装）。

该插件不提供后端服务，仅作为插件管理页中「元气宠物」的启用/停用条目。
其激活状态通过前端 UI 插件的 backend_plugin_id 联动悬浮宠物。
"""

from __future__ import annotations

import logging

from harness.kernel.contracts.base import BasePlugin, PluginManifest
from harness.kernel.context import PluginContext

logger = logging.getLogger("harness.plugin.pet_plugin")


class PetPlugin(BasePlugin):
    """元气宠物状态条目插件。"""

    manifest: PluginManifest

    async def activate(self, ctx: PluginContext) -> None:
        """启用：记录日志即可。"""
        ctx.logger.info("元气宠物已启用")

    async def deactivate(self, ctx: PluginContext) -> None:
        """停用：记录日志即可。"""
        ctx.logger.info("元气宠物已停用")