"""Theme Switcher 工具插件 —— 切换前端主题色。"""

from __future__ import annotations

import logging
from typing import Any

from harness.kernel.context import PluginContext
from harness.kernel.contracts.base import BasePlugin, PluginManifest
from harness.kernel.contracts.tool import ToolPlugin

logger = logging.getLogger("harness.tools.theme_switcher")

VALID_THEMES = ["matrix", "ocean", "sunset", "dark", "light"]

THEME_INFO = {
    "matrix": "黑客帝国（黑底绿字）",
    "ocean": "深海蓝（深蓝青色系）",
    "sunset": "日落紫红（紫红暖色系）",
    "dark": "中性深灰（蓝紫点缀）",
    "light": "亮色（白底绿字）",
}

class ThemeSwitcherTool(ToolPlugin):
    """主题切换工具。"""

    @property
    def tool_name(self) -> str:
        return "theme_switcher"

    @property
    def description(self) -> str:
        return (
            "切换前端界面主题颜色。可选主题: matrix(黑客帝国黑绿)、"
            "ocean(深海蓝)、sunset(日落紫红)、dark(中性深灰)、light(亮色)。"
            "当用户要求换肤、改主题、换个颜色风格时使用此工具。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "theme": {
                    "type": "string",
                    "description": "主题名称",
                    "enum": VALID_THEMES,
                },
            },
            "required": ["theme"],
        }

    async def execute(self, args: dict[str, Any]) -> str:
        theme = args.get("theme", "").strip().lower()
        if theme not in VALID_THEMES:
            return f"错误: 未知主题 '{theme}'，可选: {', '.join(VALID_THEMES)}"
        desc = THEME_INFO.get(theme, theme)
        return f"__THEME__:{theme}\n已切换主题为 {desc}。"

class ThemeSwitcherPlugin(BasePlugin):
    """Theme Switcher 插件。"""

    manifest: PluginManifest
    _ctx: PluginContext | None = None

    def __init__(self) -> None:
        self._ctx = None

    async def activate(self, ctx: PluginContext) -> None:
        self._ctx = ctx
        from harness.engine.tool_registry import ToolRegistry

        if not ctx.services.has(ToolRegistry):
            ctx.services.register(ToolRegistry, ToolRegistry(), owner=self.plugin_id)
        tool_registry = ctx.services.get(ToolRegistry)
        tool_registry.register(ThemeSwitcherTool(), owner=self.plugin_id)
        ctx.logger.info("Theme Switcher 工具已注册")

    async def deactivate(self, ctx: PluginContext) -> None:
        from harness.engine.tool_registry import ToolRegistry

        try:
            tool_registry = ctx.services.get(ToolRegistry)
            tool_registry.unregister_all(self.plugin_id)
        except Exception:
            pass
        ctx.logger.info("Theme Switcher 工具已注销")