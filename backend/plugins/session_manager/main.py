"""session-manager 插件 —— 会话管理服务。

注册 SessionService 到 ServiceRegistry，供 AgentEngine 等模块调用。
"""

from __future__ import annotations

import logging

from harness.infra.database import Database
from harness.kernel.context import PluginContext
from harness.kernel.contracts.base import BasePlugin, PluginManifest
from harness.modules.session_manager.service import SessionService, SessionServiceImpl

logger = logging.getLogger("harness.plugin.session_manager")


class SessionManagerPlugin(BasePlugin):
    """会话管理插件。"""

    manifest: PluginManifest
    _ctx: PluginContext | None = None
    _service: SessionServiceImpl | None = None
    _db: Database | None = None

    def __init__(self) -> None:
        self._ctx = None
        self._service = None
        self._db = None

    async def activate(self, ctx: PluginContext) -> None:
        """激活：创建数据库连接并注册 SessionService。"""
        self._ctx = ctx

        # 从配置获取数据库路径
        db_path = ctx.config.get("db_path", "")
        self._db = Database(db_path) if db_path else Database()

        self._service = SessionServiceImpl(self._db)
        ctx.services.register(SessionService, self._service, owner=self.plugin_id)

        ctx.logger.info("session-manager 已激活")

    async def deactivate(self, ctx: PluginContext) -> None:
        """停用：关闭数据库连接并注销服务。"""
        if self._db:
            self._db.close()
        self._service = None
        self._db = None
        ctx.logger.info("session-manager 已停用")
