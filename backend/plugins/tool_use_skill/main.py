"""use_skill 工具插件 —— 按需加载 Skill（L2 正文 + L3 资源清单）。

对应 Agent Skills 开放标准的渐进式披露：L1（目录）常驻 system prompt，
模型命中后调用本工具取回 L2 正文，脚本 / 参考文档等 L3 资源按需再读。
"""

from __future__ import annotations

import logging
from typing import Any

from harness.kernel.context import PluginContext
from harness.kernel.contracts.base import BasePlugin, PluginManifest
from harness.kernel.contracts.tool import ToolPlugin
from harness.kernel.services import ServiceRegistry
from harness.modules.skill_manager.service import SkillService

logger = logging.getLogger("harness.tools.use_skill")


class UseSkillTool(ToolPlugin):
    """加载 Skill 的工具。"""

    def __init__(self, services: ServiceRegistry) -> None:
        self._services = services

    @property
    def tool_name(self) -> str:
        return "use_skill"

    @property
    def risk_level(self) -> str:
        """仅读取本地 SKILL.md 与其随附资源，无副作用。"""
        return "read"

    @property
    def description(self) -> str:
        return (
            "加载一个 Skill（标准作业流程）的完整说明。\n"
            "当用户任务匹配某个可用 Skill 的描述时使用它，传入 Skill 的 name。\n"
            "返回该 Skill 的完整步骤、规范，以及可按需读取的脚本/参考文档清单。\n"
            "加载后请严格按其中的步骤执行任务。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "要加载的 Skill 名称（即目录中列出的 name）",
                },
                "resource": {
                    "type": "string",
                    "description": (
                        "可选。要读取的随附资源相对路径，"
                        "如 'references/api.md'，仅在正文要求时才读"
                    ),
                },
            },
            "required": ["name"],
        }

    async def execute(self, args: dict[str, Any]) -> str:
        """加载 Skill 正文（或读取其资源）。"""
        name = str(args.get("name", "")).strip()
        if not name:
            return "错误: 未提供 Skill 名称"

        if not self._services.has(SkillService):
            return "错误: Skill 服务不可用（skill_manager 插件未激活）"

        service: SkillService = self._services.get(SkillService)

        # 指定了资源 → 读取 L3
        resource = str(args.get("resource", "")).strip()
        if resource:
            return service.read_resource(name, resource)

        meta = service.get_skill(name)
        if meta is None:
            names = [m.name for m in service.list_skills() if m.enabled]
            hint = "、".join(names) if names else "（当前无可用 Skill）"
            return f"错误: 未找到 Skill '{name}'。可用 Skill: {hint}"

        if not meta.enabled:
            return f"错误: Skill '{name}' 已被停用，请在设置 → Skills 中启用。"

        body = service.load_body(name)
        if not body:
            return f"错误: Skill '{name}' 正文为空或读取失败。"

        resources = service.list_resources(name)
        lines = [body, "", "---", "## 随附资源（尚未加载，按需读取）"]
        if resources:
            for rel in resources:
                lines.append(f"- {rel}")
            lines.append("")
            lines.append(
                f"读取方式：再次调用 use_skill，"
                f"传 name='{name}' 与 resource='<上面的相对路径>'。"
            )
            lines.append(
                "若某资源是脚本且正文要求执行它，用你已有的文件读取/代码执行工具处理，"
                f"Skill 目录绝对路径: {meta.path}"
            )
        else:
            lines.append("- （无随附资源）")

        return "\n".join(lines)


class UseSkillPlugin(BasePlugin):
    """use_skill 工具插件。"""

    manifest: PluginManifest
    _ctx: PluginContext | None = None

    def __init__(self) -> None:
        self._ctx = None

    async def activate(self, ctx: PluginContext) -> None:
        """激活：注册工具到 ToolRegistry。"""
        self._ctx = ctx

        from harness.engine.tool_registry import ToolRegistry

        if not ctx.services.has(ToolRegistry):
            ctx.services.register(ToolRegistry, ToolRegistry(), owner=self.plugin_id)
        tool_registry = ctx.services.get(ToolRegistry)

        tool_registry.register(UseSkillTool(ctx.services), owner=self.plugin_id)
        ctx.logger.info("use_skill 工具已注册")

    async def deactivate(self, ctx: PluginContext) -> None:
        """停用：注销工具。"""
        from harness.engine.tool_registry import ToolRegistry

        try:
            tool_registry = ctx.services.get(ToolRegistry)
            tool_registry.unregister_all(self.plugin_id)
        except Exception:
            pass
        ctx.logger.info("use_skill 工具已注销")
