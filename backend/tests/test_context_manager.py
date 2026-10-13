"""上下文管理测试（ContextService.build + 策略 + token 预算 + 快照）。"""

import os
import tempfile

import pytest

from harness.infra.database import Database
from harness.kernel.contracts.token_counter import TokenCounter
from harness.kernel.services import ServiceRegistry
from harness.modules.context_manager.service import (
    ContextServiceImpl,
    SlidingWindowStrategy,
    SummaryCompressionStrategy,
)
from harness.modules.session_manager.service import SessionService, SessionServiceImpl


class MockTokenCounter(TokenCounter):
    """测试用 MockTokenCounter（固定 token 数）。"""

    def __init__(self, tokens_per_char: int = 1) -> None:
        self.tokens_per_char = tokens_per_char

    def count_tokens(self, text: str, model: str) -> int:
        """按字符数 * 倍率计算 token。"""
        return len(text) * self.tokens_per_char


@pytest.fixture
def services() -> ServiceRegistry:
    """创建带 SessionService 和 MockTokenCounter 的 ServiceRegistry。"""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db = Database(path)
    session_service = SessionServiceImpl(db)

    registry = ServiceRegistry()
    registry.register(Database, db, owner="test")
    registry.register(SessionService, session_service, owner="test")
    registry.register(TokenCounter, MockTokenCounter(), owner="test")

    yield registry

    db.close()
    os.unlink(path)


@pytest.fixture
def context_service(services: ServiceRegistry) -> ContextServiceImpl:
    """创建 ContextService。"""
    return ContextServiceImpl(services=services)


class TestContextBuild:
    """ContextService.build 测试。"""

    def test_build_returns_messages(self, context_service: ContextServiceImpl, services: ServiceRegistry) -> None:
        """build 返回消息列表。"""
        session_service = services.get(SessionService)
        session = session_service.create_session("测试")
        session_service.append_message(session.id, "user", "你好")
        session_service.append_message(session.id, "assistant", "你好！")

        messages = context_service.build(session.id, budget=4096, model="test")
        assert len(messages) == 2
        assert messages[0]["role"] == "user"
        assert messages[1]["role"] == "assistant"

    def test_build_with_system_prompt(self, services: ServiceRegistry) -> None:
        """带系统提示词模板的 build。"""
        ctx_service = ContextServiceImpl(
            services=services,
            system_prompt_template="你是助手。会话ID: $session_id",
        )
        session_service = services.get(SessionService)
        session = session_service.create_session("测试")
        session_service.append_message(session.id, "user", "你好")

        messages = ctx_service.build(session.id, budget=4096, model="test")
        # 第一条应为系统提示词
        assert messages[0]["role"] == "system"
        assert session.id in messages[0]["content"]
        # 然后是用户消息
        assert messages[1]["role"] == "user"


class TestSlidingWindowStrategy:
    """滑动窗口策略测试。"""

    def test_short_messages_not_truncated(self) -> None:
        """短消息列表不被截断。"""
        strategy = SlidingWindowStrategy(max_messages=10)
        messages = [{"role": "user", "content": f"消息{i}"} for i in range(5)]
        result = strategy.apply(messages, None, "test", 4096, set())
        assert len(result) == 5

    def test_long_messages_truncated(self) -> None:
        """长消息列表被截断到 max_messages。"""
        strategy = SlidingWindowStrategy(max_messages=3)
        messages = [{"role": "user", "content": f"消息{i}"} for i in range(10)]
        result = strategy.apply(messages, None, "test", 4096, set())
        assert len(result) == 3
        # 保留最近 3 条
        assert result[0]["content"] == "消息7"
        assert result[2]["content"] == "消息9"


