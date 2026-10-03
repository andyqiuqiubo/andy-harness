"""多 Agent 编排测试：并行任务、流水线、编排工具、端到端（E10）。"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from collections.abc import AsyncIterator
from typing import Any

import pytest

from harness.engine.agent_loop import AgentLoop, AgentLoopConfig
from harness.infra.database import Database
from harness.kernel.contracts.base import PluginManifest
from harness.kernel.hooks import HookManager
from harness.kernel.services import ServiceRegistry
from harness.modules.context_manager.service import ContextService, ContextServiceImpl
from harness.modules.orchestrator.service import (
    MAX_PARALLEL_TASKS,
    MAX_PIPELINE_STAGES,
    OrchestratorServiceImpl,
    StageSpec,
    SubtaskSpec,
)
from harness.modules.session_manager.service import SessionService, SessionServiceImpl
from harness.modules.subagent.service import (
    SubagentResult,
    SubagentService,
    SubagentServiceImpl,
)


@pytest.fixture
def db() -> Database:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    database = Database(path)
    yield database
    database.close()
    os.unlink(path)


class TrackingSubagent(SubagentService):
    """可配置延迟的假子代理，同时记录并发情况。"""

    def __init__(self) -> None:
        self.active = 0
        self.max_active = 0
        self.prompts: list[str] = []

    async def run_task(
        self,
        prompt: str,
        *,
        provider: Any = None,
        model: str | None = None,
        allow_tools: list[str] | None = None,
        max_iterations: int = 6,
    ) -> SubagentResult:
        self.prompts.append(prompt)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        # 按 prompt 中的标记给出延迟，制造乱序完成。
        delay = 0.0
        if prompt.startswith("SLOW"):
            delay = 0.05
        elif prompt.startswith("FAST"):
            delay = 0.001
        await asyncio.sleep(delay)
        self.active -= 1
        return SubagentResult(content=f"结果({prompt})", iterations=1, tool_calls=1)


def _build_services(db: Database, subagent: SubagentService | None = None) -> ServiceRegistry:
    services = ServiceRegistry()
    services.register(Database, db, owner="test")
    services.register(SessionService, SessionServiceImpl(db), owner="test")
    services.register(ContextService, ContextServiceImpl(services=services), owner="test")
    if subagent is not None:
        services.register(SubagentService, subagent, owner="test")
    return services


class TestParallel:
    """并行编排。"""

    async def test_returns_ordered_results_despite_delays(self) -> None:
        sub = TrackingSubagent()
        services = _build_services(Database(":memory:"), sub)
        orch = OrchestratorServiceImpl(services)
        outcomes = await orch.run_parallel(
            [
                SubtaskSpec("SLOW 一"),
                SubtaskSpec("FAST 二"),
                SubtaskSpec("FAST 三"),
            ]
        )
        assert [o.index for o in outcomes] == [0, 1, 2]
        assert [o.content for o in outcomes] == [
            "结果(SLOW 一)",
            "结果(FAST 二)",
            "结果(FAST 三)",
        ]
        assert all(o.error is None for o in outcomes)

    async def test_respects_max_concurrency(self) -> None:
        sub = TrackingSubagent()
        services = _build_services(Database(":memory:"), sub)
        orch = OrchestratorServiceImpl(services)
        await orch.run_parallel(
            [SubtaskSpec(f"FAST {i}") for i in range(6)],
            max_concurrency=2,
        )
        assert sub.max_active == 2

    async def test_empty_specs(self) -> None:
        sub = TrackingSubagent()
        services = _build_services(Database(":memory:"), sub)
        orch = OrchestratorServiceImpl(services)
        assert await orch.run_parallel([]) == []

    async def test_too_many_tasks_raises(self) -> None:
        sub = TrackingSubagent()
        services = _build_services(Database(":memory:"), sub)
        orch = OrchestratorServiceImpl(services)
        with pytest.raises(ValueError):
            await orch.run_parallel([SubtaskSpec("x") for _ in range(MAX_PARALLEL_TASKS + 1)])

    async def test_no_subagent_service_all_error(self) -> None:
        services = _build_services(Database(":memory:"))
        orch = OrchestratorServiceImpl(services)
        outcomes = await orch.run_parallel([SubtaskSpec("x"), SubtaskSpec("y")])
        assert len(outcomes) == 2
        assert all(o.error is not None for o in outcomes)

    async def test_single_task_exception_isolated(self) -> None:
        class FlakySubagent(TrackingSubagent):
            async def run_task(self, prompt: str, **kwargs: Any) -> SubagentResult:
                if prompt == "boom":
                    raise RuntimeError("子任务炸了")
                return await super().run_task(prompt, **kwargs)

        services = _build_services(Database(":memory:"), FlakySubagent())
        orch = OrchestratorServiceImpl(services)
        outcomes = await orch.run_parallel([SubtaskSpec("boom"), SubtaskSpec("FAST ok")])
        assert outcomes[0].error == "子任务炸了"
        assert outcomes[1].error is None
        assert outcomes[1].content == "结果(FAST ok)"


class TestPipeline:
    """流水线编排。"""

    async def test_relays_output_between_stages(self) -> None:
        sub = TrackingSubagent()
        services = _build_services(Database(":memory:"), sub)
        orch = OrchestratorServiceImpl(services)
        result = await orch.run_pipeline(
            [
                StageSpec(role="研究员", prompt="调研"),
                StageSpec(role="写作者", prompt="写作"),
                StageSpec(role="评审", prompt="评审"),
            ],
            initial_input="主题：测试",
        )
        assert result.error is None
        assert [s.role for s in result.stages] == ["研究员", "写作者", "评审"]
        # 第 2 阶段的 prompt 里应包含第 1 阶段产出
        assert "结果(调研" in sub.prompts[1]
        # 最终产出为最后一个阶段
        assert result.final_content == result.stages[-1].content

    async def test_placeholder_substitution(self) -> None:
        sub = TrackingSubagent()
        services = _build_services(Database(":memory:"), sub)
        orch = OrchestratorServiceImpl(services)
        await orch.run_pipeline(
            [StageSpec(role="a", prompt="处理以下内容：{input}")],
            initial_input="原始材料",
        )
        assert "原始材料" in sub.prompts[0]
        assert "{input}" not in sub.prompts[0]

    async def test_appends_input_without_placeholder(self) -> None:
        sub = TrackingSubagent()
        services = _build_services(Database(":memory:"), sub)
        orch = OrchestratorServiceImpl(services)
        await orch.run_pipeline(
            [StageSpec(role="a", prompt="只给任务，没有占位符")],
            initial_input="附加材料",
        )
        assert "附加材料" in sub.prompts[0]

    async def test_failure_stops_pipeline(self) -> None:
        class FailAtSecond(TrackingSubagent):
            async def run_task(self, prompt: str, **kwargs: Any) -> SubagentResult:
                if len(self.prompts) == 1:
                    return SubagentResult(error="第二阶段失败")
                return await super().run_task(prompt, **kwargs)

        sub = FailAtSecond()
        services = _build_services(Database(":memory:"), sub)
        orch = OrchestratorServiceImpl(services)
        result = await orch.run_pipeline(
            [
                StageSpec(role="a", prompt="p1"),
                StageSpec(role="b", prompt="p2"),
                StageSpec(role="c", prompt="p3"),
            ]
        )
        # 第二阶段失败后不再继续
        assert len(result.stages) == 2
        assert result.stages[1].error == "第二阶段失败"
        assert result.error == "第二阶段失败"

    async def test_no_subagent_service(self) -> None:
        services = _build_services(Database(":memory:"))
        orch = OrchestratorServiceImpl(services)
        result = await orch.run_pipeline([StageSpec(role="a", prompt="p")])
        assert result.error is not None

    async def test_too_many_stages_raises(self) -> None:
        sub = TrackingSubagent()
        services = _build_services(Database(":memory:"), sub)
        orch = OrchestratorServiceImpl(services)
        with pytest.raises(ValueError):
            await orch.run_pipeline([StageSpec(role="x", prompt="p") for _ in range(MAX_PIPELINE_STAGES + 1)])

    async def test_empty_pipeline(self) -> None:
        sub = TrackingSubagent()
        services = _build_services(Database(":memory:"), sub)
        orch = OrchestratorServiceImpl(services)
        result = await orch.run_pipeline([])
        assert result.stages == []
        assert result.final_content == ""


class TestOrchestratorTools:
    """parallel / pipeline 工具。"""

    async def test_parallel_tool_success(self) -> None:
        from plugins.tool_orchestrator.main import ParallelTool

        sub = TrackingSubagent()
        services = _build_services(Database(":memory:"), sub)
        from harness.modules.orchestrator.service import OrchestratorService

        services.register(OrchestratorService, OrchestratorServiceImpl(services), owner="test")
        out = await ParallelTool(services).execute(
            {
                "tasks": [
                    {"prompt": "SLOW 任务一"},
                    {"prompt": "FAST 任务二"},
                ],
                "max_concurrency": 1,
            }
        )
        assert "[并行完成]" in out
        assert "任务一" in out
        assert "任务二" in out

    async def test_parallel_tool_error_paths(self) -> None:
        from plugins.tool_orchestrator.main import ParallelTool

        # 服务缺失
        services = _build_services(Database(":memory:"))
        assert "不可用" in await ParallelTool(services).execute({"tasks": [{"prompt": "x"}]})
        # 缺 tasks
        from harness.modules.orchestrator.service import OrchestratorService

        services.register(OrchestratorService, OrchestratorServiceImpl(services), owner="test")
        assert "缺少 tasks" in await ParallelTool(services).execute({})
        assert "缺少 tasks" in await ParallelTool(services).execute({"tasks": []})
        # 空 prompt
        assert "必须包含非空 prompt" in await ParallelTool(services).execute({"tasks": [{"prompt": "  "}]})
        # 非数组元素
        assert "必须是对象" in await ParallelTool(services).execute({"tasks": ["x"]})

    async def test_pipeline_tool_success(self) -> None:
        from plugins.tool_orchestrator.main import PipelineTool

        sub = TrackingSubagent()
        services = _build_services(Database(":memory:"), sub)
        from harness.modules.orchestrator.service import OrchestratorService

        services.register(OrchestratorService, OrchestratorServiceImpl(services), owner="test")
        out = await PipelineTool(services).execute(
            {
                "stages": [
                    {"role": "研究员", "prompt": "调研"},
                    {"role": "写作者", "prompt": "根据 {input} 写稿"},
                ],
                "initial_input": "主题",
            }
        )
        assert "[流水线完成]" in out
        assert "研究员" in out
        assert "最终产出" in out

    async def test_pipeline_tool_error_paths(self) -> None:
        from plugins.tool_orchestrator.main import PipelineTool

        # 服务缺失
        services = _build_services(Database(":memory:"))
        assert "不可用" in await PipelineTool(services).execute({"stages": [{"role": "a", "prompt": "x"}]})
        from harness.modules.orchestrator.service import OrchestratorService

        services.register(OrchestratorService, OrchestratorServiceImpl(services), owner="test")
        assert "缺少 stages" in await PipelineTool(services).execute({})
        assert "必须包含非空 role 与 prompt" in await PipelineTool(services).execute(
            {"stages": [{"role": "", "prompt": "x"}]}
        )


class ParallelEndToEndProvider:
    """主代理调 parallel 派生两个子任务；子代理直接给结论。"""

    def __init__(self) -> None:
        self.child_count = 0
        self.parent_calls = 0

    async def chat(
        self,
        messages: list[dict[str, str]],
        model: str,
        stream: bool = True,
        **kwargs: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        user_msgs = [m.get("content") or "" for m in messages if m.get("role") == "user"]
        if any("SUB-A" in x for x in user_msgs):
            self.child_count += 1
            yield {"delta": "A 结论"}
            return
        if any("SUB-B" in x for x in user_msgs):
            self.child_count += 1
            yield {"delta": "B 结论"}
            return
        self.parent_calls += 1
        if self.parent_calls == 1:
            yield {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "o1",
                        "function": {
                            "name": "parallel",
                            "arguments": json.dumps(
                                {
                                    "tasks": [
                                        {"prompt": "SUB-A 任务"},
                                        {"prompt": "SUB-B 任务"},
                                    ]
                                },
                                ensure_ascii=False,
                            ),
                        },
                    }
                ]
            }
        else:
            yield {"delta": "已收到 A/B 结论，汇总完成"}


class TestEndToEnd:
    """端到端：主 AgentLoop 调 parallel，两个子代理并发完成。"""

    async def test_parallel_through_agent_loop(self, db: Database) -> None:
        from harness.engine.tool_registry import ToolRegistry
        from harness.kernel.context import PluginContext
        from plugins.tool_orchestrator.main import OrchestratorToolsPlugin

        services = ServiceRegistry()
        session_service = SessionServiceImpl(db)
        services.register(Database, db, owner="test")
        services.register(SessionService, session_service, owner="test")
        services.register(ContextService, ContextServiceImpl(services=services), owner="test")
        tool_registry = ToolRegistry()
        services.register(ToolRegistry, tool_registry, owner="kernel")
        # 子代理服务（真实实现，跑真实 AgentLoop）
        services.register(SubagentService, SubagentServiceImpl(services, HookManager()), owner="test")

        # 激活编排工具插件（注册服务 + parallel/pipeline 工具）
        plugin = OrchestratorToolsPlugin()
        plugin.manifest = PluginManifest(
            id="tool_orchestrator",
            name="Orchestrator",
            version="0.1.0",
            type="tool",
            entry="plugins.tool_orchestrator.main:OrchestratorToolsPlugin",
        )
        await plugin.activate(
            PluginContext(
                plugin_id="tool_orchestrator",
                services=services,
                hooks=HookManager(),
            )
        )

        session = session_service.create_session(title="主会话")
        provider = ParallelEndToEndProvider()
        loop = AgentLoop(
            services=services,
            hooks=HookManager(),
            tool_registry=tool_registry,
            config=AgentLoopConfig(),
        )
        result = await loop.run(
            session_id=session.id,
            user_message="并行处理两个任务",
            provider=provider,
        )
        assert result.error is None
        assert result.content == "已收到 A/B 结论，汇总完成"
        # 两个子代理确实都跑了
        assert provider.child_count == 2

        contents = [m.content or "" for m in session_service.list_messages(session.id)]
        joined = "\n".join(contents)
        # 主上下文出现并行结果（含 A、B 结论）
        assert "并行完成" in joined
        assert "A 结论" in joined and "B 结论" in joined
        # 子代理临时会话已清理（仅剩主会话本身）
        remaining = session_service.list_sessions()
        assert [s.id for s in remaining] == [session.id]
        assert all(not (s.title or "").startswith("[子代理]") for s in remaining)
