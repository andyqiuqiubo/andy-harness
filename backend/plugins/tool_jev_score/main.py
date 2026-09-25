"""Jev Score 工具插件 —— 评分型判断。

在自定义有序等级刻度上打分，返回得分、各等级概率分布和置信度。
刻度等级由 AI 根据场景动态生成，不写死在代码中。
"""

from __future__ import annotations

import logging
from typing import Any

from harness.kernel.context import PluginContext
from harness.kernel.contracts.base import BasePlugin, PluginManifest
from harness.kernel.contracts.tool import ToolPlugin

logger = logging.getLogger("harness.tools.jev_score")


class JevScoreTool(ToolPlugin):
    """Jev 评分型判断工具。"""

    @property
    def tool_name(self) -> str:
        return "jev_score"

    @property
    def description(self) -> str:
        return (
            "在自定义有序等级刻度上打分，返回加权得分、各等级概率分布和置信度。"
            "适用于客户情绪分级、故障严重程度评估、风险等级评估、"
            "任务难度打分、内容质量评级等场景。"
            "刻度支持 2-10 级，每个等级需指定 level(整数)和 desc(描述)。"
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
                    "description": "要评分的问题",
                },
                "scale": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "level": {
                                "type": "integer",
                                "description": "等级编号（从0开始）",
                            },
                            "desc": {
                                "type": "string",
                                "description": "该等级的描述",
                            },
                        },
                    },
                    "description": "有序等级刻度（2-10级），根据场景动态生成",
                },
            },
            "required": ["scene", "question", "scale"],
        }

    async def execute(self, args: dict[str, Any]) -> str:
        """执行评分型判断。"""
        scene = args.get("scene", "").strip()
        question = args.get("question", "").strip()
        scale = args.get("scale", [])

        if not scene:
            return "错误: 未提供场景描述"
        if not question:
            return "错误: 未提供评分问题"
        if not scale or not isinstance(scale, list):
            return "错误: 未提供评分刻度"
        if len(scale) < 2:
            return "错误: 评分刻度至少需要2个等级"
        if len(scale) > 10:
            return f"错误: 评分刻度最多10个等级，当前 {len(scale)} 个"

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
            result = await manager.score(scene, question, scale)
        except Exception as e:
            return f"Jev API 调用失败: {e}"

        # 格式化结果
        score = result.get("score", 0)
        probabilities = result.get("probabilities", {})
        confidence = result.get("confidence", 0)

        lines = ["Jev 评分型判断结果："]
        lines.append(f"场景: {scene}")
        lines.append(f"问题: {question}")
        lines.append(f"最终得分: {score}")
        if probabilities:
            prob_lines = [f"  {k}: {v}" for k, v in probabilities.items()]
            lines.append("各等级概率:")
            lines.extend(prob_lines)
        lines.append(f"置信度: {confidence}")

        return "\n".join(lines)


class JevScorePlugin(BasePlugin):
    """Jev Score 工具插件入口。"""

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
        tool_registry.register(JevScoreTool(), owner=self.plugin_id)
        ctx.logger.info("Jev Score 工具已注册")

    async def deactivate(self, ctx: PluginContext) -> None:
        from harness.engine.tool_registry import ToolRegistry

        try:
            tool_registry = ctx.services.get(ToolRegistry)
            tool_registry.unregister_all(self.plugin_id)
        except Exception:
            pass
        ctx.logger.info("Jev Score 工具已注销")