class TestSummaryCompressionStrategy:
    """摘要压缩策略测试。"""

    def test_under_budget_not_compressed(self) -> None:
        """预算充足时不压缩。"""
        counter = MockTokenCounter(tokens_per_char=1)
        strategy = SummaryCompressionStrategy()
        messages = [
            {"role": "user", "content": "短消息"},
            {"role": "assistant", "content": "回复"},
        ]
        result = strategy.apply(messages, counter, "test", budget=1000, pinned_indices=set())
        assert len(result) == 2

    def test_over_budget_compressed(self) -> None:
        """超预算时压缩旧消息。"""
        counter = MockTokenCounter(tokens_per_char=1)
        strategy = SummaryCompressionStrategy()
        messages = [
            {"role": "user", "content": "x" * 100},
            {"role": "assistant", "content": "y" * 100},
            {"role": "user", "content": "z" * 100},
        ]
        result = strategy.apply(messages, counter, "test", budget=150, pinned_indices=set())
        # 应被压缩（摘要 + 部分消息）
        assert len(result) <= 3
        # 应包含摘要
        has_summary = any("摘要" in m.get("content", "") for m in result if m["role"] == "system")
        assert has_summary


class TestTokenBudget:
    """token 预算分配测试。"""

    def test_build_within_budget(self, services: ServiceRegistry) -> None:
        """构建的上下文 token 数符合预算。"""
        counter = MockTokenCounter(tokens_per_char=2)
        services.register(TokenCounter, counter, owner="test2")

        ctx_service = ContextServiceImpl(services=services)
        session_service = services.get(SessionService)
        session = session_service.create_session("预算测试")

        # 添加多条消息
        for i in range(20):
            session_service.append_message(session.id, "user", f"消息{i}内容")

        budget = 50
        ctx_service.build(session.id, budget=budget, model="test")
        snapshot = ctx_service.get_snapshot(session.id)

        assert snapshot is not None
        # 压缩后消息数应少于原始 20 条
        assert snapshot["message_count"] < 20
        # token 数应少于未压缩时的 220
        assert snapshot["token_count"] < 220


class TestContextSnapshot:
    """上下文快照测试。"""

    def test_snapshot_created_after_build(self, context_service: ContextServiceImpl, services: ServiceRegistry) -> None:
        """build 后生成快照。"""
        session_service = services.get(SessionService)
        session = session_service.create_session("快照测试")
        session_service.append_message(session.id, "user", "你好")

        context_service.build(session.id, budget=4096, model="test")
        snapshot = context_service.get_snapshot(session.id)

        assert snapshot is not None
        assert snapshot["session_id"] == session.id
        assert "messages" in snapshot
        assert snapshot["token_count"] > 0
        assert snapshot["budget"] == 4096

    def test_list_snapshots(self, context_service: ContextServiceImpl, services: ServiceRegistry) -> None:
        """列出会话的所有快照。"""
        session_service = services.get(SessionService)
        session = session_service.create_session("多快照测试")

        for i in range(3):
            session_service.append_message(session.id, "user", f"消息{i}")
            context_service.build(session.id, budget=4096, model="test")

        snapshots = context_service.list_snapshots(session.id)
        assert len(snapshots) == 3

    def test_latest_snapshot(self, context_service: ContextServiceImpl, services: ServiceRegistry) -> None:
        """get_snapshot 返回最新快照。"""
        session_service = services.get(SessionService)
        session = session_service.create_session("最新快照测试")

        session_service.append_message(session.id, "user", "第一条")
        context_service.build(session.id, budget=4096, model="test")

        session_service.append_message(session.id, "user", "第二条")
        context_service.build(session.id, budget=4096, model="test")

        snapshot = context_service.get_snapshot(session.id)
        assert snapshot is not None
        assert snapshot["message_count"] == 2


class TestMockTokenCounterIndependent:
    """P3 独立通过：使用 MockTokenCounter（不依赖 P2）。"""

    def test_p3_independent_with_mock(self) -> None:
        """P3 在 P2 未完成时可独立通过（MockTokenCounter 固定返回值）。"""
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db = Database(path)

        registry = ServiceRegistry()
        registry.register(SessionService, SessionServiceImpl(db), owner="test")
        registry.register(TokenCounter, MockTokenCounter(), owner="test")

        ctx_service = ContextServiceImpl(services=registry)
        session_service = registry.get(SessionService)

        session = session_service.create_session("独立测试")
        session_service.append_message(session.id, "user", "你好世界")

        messages = ctx_service.build(session.id, budget=4096, model="mock-model")
        assert len(messages) == 1

        counter = registry.get(TokenCounter)
        result = counter.count_tokens("你好", "mock-model")
        assert result > 0

        db.close()
        os.unlink(path)


