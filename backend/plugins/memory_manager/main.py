"""memory-manager 插件 —— 长期记忆服务 + 定时自主总结。"""

from __future__ import annotations

import asyncio
from pathlib import Path

from harness.infra.database import Database
from harness.kernel.context import PluginContext
from harness.kernel.contracts.base import BasePlugin, PluginManifest
from harness.modules.memory_manager.service import MemoryService, MemoryServiceImpl
from harness.modules.memory_manager.summarizer import (
    MemorySummarizer,
    run_summary_loop,
    summary_enabled,
)


class MemoryManagerPlugin(BasePlugin):
    """长期记忆插件。"""

    manifest: PluginManifest
    _ctx: PluginContext | None = None
    _service: MemoryServiceImpl | None = None
    _summarizer: MemorySummarizer | None = None
    _task: asyncio.Task[None] | None = None

    def __init__(self) -> None:
        self._ctx = None
        self._service = None
        self._summarizer = None
        self._task = None

    async def activate(self, ctx: PluginContext) -> None:
        """激活：注册 MemoryService，并启动定时总结后台任务。"""
        self._ctx = ctx
        try:
            db = ctx.services.get(Database)
        except Exception:
            db = Database()
        self._service = MemoryServiceImpl(db)
        ctx.services.register(MemoryService, self._service, owner=self.plugin_id)

        # 定时自主总结（把会话问答压缩成长期记忆）
        state_path = Path(getattr(db, "db_path", "harness.db")).parent / "memory_state.json"
        self._summarizer = MemorySummarizer(ctx.services, state_path)
        ctx.services.register(
            MemorySummarizer, self._summarizer, owner=self.plugin_id
        )
        if summary_enabled():
            self._task = asyncio.create_task(run_summary_loop(self._summarizer))
            ctx.logger.info("memory-manager 已激活（含自动总结）")
        else:
            ctx.logger.info("memory-manager 已激活（自动总结已关闭）")

    async def deactivate(self, ctx: PluginContext) -> None:
        """停用：取消后台任务并注销服务。"""
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None
        self._summarizer = None
        self._service = None
        ctx.logger.info("memory-manager 已停用")
