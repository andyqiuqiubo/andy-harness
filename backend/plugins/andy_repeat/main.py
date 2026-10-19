"""andy_repeat 插件入口：装配 DesktopRepeatService + 10 个 Agent 工具。

激活流程（§2.5）：
1. 读 plugin.json config → RepeatConfig；
2. 定位 artifact 根（HARNESS_ARTIFACTS_DIR / 默认）→ <root>/repeat/sessions；
3. 注册 DesktopRepeatService（供 REST / 工具 / 调度器复用）；
4. 注册 10 个工具到 ToolRegistry；
5. 依赖缺失（pynput/uiautomation/mss）：插件保持激活、service.status().available=False，
   工具返回明确降级提示而非抛栈（"缺失时不阻塞核心对话"原则）。
"""

from __future__ import annotations

import logging
from pathlib import Path

from harness.engine.tool_registry import ToolRegistry
from harness.kernel.context import PluginContext
from harness.kernel.contracts.base import BasePlugin, PluginManifest
from harness.modules.desktop_repeat.config import RepeatConfig
from harness.modules.desktop_repeat.service import DesktopRepeatService

logger = logging.getLogger("harness.plugin.andy_repeat")


def _sessions_root() -> Path:
    from harness.modules.artifact_store.service import artifacts_dir

    p = Path(artifacts_dir()) / "repeat" / "sessions"
    p.mkdir(parents=True, exist_ok=True)
    return p


class AndyRepeatPlugin(BasePlugin):
    manifest: PluginManifest

    def __init__(self) -> None:
        self._ctx: PluginContext | None = None
        self._service: DesktopRepeatService | None = None

    async def activate(self, ctx: PluginContext) -> None:
        self._ctx = ctx
        cfg = RepeatConfig.load(ctx.config.as_dict())
        try:
            from harness.main import _services  # 复用内核单例注册（含 WorkflowService）
            services = _services or ctx.services
        except Exception:  # noqa: BLE001
            services = ctx.services

        wfsvc = None
        try:
            from harness.modules.workflows.service import WorkflowService

            if services.has(WorkflowService):
                wfsvc = services.get(WorkflowService)
        except Exception:  # noqa: BLE001
            pass
        # WorkflowService 并非注册服务，而是像 workflows REST 那样按 (db, 内核注册表)
        # 现场构造——否则「发布为工作流」拿不到服务（RP-7003）。
        if wfsvc is None:
            try:
                from harness.infra.database import Database as _DB
                from harness.modules.workflows.service import WorkflowService as _WFS

                wfsvc = _WFS(_DB(), services)
            except Exception:  # noqa: BLE001
                wfsvc = None
        skillsvc = None
        try:
            from harness.modules.skill_manager.service import SkillService

            if services.has(SkillService):
                skillsvc = services.get(SkillService)
        except Exception:  # noqa: BLE001
            pass

        notify = None
        try:
            from harness.infra.notify import NotifyService

            notify = NotifyService()
        except Exception:  # noqa: BLE001
            pass

        from harness.infra.database import Database

        db = Database()
        self._service = DesktopRepeatService(db, _sessions_root(), cfg,
                                             workflow_service=wfsvc,
                                             skill_service=skillsvc,
                                             notify=notify)
        ctx.services.register(DesktopRepeatService, self._service, owner=self.plugin_id)

        # 工具注册
        try:
            registry = ctx.services.get(ToolRegistry)
        except Exception:  # noqa: BLE001
            registry = None
        if registry is not None:
            from .tools import TOOL_CLASSES

            for cls in TOOL_CLASSES:
                tool = cls(ctx.services)  # type: ignore[abstract]
                registry.register(tool, owner=self.plugin_id)

        st = self._service.status()
        # 帧 GC 周期任务接线（体检 P1-5）：bundle 只增不减的兜底治理。
        try:
            self._service.ensure_maintenance()
        except Exception:  # noqa: BLE001
            ctx.logger.debug("repeat 维护任务挂载失败（不影响核心功能）", exc_info=True)
        if not st.get("available"):
            ctx.logger.warning(
                "andy-repeat 已激活但桌面能力不可用（pynput/uiautomation 未安装或无图形会话）；"
                "工具调用将返回降级提示。安装：pip install 'harness[repeat]'")
        else:
            ctx.logger.info("andy-repeat 已激活（留证档=%s，坐标闸门=%s）",
                            cfg.frame_tier, cfg.coord_gate)

    async def deactivate(self, ctx: PluginContext) -> None:
        if self._service is not None:
            try:
                self._service.shutdown()
            except Exception:  # noqa: BLE001
                pass
        try:
            ctx.services.get(ToolRegistry).unregister_all(self.plugin_id)
        except Exception:  # noqa: BLE001
            pass
        try:
            ctx.services.unregister(DesktopRepeatService, owner=self.plugin_id)
        except Exception:  # noqa: BLE001
            pass
        ctx.logger.info("andy-repeat 已停用")