class TestReasoningContentEcho:
    """DeepSeek 思考模式多轮回传：assistant 消息须携带 reasoning_content。

    回归场景：重新回答 / 多轮对话时，DeepSeek 思考模型要求请求中的
    assistant 消息（尤其带 tool_calls 的）回传其 reasoning_content，
    否则 400: "The reasoning_content in the thinking mode must be passed
    back to the API"。
    """

    def test_assistant_reasoning_echoed(self, context_service: ContextServiceImpl, services: ServiceRegistry) -> None:
        """带 reasoning 的 assistant 消息构建上下文时回传 reasoning_content。"""
        session_service = services.get(SessionService)
        session = session_service.create_session("回传测试")
        session_service.append_message(session.id, "user", "你好")
        session_service.append_message(session.id, "assistant", "你好！", reasoning="思考过程")

        messages = context_service.build(session.id, budget=4096, model="deepseek-v4-flash")
        assistant = [m for m in messages if m["role"] == "assistant"]
        assert len(assistant) == 1
        assert assistant[0].get("reasoning_content") == "思考过程"

    def test_tool_call_turn_keeps_reasoning_content(
        self, context_service: ContextServiceImpl, services: ServiceRegistry
    ) -> None:
        """含工具调用的轮次：assistant(tool_calls) 消息同样回传 reasoning_content。"""
        session_service = services.get(SessionService)
        session = session_service.create_session("工具轮回传")
        session_service.append_message(session.id, "user", "搜索一下")
        session_service.append_message(
            session.id,
            "assistant",
            "",
            tool_calls=[{"id": "c1", "type": "function", "function": {"name": "web_search", "arguments": "{}"}}],
            reasoning="第一轮思考",
        )
        session_service.append_message(session.id, "tool", "结果", tool_call_id="c1")
        session_service.append_message(session.id, "assistant", "搜索结果如下", reasoning="第二轮思考")

        messages = context_service.build(session.id, budget=4096, model="deepseek-v4-flash")
        assistants = [m for m in messages if m["role"] == "assistant"]
        assert len(assistants) == 2
        # 带 tool_calls 的中间消息必须有 reasoning_content（400 触发点）
        with_tc = [m for m in assistants if m.get("tool_calls")]
        assert len(with_tc) == 1
        assert with_tc[0].get("reasoning_content") == "第一轮思考"
        assert assistants[-1].get("reasoning_content") == "第二轮思考"

    def test_regenerate_collapse_keeps_latest_version_only(
        self, context_service: ContextServiceImpl, services: ServiceRegistry
    ) -> None:
        """版本折叠：同一提问存在多个答案版本时，上下文只保留最新版本。

        否则模型会把旧答案当作「已发生的事实」做总结，而不是重新作答。
        """
        session_service = services.get(SessionService)
        session = session_service.create_session("版本折叠")
        user_msg = session_service.append_message(session.id, "user", "你好")
        session_service.append_message(
            session.id, "assistant", "第一次回答", reasoning="第一次思考", parent_id=user_msg.id
        )
        session_service.append_message(
            session.id, "assistant", "第二次回答", reasoning="第二次思考", parent_id=user_msg.id
        )

        messages = context_service.build(session.id, budget=4096, model="deepseek-v4-flash")
        assistants = [m for m in messages if m["role"] == "assistant"]
        assert len(assistants) == 1
        assert assistants[0]["content"] == "第二次回答"
        assert assistants[0].get("reasoning_content") == "第二次思考"

    def test_regenerate_target_drops_all_versions(
        self, context_service: ContextServiceImpl, services: ServiceRegistry
    ) -> None:
        """重新回答：目标提问的全部历史答案版本不进入上下文（从头重新作答）。"""
        session_service = services.get(SessionService)
        session = session_service.create_session("重新回答目标")
        user_msg = session_service.append_message(session.id, "user", "明天天气如何")
        session_service.append_message(session.id, "assistant", "旧答案", reasoning="旧思考", parent_id=user_msg.id)

        messages = context_service.build(
            session.id,
            budget=4096,
            model="deepseek-v4-flash",
            regenerate_of_message_id=user_msg.id,
        )
        roles = [m["role"] for m in messages]
        assert "assistant" not in roles
        assert messages[-1]["role"] == "user"
        assert messages[-1]["content"] == "明天天气如何"

    def test_collapse_keeps_intermediate_process_of_latest_version(
        self, context_service: ContextServiceImpl, services: ServiceRegistry
    ) -> None:
        """最新版本的过程消息（带 tool_calls 的 assistant，含过渡文字）必须保留。"""
        session_service = services.get(SessionService)
        session = session_service.create_session("过程保留")
        user_msg = session_service.append_message(session.id, "user", "查天气")
        session_service.append_message(
            session.id,
            "assistant",
            "我先查一下天气",
            tool_calls=[{"id": "c1", "type": "function", "function": {"name": "weather", "arguments": "{}"}}],
            parent_id=user_msg.id,
        )
        session_service.append_message(session.id, "tool", "晴", tool_call_id="c1")
        session_service.append_message(session.id, "assistant", "明天晴", reasoning="思考", parent_id=user_msg.id)

        messages = context_service.build(session.id, budget=4096, model="deepseek-v4-flash")
        contents = [str(m.get("content") or "") for m in messages]
        assert "我先查一下天气" in contents
        assert "明天晴" in contents
        assert any(m.get("tool_call_id") == "c1" for m in messages)

    def test_run_message_ids_bypass_collapse_for_in_flight_turn(
        self, context_service: ContextServiceImpl, services: ServiceRegistry
    ) -> None:
        """重新回答的工具迭代中途：本次运行的新消息须在上下文中，旧版本不出现。"""
        session_service = services.get(SessionService)
        session = session_service.create_session("迭代中途")
        user_msg = session_service.append_message(session.id, "user", "查天气")
        # 旧版本（完整）：上一次的回答
        session_service.append_message(session.id, "assistant", "旧答案", reasoning="旧思考", parent_id=user_msg.id)
        # 本次运行进行中的消息：模型刚调了天气工具，结果已落库，等待下一轮迭代
        in_flight_tc = session_service.append_message(
            session.id,
            "assistant",
            "",
            tool_calls=[{"id": "c2", "type": "function", "function": {"name": "weather", "arguments": "{}"}}],
            reasoning="新一轮思考",
            parent_id=user_msg.id,
        )
        in_flight_tool = session_service.append_message(session.id, "tool", "晴 25 度", tool_call_id="c2")

        messages = context_service.build(
            session.id,
            budget=4096,
            model="deepseek-v4-flash",
            regenerate_of_message_id=user_msg.id,
            run_message_ids=[in_flight_tc.id, in_flight_tool.id],
        )
        contents = [str(m.get("content") or "") for m in messages]
        assert "旧答案" not in contents
        assert "晴 25 度" in contents
        # 消息顺序：提问之后紧跟本轮的工具调用与结果
        assert contents[-2] == "" and contents[-1] == "晴 25 度"
        in_flight = next(m for m in messages if m["role"] == "assistant" and m.get("tool_calls"))
        assert in_flight.get("reasoning_content") == "新一轮思考"

    def test_collapse_keeps_interrupted_partial_for_continue(
        self, context_service: ContextServiceImpl, services: ServiceRegistry
    ) -> None:
        """普通轮次尾部无终答的进行中片段（中断后待继续）不被折叠丢弃。"""
        session_service = services.get(SessionService)
        session = session_service.create_session("中断续跑")
        session_service.append_message(session.id, "user", "查天气")
        session_service.append_message(
            session.id,
            "assistant",
            "",
            tool_calls=[{"id": "c1", "type": "function", "function": {"name": "weather", "arguments": "{}"}}],
        )
        session_service.append_message(session.id, "tool", "晴", tool_call_id="c1")

        messages = context_service.build(session.id, budget=4096, model="deepseek-v4-flash")
        contents = [str(m.get("content") or "") for m in messages]
        assert "晴" in contents


