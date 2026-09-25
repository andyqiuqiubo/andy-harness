"""Jev Choice 工具插件 —— 选择型判断。

从固定选项中选最合适的一个，返回选中项、各选项概率和置信度。
options 完全由 AI 根据场景动态生成，不写死在代码中。
"""

from __future__ import annotations

import logging
from typing import Any

from harness.kernel.context import PluginContext
from harness.kernel.contracts.base import BasePlugin, PluginManifest
from harness.kernel.contracts.tool import ToolPlugin

logger = logging.getLogger("harness.tools.jev_choice")


class JevChoiceTool(ToolPlugin):
    """Jev 选择型判断工具。"""

    @property
    def tool_name(self) -> str:
        return "jev_choice"

    @property
    def description(self) -> str:
        return (
            "从固定选项中做单选题，返回选中项、各选项概率和置信度。"
            "适用于客服工单分流、用户意图分类、智能路由分配、内容标签归类等场景。"
            "当需要从多个候选中选最合适的一个时使用此工具。"
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
                "question": {
                    "type": "string",
                    "description": "要判断的问题",
                },
                "options": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "候选选项列表（最多255个），根据场景动态生成",
                },
            },
            "required": ["scene", "question", "options"],
        }

    async def execute(self, args: dict[str, Any]) -> str:
        """执行选择型判断。"""
        scene = args.get("scene", "").strip()
        question = args.get("question", "").strip()
        options = args.get("options", [])

        if not scene:
            return "错误: 未提供场景描述"
        if not question:
            return "错误: 未提供判断问题"
        if not options or not isinstance(options, list):
            return "错误: 未提供候选选项"
        if len(options) > 255:
            return f"错误: 候选选项最多255个，当前 {len(options)} 个"

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
            result = await manager.choice(scene, question, options)
        except Exception as e:
            return f"Jev API 调用失败: {e}"

        # 格式化结果供 AI 理解
        selected = result.get("selected", "")
        probabilities = result.get("probabilities", {})
        confidence = result.get("confidence", 0)

        lines = ["Jev 选择型判断结果："]
        lines.append(f"场景: {scene}")
        lines.append(f"问题: {question}")
        lines.append(f"选中选项: {selected}")
        if probabilities:
            prob_lines = [f"  {k}: {v}" for k, v in probabilities.items()]
            lines.append("各选项概率:")
            lines.extend(prob_lines)
        lines.append(f"置信度: {confidence}")

        return "\n".join(lines)


class JevChoicePlugin(BasePlugin):
    """Jev Choice 工具插件入口。"""

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
        tool_registry.register(JevChoiceTool(), owner=self.plugin_id)
        ctx.logger.info("Jev Choice 工具已注册")

    async def deactivate(self, ctx: PluginContext) -> None:
        from harness.engine.tool_registry import ToolRegistry

        try:
            tool_registry = ctx.services.get(ToolRegistry)
            tool_registry.unregister_all(self.plugin_id)
        except Exception:
            pass
        ctx.logger.info("Jev Choice 工具已注销")
