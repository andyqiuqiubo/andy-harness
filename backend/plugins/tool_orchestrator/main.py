"""orchestrator 工具插件 —— 多 Agent 编排（E10）。

提供两个工具：
- `parallel`：一次派生多个互相独立的子代理，**并发执行**，按输入顺序返回
  各自结论。适合"分别调研 A/B/C 后再汇总"。
- `pipeline`：把多个有角色的阶段串行接力（如研究员 → 写作者 → 评审），
  上一阶段产出自动作为下一阶段输入。

每个子任务仍由 SubagentService 在独立临时会话中执行，主上下文只收到编排
结果，不被各子任务的中间过程污染。
"""

from __future__ import annotations

import logging
from typing import Any

from harness.kernel.context import PluginContext
from harness.kernel.contracts.base import BasePlugin, PluginManifest
from harness.kernel.contracts.tool import ToolPlugin
from harness.kernel.services import ServiceRegistry
from harness.modules.orchestrator.service import (
    DEFAULT_MAX_CONCURRENCY,
    MAX_PARALLEL_TASKS,
    MAX_PIPELINE_STAGES,
    OrchestratorService,
    OrchestratorServiceImpl,
    StageSpec,
    SubtaskSpec,
)

logger = logging.getLogger("harness.tools.orchestrator")

# 单个子任务结果的回传长度上限，防止编排结果整体撑爆上下文。
_MAX_RESULT_CHARS = 2000


def _coerce_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class ParallelTool(ToolPlugin):
    """并行子任务编排工具。"""

    def __init__(self, services: ServiceRegistry) -> None:
        self._services = services

    @property
    def tool_name(self) -> str:
        return "parallel"

    @property
    def risk_level(self) -> str:
        return "write"

    @property
    def description(self) -> str:
        return (
            "一次派生**多个互相独立的子代理并发执行**，按输入顺序返回每个子任务的结论。\n"
            "何时使用：有若干互不依赖的子任务（如分别研究不同主题、分别处理多个文件），"
            "想让它们同时跑完再由你汇总。\n"
            "用法：tasks 为数组，每项含 prompt（自包含的子任务描述）；"
            "可选 allow_tools 限定每个子任务可用工具、max_iterations 限定迭代数。\n"
            "注意：各子任务之间不共享上下文、也不能互相依赖；"
            "有依赖关系的接力任务请用 pipeline。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "tasks": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "prompt": {"type": "string"},
                            "allow_tools": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "max_iterations": {"type": "integer"},
                        },
                        "required": ["prompt"],
                    },
                    "description": f"子任务列表（最多 {MAX_PARALLEL_TASKS} 个），按顺序返回",
                },
                "max_concurrency": {
                    "type": "integer",
                    "description": "同时执行的子任务上限，默认 4",
                },
            },
            "required": ["tasks"],
        }

    async def execute(self, args: dict[str, Any]) -> str:
        raw_tasks = args.get("tasks")
        if not isinstance(raw_tasks, list) or not raw_tasks:
            return "错误: 缺少 tasks 参数（非空数组）"
        if len(raw_tasks) > MAX_PARALLEL_TASKS:
            return f"错误: 一次最多 {MAX_PARALLEL_TASKS} 个并行子任务"

        specs: list[SubtaskSpec] = []
        for item in raw_tasks:
            if not isinstance(item, dict):
                return "错误: tasks 中每项必须是对象"
            prompt = str(item.get("prompt") or "").strip()
            if not prompt:
                return "错误: 每个子任务都必须包含非空 prompt"
            allow_tools = item.get("allow_tools")
            if allow_tools is not None and not isinstance(allow_tools, list):
                allow_tools = None
            max_iterations = _coerce_int(item.get("max_iterations"), 6)
            specs.append(
                SubtaskSpec(
                    prompt=prompt,
                    allow_tools=allow_tools,
                    max_iterations=max_iterations,
                )
            )

        max_concurrency = _coerce_int(args.get("max_concurrency"), DEFAULT_MAX_CONCURRENCY)

        if not self._services.has(OrchestratorService):
            return "错误: 编排服务不可用（orchestrator 插件未激活）"
        service: OrchestratorService = self._services.get(OrchestratorService)

        try:
            outcomes = await service.run_parallel(specs, max_concurrency=max_concurrency)
        except ValueError as exc:
            return f"错误: {exc}"

        lines = [f"[并行完成] 共 {len(outcomes)} 个子任务："]
        for outcome in outcomes:
            if outcome.error:
                lines.append(f"\n{outcome.index + 1}. ❌ 失败：{outcome.error}")
                continue
            content = outcome.content or "（未返回内容）"
            if len(content) > _MAX_RESULT_CHARS:
                content = content[:_MAX_RESULT_CHARS] + " …（已截断）"
            lines.append(f"\n{outcome.index + 1}. ✅ 迭代 {outcome.iterations} 次：\n{content}")
        return "\n".join(lines)


