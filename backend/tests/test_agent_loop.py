"""AgentLoop 集成测试（mock provider + 工具调用 + 钩子 + 异常处理）。"""

import os
import tempfile
from collections.abc import AsyncIterator
from typing import Any

import pytest

from harness.engine.agent_loop import AgentLoop, AgentLoopConfig
from harness.engine.builtin_tools import CalculatorTool, CurrentTimeTool
from harness.engine.tool_registry import ToolRegistry
from harness.infra.database import Database
from harness.kernel.contracts.hook import HookContext, HookResult
from harness.kernel.hooks import HookManager
from harness.kernel.services import ServiceRegistry
from harness.modules.context_manager.service import ContextService, ContextServiceImpl
from harness.modules.session_manager.service import SessionService, SessionServiceImpl


class MockProvider:
    """Mock provider —— 模拟模型响应。"""

    def __init__(self, responses: list[list[dict[str, Any]]]) -> None:
        """初始化。

        Args:
            responses: 每次调用的响应列表（每次调用返回一组 chunk）
        """
        self._responses = responses
        self._call_index = 0

    async def chat(
        self,
        messages: list[dict[str, str]],
        model: str,
        stream: bool = True,
        **kwargs: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        """模拟流式对话。"""
        if self._call_index < len(self._responses):
            chunks = self._responses[self._call_index]
            self._call_index += 1
        else:
            chunks = [{"delta": "无更多响应"}]

        for chunk in chunks:
            yield chunk


@pytest.fixture
def services() -> ServiceRegistry:
    """创建带 SessionService 和 ContextService 的 ServiceRegistry。"""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db = Database(path)
    session_service = SessionServiceImpl(db)
    ContextServiceImpl(services=ServiceRegistry())
    # ContextServiceImpl 需要一个含 SessionService 的 registry
    # 创建嵌套 registry
    registry = ServiceRegistry()
    registry.register(SessionService, session_service, owner="test")
    ctx_service = ContextServiceImpl(services=registry)
    registry.register(ContextService, ctx_service, owner="test")

    yield registry

    db.close()
    os.unlink(path)


@pytest.fixture
def tool_registry() -> ToolRegistry:
    """创建带内置工具的 ToolRegistry。"""
    registry = ToolRegistry()
    registry.register(CalculatorTool(), owner="test")
    registry.register(CurrentTimeTool(), owner="test")
    return registry


class TestAgentLoopBasic:
    """AgentLoop 基本对话测试。"""

    @pytest.mark.asyncio
    async def test_simple_response(
        self, services: ServiceRegistry, tool_registry: ToolRegistry
    ) -> None:
        """简单对话（无工具调用）。"""
        session_service = services.get(SessionService)
        session = session_service.create_session("测试")

        provider = MockProvider(
            responses=[[{"delta": "你好！我是助手。"}]]
        )

        loop = AgentLoop(
            services=services,
            hooks=HookManager(),
            tool_registry=tool_registry,
        )

        result = await loop.run(
            session_id=session.id,
            user_message="你好",
            provider=provider,
        )

        assert result.content == "你好！我是助手。"
        assert len(result.tool_calls_made) == 0
        assert result.error is None

        # 验证消息已持久化
        messages = session_service.list_messages(session.id)
        assert len(messages) == 2  # user + assistant
        assert messages[0].role == "user"
        assert messages[0].content == "你好"
        assert messages[1].role == "assistant"

    @pytest.mark.asyncio
    async def test_tool_call_calculation(
        self, services: ServiceRegistry, tool_registry: ToolRegistry
    ) -> None:
        """工具调用：计算 123*456。"""
        session_service = services.get(SessionService)
        session = session_service.create_session("计算测试")

        # 第一次调用返回 tool_calls，第二次返回终答
        provider = MockProvider(
            responses=[
                [{"delta": "", "tool_calls": [{"function": {"name": "calculator", "arguments": '{"expression": "123*456"}'}}]}],  # noqa: E501
                [{"delta": "123 * 456 = 56088"}],
            ]
        )

        loop = AgentLoop(
            services=services,
            hooks=HookManager(),
            tool_registry=tool_registry,
        )

        result = await loop.run(
            session_id=session.id,
            user_message="计算 123*456",
            provider=provider,
        )

        assert result.content == "123 * 456 = 56088"
        assert len(result.tool_calls_made) == 1
        assert result.tool_calls_made[0]["tool_name"] == "calculator"
        assert result.tool_calls_made[0]["result"] == "56088"


class TestAgentLoopMultipleTools:
    """AgentLoop 多工具连续调用测试。"""

    @pytest.mark.asyncio
    async def test_two_tools_in_sequence(
        self, services: ServiceRegistry, tool_registry: ToolRegistry
    ) -> None:
        """连续调用两个工具（当前时间 + 计算器）。"""
        session_service = services.get(SessionService)
        session = session_service.create_session("多工具测试")

        provider = MockProvider(
            responses=[
                # 第一次：调用 current_time
                [{"delta": "", "tool_calls": [{"function": {"name": "current_time", "arguments": "{}"}}]}],
                # 第二次：调用 calculator
                [{"delta": "", "tool_calls": [{"function": {"name": "calculator", "arguments": '{"expression": "123*456"}'}}]}],  # noqa: E501
                # 第三次：终答
                [{"delta": "现在是晚上，123*456=56088"}],
            ]
        )

        loop = AgentLoop(
            services=services,
            hooks=HookManager(),
            tool_registry=tool_registry,
        )

        result = await loop.run(
            session_id=session.id,
            user_message="现在几点？顺便算一下 123*456",
            provider=provider,
        )

        assert len(result.tool_calls_made) == 2
        assert result.tool_calls_made[0]["tool_name"] == "current_time"
        assert result.tool_calls_made[1]["tool_name"] == "calculator"
        assert result.tool_calls_made[1]["result"] == "56088"


class TestAgentLoopHooks:
    """AgentLoop 钩子测试。"""

    @pytest.mark.asyncio
    async def test_pre_model_call_hook_modifies_request(
        self, services: ServiceRegistry, tool_registry: ToolRegistry
    ) -> None:
        """pre_model_call 钩子改写模型请求（追加系统提示）。"""
        session_service = services.get(SessionService)
        session = session_service.create_session("钩子测试")

        provider = MockProvider(
            responses=[[{"delta": "收到系统提示"}]]
        )

        hooks = HookManager()

        # 注册 pre_model_call 钩子：修改 model 名称
        async def modify_model_hook(ctx: HookContext) -> HookResult:
            from harness.engine.hook_types import ModelRequest

            if isinstance(ctx.data, ModelRequest):
                ctx.data.model = "modified-model"
                return HookResult(data=ctx.data)
            return HookResult(data=ctx.data)

        hooks.register("pre_model_call", modify_model_hook, owner="test")

        loop = AgentLoop(
            services=services,
            hooks=hooks,
            tool_registry=tool_registry,
        )

        result = await loop.run(
            session_id=session.id,
            user_message="你好",
            provider=provider,
        )

        # 钩子不阻止对话，只修改请求
        assert result.content == "收到系统提示"

    @pytest.mark.asyncio
    async def test_hook_short_circuit(
        self, services: ServiceRegistry, tool_registry: ToolRegistry
    ) -> None:
        """pre_model_call 钩子短路终止。"""
        session_service = services.get(SessionService)
        session = session_service.create_session("短路测试")

        provider = MockProvider(responses=[[{"delta": "不该到达"}]])

        hooks = HookManager()

        async def short_circuit_hook(ctx: HookContext) -> HookResult:
            return HookResult(short_circuit=True, error="被钩子拦截")

        hooks.register("pre_model_call", short_circuit_hook, owner="test")

        loop = AgentLoop(
            services=services,
            hooks=hooks,
            tool_registry=tool_registry,
        )

        result = await loop.run(
            session_id=session.id,
            user_message="你好",
            provider=provider,
        )

        assert result.error is not None
        assert "被钩子拦截" in result.error


class TestAgentLoopErrorHandling:
    """AgentLoop 异常处理测试。"""

    @pytest.mark.asyncio
    async def test_tool_not_found(
        self, services: ServiceRegistry, tool_registry: ToolRegistry
    ) -> None:
        """工具未找到时作为错误回填，不崩溃。"""
        session_service = services.get(SessionService)
        session = session_service.create_session("异常测试")

        provider = MockProvider(
            responses=[
                [{"delta": "", "tool_calls": [{"function": {"name": "nonexistent_tool", "arguments": "{}"}}]}],
                [{"delta": "工具不存在，但我继续回答"}],
            ]
        )

        loop = AgentLoop(
            services=services,
            hooks=HookManager(),
            tool_registry=tool_registry,
        )

        result = await loop.run(
            session_id=session.id,
            user_message="调用不存在的工具",
            provider=provider,
        )

        assert result.error is None  # 不导致循环崩溃
        assert len(result.tool_calls_made) == 1
        assert result.tool_calls_made[0]["error"] is not None
        assert "工具未找到" in result.tool_calls_made[0]["error"]

    @pytest.mark.asyncio
    async def test_max_iterations_protection(
        self, services: ServiceRegistry, tool_registry: ToolRegistry
    ) -> None:
        """达到最大迭代数后停止。"""
        session_service = services.get(SessionService)
        session = session_service.create_session("迭代保护测试")

        # 每次都返回 tool_calls，永远不终止
        infinite_tool_calls = [{"delta": "", "tool_calls": [{"function": {"name": "calculator", "arguments": '{"expression": "1+1"}'}}]}]  # noqa: E501
        provider = MockProvider(responses=[infinite_tool_calls] * 20)

        loop = AgentLoop(
            services=services,
            hooks=HookManager(),
            tool_registry=tool_registry,
            config=AgentLoopConfig(max_tool_iterations=3),
        )

        result = await loop.run(
            session_id=session.id,
            user_message="无限循环",
            provider=provider,
        )

        # 应在 3 次迭代后停止
        assert len(result.tool_calls_made) == 3

    @pytest.mark.asyncio
    async def test_tool_exception_captured(
        self, services: ServiceRegistry
    ) -> None:
        """工具执行异常被捕获并回填。"""
        session_service = services.get(SessionService)
        session = session_service.create_session("工具异常测试")

        class FailingTool:
            @property
            def tool_name(self) -> str:
                return "failing_tool"

            @property
            def description(self) -> str:
                return "总是失败的工具"

            @property
            def parameters_schema(self) -> dict:
                return {"type": "object", "properties": {}}

            async def execute(self, args: dict) -> str:
                raise RuntimeError("工具故意失败")

        tool_registry = ToolRegistry()
        tool_registry.register(FailingTool(), owner="test")

        provider = MockProvider(
            responses=[
                [{"delta": "", "tool_calls": [{"function": {"name": "failing_tool", "arguments": "{}"}}]}],
                [{"delta": "工具失败了，但循环继续"}],
            ]
        )

        loop = AgentLoop(
            services=services,
            hooks=HookManager(),
            tool_registry=tool_registry,
        )

        result = await loop.run(
            session_id=session.id,
            user_message="调用会失败的工具",
            provider=provider,
        )

        assert result.error is None  # 循环不崩溃
        assert len(result.tool_calls_made) == 1
        assert result.tool_calls_made[0]["error"] is not None
        assert "工具执行异常" in result.tool_calls_made[0]["error"]
