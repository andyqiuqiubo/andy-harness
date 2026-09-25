"""Jev Noul 工具插件 —— 是非型判断。

判断一个命题是否成立，返回 0~1 之间的概率值。
0 代表完全不成立，1 代表完全成立。
命题由 AI 根据场景动态生成，不写死在代码中。
"""

from __future__ import annotations

import logging
from typing import Any

from harness.kernel.context import PluginContext
from harness.kernel.contracts.base import BasePlugin, PluginManifest
from harness.kernel.contracts.tool import ToolPlugin

logger = logging.getLogger("harness.tools.jev_noul")


class JevNoulTool(ToolPlugin):
    """Jev 是非型判断工具。"""

    @property
    def tool_name(self) -> str:
        return "jev_noul"

    @property
    def description(self) -> str:
        return (
            "判断一个命题是否成立，返回 0~1 之间的概率值。"
            "0 代表完全不成立，1 代表完全成立。"
            "适用于敏感内容检测、规则命中判断、单一意图确认、"
            "风险拦截（如是否包含隐私信息）等场景。"
            "注意：调用前需确保 jev_manager 已配置 API Key。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "scene": {
                    "type": "string",
                    "description": "场景描述，提供充分的上下文信息",
                },
                "proposition": {
                    "type": "string",
                    "description": "要判断的命题（陈述句）",
                },
            },
            "required": ["scene", "proposition"],
        }

    async def execute(self, args: dict[str, Any]) -> str:
        """执行是非型判断。"""
        scene = args.get("scene", "").strip()
        proposition = args.get("proposition", "").strip()

        if not scene:
            return "错误: 未提供场景描述"
        if not proposition:
            return "错误: 未提供命题"

        # 获取 JevManager 服务
        try:
            from plugins.jev_manager.main import JevManager

            if self._ctx is None:
                return "错误: 插件未正确激活"

            manager: JevManager = self._ctx.services.get(JevManager)
        except Exception as e:
            return f"错误: Jev Manager 服务未激活: {e}"

        if not manager.is_configured:
            return (
                "Jev API Key 未配置，请先在设置中配置 jev_manager 的 api_key，"
                "或设置环境变量 JEV_API_KEY。"
            )

        try:
            result = await manager.noul(scene, proposition)
        except Exception as e:
            return f"Jev API 调用失败: {e}"

        # 格式化结果
        probability = result.get("probability", 0)

        lines = ["Jev 是非型判断结果："]
        lines.append(f"场景: {scene}")
        lines.append(f"命题: {proposition}")
        lines.append(f"成立概率: {probability}")

        # 添加解读
        if probability >= 0.9:
            lines.append("解读: 命题极大概率成立")
        elif probability >= 0.7:
            lines.append("解读: 命题大概率成立")
        elif probability >= 0.3:
            lines.append("解读: 命题成立与否不确定")
        elif probability >= 0.1:
            lines.append("解读: 命题大概率不成立")
        else:
            lines.append("解读: 命题极大概率不成立")

        return "\n".join(lines)


class JevNoulPlugin(BasePlugin):
    """Jev Noul 工具插件入口。"""

    manifest: PluginManifest
    _ctx: PluginContext | None = None

    def __init__(self) -> None:
        self._ctx = None

    async def activate(self, ctx: PluginContext) -> None:
        self._ctx = ctx
        from harness.engine.tool_registry import ToolRegistry

        if not ctx.services.has(ToolRegistry):
            ctx.services.register(
                ToolRegistry, ToolRegistry(), owner=self.plugin_id
            )
        tool_registry = ctx.services.get(ToolRegistry)
        tool_registry.register(JevNoulTool(), owner=self.plugin_id)
        ctx.logger.info("Jev Noul 工具已注册")

    async def deactivate(self, ctx: PluginContext) -> None:
        from harness.engine.tool_registry import ToolRegistry

        try:
            tool_registry = ctx.services.get(ToolRegistry)
            tool_registry.unregister_all(self.plugin_id)
        except Exception:
            pass
        ctx.logger.info("Jev Noul 工具已注销")
