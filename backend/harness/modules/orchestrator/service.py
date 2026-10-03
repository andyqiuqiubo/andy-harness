"""多 Agent 编排服务 —— 并行任务与流水线（E10）。

在现有 `SubagentService`（单任务、上下文隔离）之上提供两种编排模式：

- **并行（parallel）**：多个互相独立的子任务同时跑，带并发上限，按输入
  顺序返回结果。适合"分别研究 A / B / C 后汇总"。
- **流水线（pipeline）**：多个有角色的阶段串行，上一阶段的产出作为下一
  阶段的输入。适合"研究员 → 写作者 → 评审"这类接力。

编排本身不实现对话流程：每个子任务仍由 SubagentService 用独立临时会话
的 AgentLoop 执行，因此隔离、压缩、权限、offload 等能力全部保留。
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import cast

from harness.kernel.services import ServiceRegistry
from harness.modules.subagent.service import (
    DEFAULT_MAX_ITERATIONS,
    SubagentService,
)

# 安全护栏：一次编排允许的子任务 / 阶段数上限，防止模型失控派生大量代理。
MAX_PARALLEL_TASKS = 10
MAX_PIPELINE_STAGES = 8
DEFAULT_MAX_CONCURRENCY = 4
# 流水线阶段提示词中的输入占位符。
INPUT_PLACEHOLDER = "{input}"


@dataclass
class SubtaskSpec:
    """一个并行子任务的描述。"""

    prompt: str
    allow_tools: list[str] | None = None
    max_iterations: int = DEFAULT_MAX_ITERATIONS


@dataclass
class SubtaskOutcome:
    """一个并行子任务的结果（按输入顺序返回）。"""

    index: int
    content: str = ""
    iterations: int = 0
    tool_calls: int = 0
    error: str | None = None


@dataclass
class StageSpec:
    """流水线的一个阶段（角色 + 该阶段的提示词）。"""

    role: str
    prompt: str


@dataclass
class StageOutcome:
    """流水线一个阶段的产出。"""

    index: int
    role: str
    content: str = ""
    error: str | None = None


@dataclass
class PipelineResult:
    """整条流水线的结果。"""

    stages: list[StageOutcome] = field(default_factory=list)
    final_content: str = ""

    @property
    def error(self) -> str | None:
        """首个阶段错误（供快速判断）。"""
        for stage in self.stages:
            if stage.error:
                return stage.error
        return None


class OrchestratorService(ABC):
    """多 Agent 编排服务接口。"""

    @abstractmethod
    async def run_parallel(
        self,
        specs: list[SubtaskSpec],
        *,
        max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
    ) -> list[SubtaskOutcome]:
        """并行执行多个独立子任务，按输入顺序返回。"""

    @abstractmethod
    async def run_pipeline(
        self,
        stages: list[StageSpec],
        *,
        initial_input: str = "",
    ) -> PipelineResult:
        """按顺序执行流水线，逐阶段传递产出。"""


class OrchestratorServiceImpl(OrchestratorService):
    """基于 SubagentService 的编排实现。"""

    def __init__(self, services: ServiceRegistry) -> None:
        self._services = services

    def _subagent(self) -> SubagentService | None:
        try:
            return cast(SubagentService, self._services.get(SubagentService))
        except Exception:
            return None

    async def run_parallel(
        self,
        specs: list[SubtaskSpec],
        *,
        max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
    ) -> list[SubtaskOutcome]:
        if not specs:
            return []
        if len(specs) > MAX_PARALLEL_TASKS:
            raise ValueError(f"一次最多并行 {MAX_PARALLEL_TASKS} 个子任务")

        subagent = self._subagent()
        if subagent is None:
            # 子代理服务不可用时，全部任务快速失败（不静默丢任务）。
            return [
                SubtaskOutcome(
                    index=i,
                    error="子代理服务不可用（subagent 插件未激活）",
                )
                for i in range(len(specs))
            ]

        concurrency = max(1, min(max_concurrency, len(specs)))
        sem = asyncio.Semaphore(concurrency)

        async def _run_one(index: int, spec: SubtaskSpec) -> SubtaskOutcome:
            async with sem:
                try:
                    result = await subagent.run_task(
                        spec.prompt,
                        allow_tools=spec.allow_tools,
                        max_iterations=spec.max_iterations,
                    )
                except Exception as exc:  # 单个任务异常不拖垮整批
                    return SubtaskOutcome(index=index, error=str(exc))
                return SubtaskOutcome(
                    index=index,
                    content=result.content,
                    iterations=result.iterations,
                    tool_calls=result.tool_calls,
                    error=result.error,
                )

        outcomes = await asyncio.gather(*(_run_one(i, spec) for i, spec in enumerate(specs)))
        # gather 已按任务顺序返回，这里再显式排序以防实现变化。
        return sorted(outcomes, key=lambda o: o.index)

    async def run_pipeline(
        self,
        stages: list[StageSpec],
        *,
        initial_input: str = "",
    ) -> PipelineResult:
        if not stages:
            return PipelineResult()
        if len(stages) > MAX_PIPELINE_STAGES:
            raise ValueError(f"流水线最多 {MAX_PIPELINE_STAGES} 个阶段")

        subagent = self._subagent()
        if subagent is None:
            return PipelineResult(
                stages=[
                    StageOutcome(
                        index=i,
                        role=stage.role,
                        error="子代理服务不可用（subagent 插件未激活）",
                    )
                    for i, stage in enumerate(stages)
                ],
            )

        outcomes: list[StageOutcome] = []
        current = initial_input or ""
        for i, stage in enumerate(stages):
            prompt = self._render_stage_prompt(stage.prompt, current, first=(i == 0))
            try:
                result = await subagent.run_task(prompt)
            except Exception as exc:
                outcome = StageOutcome(index=i, role=stage.role, error=str(exc))
                outcomes.append(outcome)
                # 阶段失败后，后续阶段无法接力，直接终止流水线。
                break

            content = result.content
            if result.error:
                outcomes.append(StageOutcome(index=i, role=stage.role, error=result.error))
                break
            outcomes.append(StageOutcome(index=i, role=stage.role, content=content))
            # 下一个阶段以本阶段产出为输入。
            current = content

        return PipelineResult(
            stages=outcomes,
            final_content=outcomes[-1].content if outcomes else "",
        )

    @staticmethod
    def _render_stage_prompt(template: str, incoming: str, *, first: bool) -> str:
        """渲染阶段提示词。

        - 模板含 `{input}`：直接替换为上一阶段产出（或初始输入）；
        - 不含：把上一阶段产出追加在模板之后（首阶段同样处理）。
        """
        incoming = incoming or ""
        if INPUT_PLACEHOLDER in template:
            return template.replace(INPUT_PLACEHOLDER, incoming)
        if incoming:
            return f"{template}\n\n以下是你需要处理的输入：\n{incoming}"
        return template
