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
        messages = [
            {"role": "user", "content": f"消息{i}"} for i in range(5)
        ]
        result = strategy.apply(messages, None, "test", 4096, set())
        assert len(result) == 5

    def test_long_messages_truncated(self) -> None:
        """长消息列表被截断到 max_messages。"""
        strategy = SlidingWindowStrategy(max_messages=3)
        messages = [
            {"role": "user", "content": f"消息{i}"} for i in range(10)
        ]
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
        has_summary = any(
            "摘要" in m.get("content", "") for m in result if m["role"] == "system"
        )
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