class TestBuildCache:
    """MOD-01：上下文构建缓存（命中复用 / 内容变更自失效 / 写入后显式失效）。"""

    @staticmethod
    def _session_with_messages(services: ServiceRegistry, n: int = 2):
        session_service = services.get(SessionService)
        session = session_service.create_session("缓存测试")
        for i in range(n):
            role = "user" if i % 2 == 0 else "assistant"
            session_service.append_message(session.id, role, f"消息{i}")
        return session_service, session

    def test_repeated_build_hits_cache(self, context_service: ContextServiceImpl, services: ServiceRegistry) -> None:
        """同一输入重复 build：结果一致，且快照不会重复落库。"""
        session_service, session = self._session_with_messages(services)

        first = context_service.build(session.id, budget=4096, model="test")
        assert session.id in context_service._build_cache

        snapshots_after_first = context_service.list_snapshots(session.id)
        second = context_service.build(session.id, budget=4096, model="test")
        snapshots_after_second = context_service.list_snapshots(session.id)

        assert [m["content"] for m in first] == [m["content"] for m in second]
        # 命中缓存不再写新快照（去重，避免每次工具迭代都落一条）
        assert len(snapshots_after_second) == len(snapshots_after_first)

    def test_new_message_invalidates(self, context_service: ContextServiceImpl, services: ServiceRegistry) -> None:
        """追加新消息后必须重新构建（签名自失效）。"""
        session_service, session = self._session_with_messages(services)
        first = context_service.build(session.id, budget=4096, model="test")
        session_service.append_message(session.id, "user", "新消息")
        second = context_service.build(session.id, budget=4096, model="test")
        assert len(second) == len(first) + 1

    def test_content_change_invalidates(self, context_service: ContextServiceImpl, services: ServiceRegistry) -> None:
        """消息内容被改写后立即失效（长度相同也不得命中旧结果）。"""
        session_service, session = self._session_with_messages(services, n=1)
        context_service.build(session.id, budget=4096, model="test")

        msgs = session_service.list_messages(session.id)
        session_service.update_message_content(msgs[0].id, "改后") if hasattr(
            session_service, "update_message_content"
        ) else session_service.append_message(session.id, "user", "改后")

        result = context_service.build(session.id, budget=4096, model="test")
        contents = [str(m.get("content") or "") for m in result]
        assert "改后" in contents

    def test_invalidate_cache_forces_rebuild(
        self, context_service: ContextServiceImpl, services: ServiceRegistry
    ) -> None:
        session_service, session = self._session_with_messages(services)
        context_service.build(session.id, budget=4096, model="test")
        assert session.id in context_service._build_cache

        context_service.invalidate_cache(session.id)
        assert session.id not in context_service._build_cache

        context_service.invalidate_cache()  # 全清不报错
        assert context_service._build_cache == {}

    def test_pin_invalidates_cache(self, context_service: ContextServiceImpl, services: ServiceRegistry) -> None:
        session_service, session = self._session_with_messages(services)
        context_service.build(session.id, budget=4096, model="test")
        context_service.pin_message(session.id, 0)
        assert session.id not in context_service._build_cache

    def test_returned_messages_are_copies(self, context_service: ContextServiceImpl, services: ServiceRegistry) -> None:
        """缓存返回的必须是副本：调用方就地修改不得污染缓存。"""
        session_service, session = self._session_with_messages(services)
        first = context_service.build(session.id, budget=4096, model="test")
        first[0]["content"] = "被外部篡改"
        second = context_service.build(session.id, budget=4096, model="test")
        assert second[0]["content"] != "被外部篡改"

    def test_cache_bounded_by_lru(self, context_service: ContextServiceImpl, services: ServiceRegistry) -> None:
        """会话数超过上限时淘汰最旧，防无界增长。"""
        from harness.modules.context_manager.service import _BUILD_CACHE_MAX

        session_service = services.get(SessionService)
        for i in range(_BUILD_CACHE_MAX + 3):
            s = session_service.create_session(f"会话{i}")
            session_service.append_message(s.id, "user", "hi")
            context_service.build(s.id, budget=4096, model="test")
        assert len(context_service._build_cache) <= _BUILD_CACHE_MAX
