"""permission-manager 插件 —— 工具权限分级与人工确认。

注册 PermissionService；AgentLoop 在每次工具调用前查询该服务，
需要确认时通过回调（由接入层如 WebSocket 提供）暂停等待用户决定。
"""

from __future__ import annotations

import logging

from harness.engine.tool_registry import ToolRegistry
from harness.kernel.context import PluginContext
from harness.kernel.contracts.base import BasePlugin, PluginManifest
from harness.modules.permission_manager.service import (
    MODE_CONFIRM_DANGEROUS,
    PermissionService,
    PermissionServiceImpl,
)

logger = logging.getLogger("harness.plugin.permission_manager")


class PermissionManagerPlugin(BasePlugin):
    """权限管理插件。"""

    manifest: PluginManifest
    _ctx: PluginContext | None = None
    _service: PermissionServiceImpl | None = None

    def __init__(self) -> None:
        self._ctx = None
        self._service = None

    async def activate(self, ctx: PluginContext) -> None:
        """激活：注册 PermissionService。"""
        self._ctx = ctx

        tool_registry = None
        if ctx.services.has(ToolRegistry):
            tool_registry = ctx.services.get(ToolRegistry)
        else:
            logger.warning("ToolRegistry 未注册，权限服务将以最保守等级工作")

        mode = ctx.config.get("mode", "") or MODE_CONFIRM_DANGEROUS
        self._service = PermissionServiceImpl(
            tool_registry=tool_registry, mode=mode
        )
        ctx.services.register(
            PermissionService, self._service, owner=self.plugin_id
        )
        ctx.logger.info(
            "permission-manager 已激活（策略: %s）", self._service.get_mode()
        )

    async def deactivate(self, ctx: PluginContext) -> None:
        """停用：注销服务。"""
        self._service = None
        ctx.logger.info("permission-manager 已停用")
