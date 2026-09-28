"""scheduler 插件 —— 定时任务服务 + 后台调度循环。

激活时注册 `SchedulerService`（任务存储与调度计算）与 `TaskRunner`（执行器），
并启动一个轻量循环：每 `HARNESS_SCHEDULER_TICK` 秒（默认 30s）检查一次到期任务，
**顺序**执行（避免并发打爆模型配额），执行期间用内存集合防止同一任务重入。
"""

from __future__ import annotations

import asyncio
import logging
import os

from harness.infra.database import Database
from harness.kernel.context import PluginContext
from harness.kernel.contracts.base import BasePlugin, PluginManifest
from harness.modules.scheduler.runner import TaskRunner
from harness.modules.scheduler.service import ScheduledTask, SchedulerService

logger = logging.getLogger("harness.plugin.scheduler")


def scheduler_enabled() -> bool:
    return os.environ.get("HARNESS_SCHEDULER", "1") not in ("0", "false", "False")


def scheduler_tick() -> int:
    """检查间隔（秒）。"""
    try:
        return max(5, int(os.environ.get("HARNESS_SCHEDULER_TICK", "30")))
    except ValueError:
        return 30


class SchedulerPlugin(BasePlugin):
    """定时任务插件。"""

    manifest: PluginManifest
    _ctx: PluginContext | None = None
    _service: SchedulerService | None = None
    _runner: TaskRunner | None = None
    _task: asyncio.Task[None] | None = None
    _running: set[str]

    def __init__(self) -> None:
        self._ctx = None
        self._service = None
        self._runner = None
        self._task = None
        self._running = set()

    async def activate(self, ctx: PluginContext) -> None:
        """激活：注册服务/执行器并启动调度循环。"""
        self._ctx = ctx
        try:
            db = ctx.services.get(Database)
        except Exception:
            db = Database()
        self._service = SchedulerService(db)
        self._runner = TaskRunner(ctx.services)

        ctx.services.register(SchedulerService, self._service, owner=self.plugin_id)
        ctx.services.register(TaskRunner, self._runner, owner=self.plugin_id)

        fixed = self._service.refresh_next_runs()
        if scheduler_enabled():
            self._task = asyncio.create_task(self._loop())
            ctx.logger.info(
                "scheduler 已激活（检查间隔 %ds，补齐 %d 个下次运行时间）",
                scheduler_tick(),
                fixed,
            )
        else:
            ctx.logger.info("scheduler 已激活（调度循环已关闭）")

    async def deactivate(self, ctx: PluginContext) -> None:
        """停用：停止循环。"""
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None
        self._runner = None
        self._service = None
        ctx.logger.info("scheduler 已停用")

    # ── 调度循环 ──────────────────────────────────────

    async def _loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(scheduler_tick())
            except asyncio.CancelledError:
                raise
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                logger.warning("调度循环出错（已忽略，下轮重试）: %s", e)

    async def _tick(self) -> None:
        if self._service is None or self._runner is None:
            return
        due = self._service.due_tasks()
        for task in due:
            if task.id in self._running:
                continue
            # 抢占：先把下次运行时间推后，避免执行期间重复触发
            self._service.claim_task(task.id)
            self._running.add(task.id)
            try:
                await self._run_one(task)
            finally:
                self._running.discard(task.id)

    async def _run_one(self, task: ScheduledTask) -> None:
        assert self._runner is not None
        logger.info("定时任务开始: %s（%s）", task.name, task.schedule.describe())
        outcome = await self._runner.run_task(task, trigger="schedule")
        logger.info(
            "定时任务结束: %s → %s（%dms）%s",
            task.name,
            outcome.status,
            outcome.duration_ms,
            outcome.error or "",
        )
