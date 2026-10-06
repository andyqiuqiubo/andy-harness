"""EvalRunner —— 构建最小运行环境，逐 case 跑 AgentLoop 并汇总。"""

from __future__ import annotations

import asyncio
import inspect
import tempfile
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from harness.engine.agent_loop import AgentLoop, AgentLoopConfig
from harness.engine.builtin_tools import CalculatorTool, CurrentTimeTool
from harness.engine.tool_registry import ToolRegistry
from harness.eval.cases import EvalCase
from harness.eval.report import EvalCaseResult, EvalReport
from harness.eval.scoring import score_case
from harness.infra.database import Database
from harness.kernel.hooks import HookManager
from harness.kernel.services import ServiceRegistry
from harness.modules.context_manager.service import (
    ContextService,
    ContextServiceImpl,
)
from harness.modules.session_manager.service import (
    SessionService,
    SessionServiceImpl,
)

# provider 工厂：传入 case，返回 provider 实例（可同步可异步）。
ProviderFactory = Callable[[EvalCase], Any]


class EvalRunner:
    """评测运行器。

    每个 case：
    1. 用 provider 工厂构造该 case 的 provider；
    2. 在独立会话中跑一次 AgentLoop（可注入额外工具）；
    3. 打分后收进报告。

    Args:
        provider_factory: `(case) -> provider`，离线可返回脚本化 provider，
            在线可返回真实 provider（同一种 provider 可对所有 case 复用）。
        default_model: 默认模型名。
        extra_tools: 所有 case 额外注册的工具（ToolPlugin 实例）。
        db_path: 评测数据库路径（None 用临时库，跑完删除）。
        system_prompt: 注入每次运行的系统提示词（评测矩阵的提示词维度）。
    """

    def __init__(
        self,
        provider_factory: ProviderFactory,
        default_model: str = "eval-model",
        extra_tools: Sequence[Any] | None = None,
        db_path: str | Path | None = None,
        system_prompt: str = "",
    ) -> None:
        self._provider_factory = provider_factory
        self._default_model = default_model
        self._extra_tools = list(extra_tools or [])
        self._db_path = str(db_path) if db_path else None
        self._system_prompt = system_prompt

    async def run(self, cases: Sequence[EvalCase]) -> EvalReport:
        """运行一批 case，返回汇总报告。"""
        wall_start = time.time()
        report = EvalReport(model=self._default_model)

        own_db_path = self._db_path
        cleanup_db = False
        if own_db_path is None:
            fd, tmp = tempfile.mkstemp(suffix=".db")
            import os

            os.close(fd)
            own_db_path = tmp
            cleanup_db = True

        db = Database(own_db_path)
        try:
            for case in cases:
                result = await self._run_one(case, db)
                report.results.append(result)
        finally:
            db.close()
            if cleanup_db:
                import os

                try:
                    os.unlink(own_db_path)
                except OSError:
                    pass

        report.duration_s = time.time() - wall_start
        return report

    async def _run_one(self, case: EvalCase, db: Database) -> EvalCaseResult:
        """运行单个 case。"""
        # 每个 case 独立服务 / 会话，避免相互污染
        registry = ServiceRegistry()
        session_service = SessionServiceImpl(db)
        registry.register(SessionService, session_service, owner="eval")
        ctx_service = ContextServiceImpl(services=registry)
        registry.register(ContextService, ctx_service, owner="eval")

        tool_registry = ToolRegistry()
        tool_registry.register(CalculatorTool(), owner="eval")
        tool_registry.register(CurrentTimeTool(), owner="eval")
        for tool in self._extra_tools:
            tool_registry.register(tool, owner="eval")

        config = AgentLoopConfig(
            model=case.model or self._default_model,
            max_tool_iterations=case.max_iterations,
            system_prompt=self._system_prompt,
        )
        loop = AgentLoop(
            services=registry,
            hooks=HookManager(),
            tool_registry=tool_registry,
            config=config,
        )

        session = session_service.create_session(f"eval-{case.id}")

        provider = self._maybe_await(self._provider_factory(case))
        if inspect.isawaitable(provider):
            provider = await provider

        run_result = await loop.run(
            session_id=session.id,
            user_message=case.prompt,
            provider=provider,
            model=case.model or self._default_model,
        )

        called_tools: list[str] = []
        for tc in run_result.tool_calls_made or []:
            name = tc.get("tool_name")
            if isinstance(name, str) and name:
                called_tools.append(name)
        score = score_case(case, run_result)
        return EvalCaseResult(
            case=case,
            passed=score.passed,
            content=run_result.content,
            iterations=run_result.iterations,
            latency_ms=run_result.latency_ms,
            tool_calls=called_tools,
            score=score,
            error=run_result.error,
        )

    @staticmethod
    def _maybe_await(value: Any) -> Any:
        """统一处理工厂返回值（协程 / Awaitable / 直接值）。"""
        if asyncio.iscoroutine(value) or inspect.isawaitable(value):
            return value
        return value
