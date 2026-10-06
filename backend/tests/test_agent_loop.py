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
    async def test_simple_response(self, services: ServiceRegistry, tool_registry: ToolRegistry) -> None:
        """简单对话（无工具调用）。"""
        session_service = services.get(SessionService)
        session = session_service.create_session("测试")

        provider = MockProvider(responses=[[{"delta": "你好！我是助手。"}]])

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
    async def test_tool_call_calculation(self, services: ServiceRegistry, tool_registry: ToolRegistry) -> None:
        """工具调用：计算 123*456。"""
        session_service = services.get(SessionService)
        session = session_service.create_session("计算测试")

        # 第一次调用返回 tool_calls，第二次返回终答
        provider = MockProvider(
            responses=[
                [
                    {
                        "delta": "",
                        "tool_calls": [{"function": {"name": "calculator", "arguments": '{"expression": "123*456"}'}}],
                    }
                ],  # noqa: E501
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
    async def test_two_tools_in_sequence(self, services: ServiceRegistry, tool_registry: ToolRegistry) -> None:
        """连续调用两个工具（当前时间 + 计算器）。"""
        session_service = services.get(SessionService)
        session = session_service.create_session("多工具测试")

        provider = MockProvider(
            responses=[
                # 第一次：调用 current_time
                [{"delta": "", "tool_calls": [{"function": {"name": "current_time", "arguments": "{}"}}]}],
                # 第二次：调用 calculator
                [
                    {
                        "delta": "",
                        "tool_calls": [{"function": {"name": "calculator", "arguments": '{"expression": "123*456"}'}}],
                    }
                ],  # noqa: E501
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


class TestAgentLoopLanguagePolicy:
    """语言策略：回答须匹配用户提问语种，内部思考可用中文。"""

    @pytest.mark.asyncio
    async def test_language_policy_injected(self, services: ServiceRegistry, tool_registry: ToolRegistry) -> None:
        """_build_context 注入的系统消息中须包含语言策略指令。"""
        session_service = services.get(SessionService)
        session = session_service.create_session("语言策略测试")

        loop = AgentLoop(
            services=services,
            hooks=HookManager(),
            tool_registry=tool_registry,
        )

        messages = await loop._build_context(session_id=session.id, budget=6000, model="test-model")

        system_texts = [m.get("content", "") for m in messages if m.get("role") == "system"]
        joined = "\n".join(system_texts)
        # 关键约束必须出现：要求用用户「本条消息」语种回答
        assert "语言策略" in joined
        assert "本条消息" in joined
        # 明确允许内部思考用中文
        assert "内部思考" in joined and "中文" in joined
        # 明确要求最终回答与用户提问语种一致
        assert "语种一致" in joined

    @pytest.mark.asyncio
    async def test_language_policy_prefixed_before_history(
        self, services: ServiceRegistry, tool_registry: ToolRegistry
    ) -> None:
        """语言策略作为系统消息出现在会话历史之前（位置正确）。"""
        session_service = services.get(SessionService)
        session = session_service.create_session("语言策略顺序")
        session_service.append_message(session.id, "user", "hello")

        loop = AgentLoop(
            services=services,
            hooks=HookManager(),
            tool_registry=tool_registry,
        )

        messages = await loop._build_context(session_id=session.id, budget=6000, model="test-model")

        # 系统消息都应在第一条 user 消息之前
        first_user_idx = next(i for i, m in enumerate(messages) if m.get("role") == "user")
        assert all(m.get("role") == "system" for m in messages[:first_user_idx])
        assert any("语言策略" in m.get("content", "") for m in messages[:first_user_idx])


class TestAgentLoopHooks:
    """AgentLoop 钩子测试。"""

    @pytest.mark.asyncio
    async def test_pre_model_call_hook_modifies_request(
        self, services: ServiceRegistry, tool_registry: ToolRegistry
    ) -> None:
        """pre_model_call 钩子改写模型请求（追加系统提示）。"""
        session_service = services.get(SessionService)
        session = session_service.create_session("钩子测试")

        provider = MockProvider(responses=[[{"delta": "收到系统提示"}]])

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
    async def test_hook_short_circuit(self, services: ServiceRegistry, tool_registry: ToolRegistry) -> None:
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
    async def test_tool_not_found(self, services: ServiceRegistry, tool_registry: ToolRegistry) -> None:
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
    async def test_max_iterations_protection(self, services: ServiceRegistry, tool_registry: ToolRegistry) -> None:
        """达到最大迭代数后停止。"""
        session_service = services.get(SessionService)
        session = session_service.create_session("迭代保护测试")

        # 每次都返回 tool_calls，永远不终止
        infinite_tool_calls = [
            {"delta": "", "tool_calls": [{"function": {"name": "calculator", "arguments": '{"expression": "1+1"}'}}]}
        ]  # noqa: E501
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
    async def test_max_iterations_forces_final_answer(
        self, services: ServiceRegistry, tool_registry: ToolRegistry
    ) -> None:
        """撞迭代上限后强制无工具收尾：必须产出文本终答并落库。

        回归场景：定时任务跑满 max_iterations 后会话里只有一堆
        工具消息、没有任何最终回答（任务却显示"成功"）。
        """
        session_service = services.get(SessionService)
        session = session_service.create_session("强制收尾测试")

        # 前 2 轮：永远要求调工具；第 3 次调用（收尾，无工具）给文字终答
        tool_round = [
            {"delta": "", "tool_calls": [{"function": {"name": "calculator", "arguments": '{"expression": "1+1"}'}}]}
        ]  # noqa: E501
        final_answer = [{"delta": "这是基于已有信息的最终回答。"}]
        provider = MockProvider(responses=[tool_round, tool_round, final_answer])

        loop = AgentLoop(
            services=services,
            hooks=HookManager(),
            tool_registry=tool_registry,
            config=AgentLoopConfig(max_tool_iterations=2),
        )

        result = await loop.run(
            session_id=session.id,
            user_message="帮我算一下",
            provider=provider,
        )

        # 2 轮工具 + 1 次强制收尾
        assert len(result.tool_calls_made) == 2
        assert "最终回答" in (result.content or "")

        # 终答必须落库：会话最后一条消息是带正文的 assistant
        msgs = session_service.list_messages(session.id)
        finals = [m for m in msgs if m.role == "assistant" and (m.content or "").strip()]
        assert finals, "撞上限后必须持久化最终回答"
        assert "最终回答" in finals[-1].content

    @pytest.mark.asyncio
    async def test_max_iterations_wrapup_failure_not_fatal(
        self, services: ServiceRegistry, tool_registry: ToolRegistry
    ) -> None:
        """收尾调用失败不应把整轮标记为失败（工具结果已落库）。"""
        session_service = services.get(SessionService)
        session = session_service.create_session("收尾失败测试")

        tool_round = [
            {"delta": "", "tool_calls": [{"function": {"name": "calculator", "arguments": '{"expression": "1+1"}'}}]}
        ]  # noqa: E501

        class WrapupFailsProvider(MockProvider):
            async def chat(
                self, messages: Any, model: str, stream: bool = True, **kwargs: Any
            ) -> AsyncIterator[dict[str, Any]]:  # noqa: E501
                if self._call_index >= 2:
                    # 收尾调用直接抛错
                    raise RuntimeError("wrapup boom")
                async for chunk in super().chat(messages, model, stream, **kwargs):
                    yield chunk

        provider = WrapupFailsProvider(responses=[tool_round, tool_round])
        loop = AgentLoop(
            services=services,
            hooks=HookManager(),
            tool_registry=tool_registry,
            config=AgentLoopConfig(max_tool_iterations=2, max_retries=1),
        )
        result = await loop.run(session_id=session.id, user_message="测试", provider=provider)
        # 工具轮正常执行，收尾失败不算整体错误
        assert result.error is None
        assert len(result.tool_calls_made) == 2

    @pytest.mark.asyncio
    async def test_tool_exception_captured(self, services: ServiceRegistry) -> None:
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


class TestReasoningPersistence:
    """思维链持久化：DeepSeek 思考模式要求每条 assistant 消息回传它自己的 reasoning_content。

    回归场景：中间 assistant(tool_calls) 消息不存当轮思维链时，
    重新回答 / 工具迭代的后继请求会被 DeepSeek 以
    400 "The reasoning_content in the thinking mode must be passed back
    to the API" 拒绝。
    """

    @pytest.mark.asyncio
    async def test_tool_call_message_persists_own_reasoning(
        self, services: ServiceRegistry, tool_registry: ToolRegistry
    ) -> None:
        """中间 assistant(tool_calls) 消息存当轮思维链；终答只存产出它的那次思维链。"""
        session_service = services.get(SessionService)
        session = session_service.create_session("思维链持久化")

        provider = MockProvider(
            responses=[
                [
                    {
                        "reasoning_content": "第一轮思考",
                        "tool_calls": [{"function": {"name": "calculator", "arguments": '{"expression": "1+1"}'}}],
                    }
                ],
                [{"reasoning_content": "第二轮思考", "delta": "1+1=2"}],
            ]
        )

        loop = AgentLoop(services=services, hooks=HookManager(), tool_registry=tool_registry)
        result = await loop.run(session_id=session.id, user_message="算一下 1+1", provider=provider)

        assert result.content == "1+1=2"
        messages = session_service.list_messages(session.id)
        tool_call_msgs = [m for m in messages if m.role == "assistant" and m.tool_calls]
        assert len(tool_call_msgs) == 1
        # 关键断言：中间消息必须携带当轮思维链（400 触发点）
        assert tool_call_msgs[0].reasoning == "第一轮思考"
        finals = [m for m in messages if m.role == "assistant" and (m.content or "").strip()]
        assert len(finals) == 1
        # 终答只带产出它的那次调用的思维链，不重复拼接前几轮
        assert finals[0].reasoning == "第二轮思考"

    @pytest.mark.asyncio
    async def test_regenerate_keeps_history_and_appends_version(
        self, services: ServiceRegistry, tool_registry: ToolRegistry
    ) -> None:
        """重新回答：不新建 user 消息，历史问答对保留，新答案 parent_id 指向原提问。"""
        session_service = services.get(SessionService)
        session = session_service.create_session("重新回答")

        provider1 = MockProvider(responses=[[{"reasoning_content": "第一次思考", "delta": "第一次回答"}]])
        loop = AgentLoop(services=services, hooks=HookManager(), tool_registry=tool_registry)
        await loop.run(session_id=session.id, user_message="你好", provider=provider1)

        messages = session_service.list_messages(session.id)
        user_msg = messages[0]
        assert user_msg.role == "user"
        first_answer = messages[1]
        assert first_answer.content == "第一次回答"
        assert first_answer.reasoning == "第一次思考"

        # 重新回答：parent_message_id 指向原提问，同一提问再生成一条新答案
        provider2 = MockProvider(responses=[[{"reasoning_content": "第二次思考", "delta": "第二次回答"}]])
        await loop.run(
            session_id=session.id,
            user_message="你好",
            provider=provider2,
            parent_message_id=user_msg.id,
        )

        messages = session_service.list_messages(session.id)
        # 只有一条 user 消息（不新建提问），历史答案保留
        user_msgs = [m for m in messages if m.role == "user"]
        assert len(user_msgs) == 1
        answers = [m for m in messages if m.role == "assistant" and (m.content or "").strip()]
        assert len(answers) == 2
        assert answers[0].content == "第一次回答"
        assert answers[1].content == "第二次回答"
        # 两条答案的 parent_id 都指向同一提问
        assert answers[0].parent_id == user_msg.id
        assert answers[1].parent_id == user_msg.id

        # 常规请求上下文：版本折叠后只保留最新答案（且带 reasoning_content 回传）
        ctx_service = services.get(ContextService)
        context_msgs = ctx_service.build(session.id, budget=4096, model="deepseek-v4-flash")
        assistants = [m for m in context_msgs if m["role"] == "assistant"]
        assert len(assistants) == 1
        assert assistants[0]["content"] == "第二次回答"
        assert assistants[0].get("reasoning_content") == "第二次思考"

        # 重新回答请求上下文：目标提问的历史答案全部不可见（模型从头重新作答）
        regen_msgs = ctx_service.build(
            session.id,
            budget=4096,
            model="deepseek-v4-flash",
            regenerate_of_message_id=user_msg.id,
        )
        assert not [m for m in regen_msgs if m["role"] == "assistant"]
        assert regen_msgs[-1]["content"] == "你好"

    @pytest.mark.asyncio
    async def test_regenerate_with_tools_reanswers_fresh(
        self, services: ServiceRegistry, tool_registry: ToolRegistry
    ) -> None:
        """重新回答带工具的提问：首次调用看不到旧答案；迭代中能看到本轮工具结果。

        回归场景：旧实现把历史答案留在上下文里且以旧答案结尾，
        模型对「重新回答」只做总结分析，而不是重新调工具重新作答。
        """
        session_service = services.get(SessionService)
        session = session_service.create_session("重新回答-工具轮")

        # 第一次回答：调计算器 + 终答（mock 的 tool_calls 带 id，与真实 API 一致）
        provider1 = MockProvider(
            responses=[
                [
                    {
                        "reasoning_content": "旧思考",
                        "tool_calls": [
                            {
                                "id": "call_old",
                                "type": "function",
                                "function": {"name": "calculator", "arguments": '{"expression": "40+2"}'},
                            }
                        ],
                    }
                ],
                [{"reasoning_content": "旧终答思考", "delta": "旧答案：42"}],
            ]
        )
        loop = AgentLoop(services=services, hooks=HookManager(), tool_registry=tool_registry)
        await loop.run(session_id=session.id, user_message="算 40+2", provider=provider1)

        user_msg = session_service.list_messages(session.id)[0]

        # 重新回答：记录每次模型调用看到的上下文
        seen: list[list[dict[str, Any]]] = []

        class RecordingProvider(MockProvider):
            async def chat(
                self, messages: Any, model: str, stream: bool = True, **kwargs: Any
            ) -> AsyncIterator[dict[str, Any]]:
                seen.append(list(messages))
                async for c in super().chat(messages, model, stream, **kwargs):
                    yield c

        provider2 = RecordingProvider(
            responses=[
                [
                    {
                        "reasoning_content": "新思考",
                        "tool_calls": [
                            {
                                "id": "call_new",
                                "type": "function",
                                "function": {"name": "calculator", "arguments": '{"expression": "40+2"}'},
                            }
                        ],
                    }
                ],
                [{"reasoning_content": "新终答思考", "delta": "新答案：42"}],
            ]
        )
        result = await loop.run(
            session_id=session.id,
            user_message="算 40+2",
            provider=provider2,
            parent_message_id=user_msg.id,
        )
        assert result.content == "新答案：42"

        assert len(seen) == 2
        # 首次迭代：以提问结尾，旧答案与旧思维链完全不可见
        first_contents = [str(m.get("content") or "") for m in seen[0]]
        assert "旧答案：42" not in first_contents
        assert first_contents[-1] == "算 40+2"
        assert all(m.get("reasoning_content") != "旧思考" for m in seen[0])
        # 第二次迭代：能看到本轮的工具结果与新思维链，旧答案依旧不可见
        second_contents = [str(m.get("content") or "") for m in seen[1]]
        assert "旧答案：42" not in second_contents
        assert any("42" in c for c in second_contents)
        assert any(m.get("reasoning_content") == "新思考" for m in seen[1])
