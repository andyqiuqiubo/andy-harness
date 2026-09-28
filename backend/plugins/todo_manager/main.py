"""todo-manager 插件 —— 会话级任务清单服务。"""

from __future__ import annotations

import logging

from harness.infra.database import Database
from harness.kernel.context import PluginContext
from harness.kernel.contracts.base import BasePlugin, PluginManifest
from harness.modules.todo_manager.service import TodoService, TodoServiceImpl

logger = logging.getLogger("harness.plugin.todo_manager")


class TodoManagerPlugin(BasePlugin):
    """Todo 管理插件。"""

    manifest: PluginManifest
    _ctx: PluginContext | None = None
    _service: TodoServiceImpl | None = None

    def __init__(self) -> None:
        self._ctx = None
        self._service = None

    async def activate(self, ctx: PluginContext) -> None:
        """激活：注册 TodoService。"""
        self._ctx = ctx
        try:
            db = ctx.services.get(Database)
        except Exception:
            db = Database()
        self._service = TodoServiceImpl(db)
        ctx.services.register(TodoService, self._service, owner=self.plugin_id)
        ctx.logger.info("todo-manager 已激活")

    async def deactivate(self, ctx: PluginContext) -> None:
        """停用：注销服务。"""
        self._service = None
        ctx.logger.info("todo-manager 已停用")
