"""skill-manager 插件 —— Skill 管理服务。

扫描 SKILL.md（内置 / 用户级 / 插件自带），注册 SkillService 到 ServiceRegistry。
"""

from __future__ import annotations

import logging
from pathlib import Path

from harness.kernel.context import PluginContext
from harness.kernel.contracts.base import BasePlugin, PluginManifest
from harness.modules.skill_manager.service import (
    SkillService,
    SkillServiceImpl,
    default_plugin_dirs,
    default_skill_roots,
)

logger = logging.getLogger("harness.plugin.skill_manager")


class SkillManagerPlugin(BasePlugin):
    """Skill 管理插件。"""

    manifest: PluginManifest
    _ctx: PluginContext | None = None
    _service: SkillServiceImpl | None = None

    def __init__(self) -> None:
        self._ctx = None
        self._service = None

    async def activate(self, ctx: PluginContext) -> None:
        """激活：扫描 Skill 并注册 SkillService。"""
        self._ctx = ctx

        extra_roots_raw = ctx.config.get("extra_roots", "")
        roots = default_skill_roots()
        if isinstance(extra_roots_raw, str) and extra_roots_raw.strip():
            roots = roots + [Path(p.strip()) for p in extra_roots_raw.split(";") if p.strip()]

        self._service = SkillServiceImpl(roots=roots, plugin_dirs=default_plugin_dirs())
        ctx.services.register(SkillService, self._service, owner=self.plugin_id)

        ctx.logger.info("skill-manager 已激活，发现 %d 个 Skill", len(self._service.list_skills()))

    async def deactivate(self, ctx: PluginContext) -> None:
        """停用：注销服务。"""
        self._service = None
        ctx.logger.info("skill-manager 已停用")