class PipelineTool(ToolPlugin):
    """角色流水线编排工具。"""

    def __init__(self, services: ServiceRegistry) -> None:
        self._services = services

    @property
    def tool_name(self) -> str:
        return "pipeline"

    @property
    def risk_level(self) -> str:
        return "write"

    @property
    def description(self) -> str:
        return (
            "把多个有角色的阶段**串行接力**：上一阶段的产出自动作为下一阶段的输入。\n"
            "何时使用：任务有明确的接力环节（如研究员收集信息 → 写作者成稿 → 评审挑错）。\n"
            "用法：stages 为数组，每项含 role（角色名，如 研究员）与 prompt（该阶段"
            "任务说明）；prompt 中的 {input} 会被替换为上一阶段产出（未写占位符时"
            "会自动把上一阶段产出附在末尾）；initial_input 为最初输入。\n"
            "注意：各阶段在独立上下文执行，默认只把最终结果返回给你。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "stages": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "role": {"type": "string"},
                            "prompt": {"type": "string"},
                        },
                        "required": ["role", "prompt"],
                    },
                    "description": f"流水线阶段（最多 {MAX_PIPELINE_STAGES} 个），按顺序接力",
                },
                "initial_input": {
                    "type": "string",
                    "description": "可选，交给第一阶段的初始输入",
                },
            },
            "required": ["stages"],
        }

    async def execute(self, args: dict[str, Any]) -> str:
        raw_stages = args.get("stages")
        if not isinstance(raw_stages, list) or not raw_stages:
            return "错误: 缺少 stages 参数（非空数组）"
        if len(raw_stages) > MAX_PIPELINE_STAGES:
            return f"错误: 流水线最多 {MAX_PIPELINE_STAGES} 个阶段"

        stages: list[StageSpec] = []
        for item in raw_stages:
            if not isinstance(item, dict):
                return "错误: stages 中每项必须是对象"
            role = str(item.get("role") or "").strip()
            prompt = str(item.get("prompt") or "").strip()
            if not role or not prompt:
                return "错误: 每个阶段都必须包含非空 role 与 prompt"
            stages.append(StageSpec(role=role, prompt=prompt))

        initial_input = str(args.get("initial_input") or "")

        if not self._services.has(OrchestratorService):
            return "错误: 编排服务不可用（orchestrator 插件未激活）"
        service: OrchestratorService = self._services.get(OrchestratorService)

        try:
            result = await service.run_pipeline(stages, initial_input=initial_input)
        except ValueError as exc:
            return f"错误: {exc}"

        if not result.stages:
            return "错误: 流水线未产出任何阶段结果"

        lines = [f"[流水线完成] 共 {len(result.stages)} 个阶段："]
        for stage in result.stages:
            if stage.error:
                lines.append(f"\n{stage.index + 1}. {stage.role} ❌ 失败：{stage.error}")
                continue
            lines.append(f"\n{stage.index + 1}. {stage.role} ✅")
        if result.error:
            lines.append(f"\n流水线在某阶段失败：{result.error}")
            return "\n".join(lines)

        final = result.final_content or "（未返回内容）"
        lines.append(f"\n最终产出：\n{final}")
        return "\n".join(lines)


class OrchestratorToolsPlugin(BasePlugin):
    """多 Agent 编排工具插件。"""

    manifest: PluginManifest
    _ctx: PluginContext | None = None

    def __init__(self) -> None:
        self._ctx = None

    async def activate(self, ctx: PluginContext) -> None:
        """激活：注册编排服务 + parallel / pipeline 两个工具。"""
        self._ctx = ctx

        from harness.engine.tool_registry import ToolRegistry

        if not ctx.services.has(ToolRegistry):
            ctx.services.register(ToolRegistry, ToolRegistry(), owner=self.plugin_id)
        tool_registry = ctx.services.get(ToolRegistry)

        # 幂等注册：重复激活时不覆盖已有编排服务。
        if not ctx.services.has(OrchestratorService):
            ctx.services.register(
                OrchestratorService,
                OrchestratorServiceImpl(ctx.services),
                owner=self.plugin_id,
            )

        tool_registry.register(ParallelTool(ctx.services), owner=self.plugin_id)
        tool_registry.register(PipelineTool(ctx.services), owner=self.plugin_id)
        ctx.logger.info("parallel / pipeline 编排工具已注册")

    async def deactivate(self, ctx: PluginContext) -> None:
        """停用：注销工具。"""
        from harness.engine.tool_registry import ToolRegistry

        try:
            tool_registry = ctx.services.get(ToolRegistry)
            tool_registry.unregister_all(self.plugin_id)
        except Exception:
            pass
        ctx.logger.info("orchestrator 编排工具已注销")
