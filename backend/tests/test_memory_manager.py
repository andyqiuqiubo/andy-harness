"""长期记忆测试：服务 / 工具 / AgentLoop 注入。"""

from __future__ import annotations

import os
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from harness.engine.agent_loop import AgentLoop
from harness.engine.tool_registry import ToolRegistry
from harness.infra.database import Database
from harness.kernel.hooks import HookManager
from harness.kernel.services import ServiceRegistry
from harness.modules.context_manager.service import ContextService, ContextServiceImpl
from harness.modules.memory_manager.service import (
    SCOPE_GLOBAL,
    SCOPE_SESSION,
    MemoryService,
    MemoryServiceImpl,
)
from harness.modules.session_manager.service import SessionService, SessionServiceImpl


@pytest.fixture
def db() -> Database:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    database = Database(path)
    yield database
    database.close()
    os.unlink(path)


@pytest.fixture
def service(db: Database) -> MemoryServiceImpl:
    return MemoryServiceImpl(db)


class TestMemoryService:
    """长期记忆服务测试。"""

    def test_save_and_get(self, service: MemoryServiceImpl) -> None:
        rec = service.save("user.language", "中文", tags=["pref"])
        assert rec.id.startswith("mem_")
        assert rec.scope == SCOPE_GLOBAL
        assert rec.tags == ["pref"]
        assert service.get(rec.id) is not None
        assert service.get(rec.id).value == "中文"

    def test_save_same_key_overwrites(self, service: MemoryServiceImpl) -> None:
        a = service.save("k", "v1")
        b = service.save("k", "v2")
        assert a.id == b.id
        assert service.get(a.id).value == "v2"
        assert len(service.list_memories()) == 1

    def test_global_and_session_scopes_isolated(self, service: MemoryServiceImpl) -> None:
        service.save("k", "global-val", scope=SCOPE_GLOBAL)
        service.save("k", "session-val", scope=SCOPE_SESSION, session_id="s1")
        global_items = service.list_memories(scope=SCOPE_GLOBAL)
        session_items = service.list_memories(scope=SCOPE_SESSION)
        assert [m.value for m in global_items] == ["global-val"]
        assert [m.value for m in session_items] == ["session-val"]

    def test_search_matches_key_value_tags(self, service: MemoryServiceImpl) -> None:
        service.save("project.stack", "Vue3 + FastAPI", tags=["tech"])
        service.save("user.city", "深圳")
        assert len(service.search("Vue3")) == 1
        assert len(service.search("深圳")) == 1
        assert len(service.search("tech")) == 1  # tags 也可命中
        assert len(service.search("nonexistent")) == 0
        # 空 query 返回全部（最近优先）
        assert len(service.search("")) == 2

    def test_search_wildcards_are_literal(self, service: MemoryServiceImpl) -> None:
        """回归：查询里的 % / _ 必须当作字面量，不能匹配全部。"""
        service.save("k1", "value-one")
        service.save("k2", "value-two")
        assert service.search("%") == []
        assert service.search("_") == []
        service.save("a_b", "underscore")
        assert [m.key for m in service.search("a_b")] == ["a_b"]
        assert [m.key for m in service.search("_")] == ["a_b"]

    def test_update_and_delete(self, service: MemoryServiceImpl) -> None:
        rec = service.save("k", "v")
        updated = service.update(rec.id, value="v2", tags=["a", "b"])
        assert updated is not None
        assert updated.value == "v2"
        assert updated.tags == ["a", "b"]
        assert service.update("missing", value="x") is None
        assert service.delete(rec.id) is True
        assert service.delete(rec.id) is False

    def test_render_hint(self, service: MemoryServiceImpl) -> None:
        assert service.render_hint() == ""
        service.save("user.language", "中文")
        service.save("long", "x" * 500)
        # session 作用域不应出现在注入片段中
        service.save("secret", "s", scope=SCOPE_SESSION, session_id="s1")
        hint = service.render_hint()
        assert "[长期记忆]" in hint
        assert "user.language" in hint
        assert "secret" not in hint
        assert "…" in hint  # 长值被截断


class TestMemoryTools:
    """memory_save / memory_search 工具测试。"""

    @staticmethod
    def _build(db: Database) -> tuple[ServiceRegistry, MemoryServiceImpl]:
        services = ServiceRegistry()
        services.register(Database, db, owner="test")
        svc = MemoryServiceImpl(db)
        services.register(MemoryService, svc, owner="test")
        return services, svc

    async def test_save_tool(self, db: Database) -> None:
        from plugins.tool_memory.main import MemorySaveTool

        services, svc = self._build(db)
        tool = MemorySaveTool(services)
        assert tool.tool_name == "memory_save"
        assert tool.risk_level == "write"
        assert tool.needs_session is True

        out = await tool.execute({"key": "user.name", "value": "Andy"})
        assert "已记住" in out
        assert svc.search("user.name")[0].value == "Andy"

    async def test_save_tool_validation(self, db: Database) -> None:
        from plugins.tool_memory.main import MemorySaveTool

        services, _svc = self._build(db)
        tool = MemorySaveTool(services)
        assert "缺少 key" in await tool.execute({"value": "x"})
        assert "缺少或空的 value" in await tool.execute({"key": "k"})
        assert "需要当前会话" in await tool.execute({"key": "k", "value": "v", "scope": SCOPE_SESSION})

    async def test_search_tool(self, db: Database) -> None:
        from plugins.tool_memory.main import MemorySearchTool

        services, svc = self._build(db)
        svc.save("user.language", "中文")
        tool = MemorySearchTool(services)
        assert tool.tool_name == "memory_search"
        assert tool.risk_level == "read"

        out = await tool.execute({"query": "language"})
        assert "user.language" in out
        assert "未找到" in await tool.execute({"query": "zzz-nope"})

    async def test_tools_service_unavailable(self) -> None:
        from plugins.tool_memory.main import MemorySaveTool, MemorySearchTool

        empty = ServiceRegistry()
        assert "不可用" in await MemorySaveTool(empty).execute({"key": "k", "value": "v"})
        assert "不可用" in await MemorySearchTool(empty).execute({"query": "x"})


class CapturingProvider:
    """记录收到的 messages，返回终答。"""

    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] | None = None

    async def chat(
        self, messages: list[dict[str, str]], model: str, stream: bool = True, **kwargs: Any
    ) -> AsyncIterator[dict[str, Any]]:
        self.messages = list(messages)
        yield {"delta": "好的"}


class TestMemoryInjection:
    """AgentLoop 应把长期记忆注入 system 前缀（跨会话可见）。"""

    async def test_memory_injected_into_new_session(self, db: Database) -> None:
        services = ServiceRegistry()
        session_service = SessionServiceImpl(db)
        services.register(Database, db, owner="test")
        services.register(SessionService, session_service, owner="test")
        services.register(ContextService, ContextServiceImpl(services=services), owner="test")
        memory_service = MemoryServiceImpl(db)
        services.register(MemoryService, memory_service, owner="test")
        tool_registry = ToolRegistry()
        services.register(ToolRegistry, tool_registry, owner="kernel")

        # 上一个会话里保存了一条跨会话记忆
        memory_service.save("user.language", "用户偏好中文交流")

        # 新会话
        session = session_service.create_session(title="新会话")
        provider = CapturingProvider()
        loop = AgentLoop(services=services, hooks=HookManager(), tool_registry=tool_registry)
        result = await loop.run(session_id=session.id, user_message="你好", provider=provider)
        assert result.error is None
        assert provider.messages is not None
        system_text = "\n".join(m["content"] for m in provider.messages if m.get("role") == "system")
        assert "[长期记忆]" in system_text
        assert "user.language" in system_text
        assert "用户偏好中文交流" in system_text


# ───────────────────────── 定时自主总结 ─────────────────────────


class _FakeProvider:
    """按预设回复产出 delta。"""

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls = 0

    async def chat(
        self, messages: list[dict[str, str]], model: str, stream: bool = True, **kwargs: Any
    ) -> AsyncIterator[dict[str, Any]]:
        self.calls += 1
        yield {"delta": self.reply}


class _FakeRegistry:
    """最小 ProviderRegistry 替身。"""

    def __init__(self, provider: Any, provider_id: str = "deepseek") -> None:
        self._provider = provider
        self._id = provider_id

    def list_providers(self) -> list[dict[str, Any]]:
        return [
            {
                "id": self._id,
                "enabled": True,
                "has_api_key": True,
                "models": ["fake-model"],
            }
        ]

    def get_provider(self, provider_id: str) -> Any:
        return self._provider


def _build_summary_env(
    db: Database, reply: str, with_provider: bool = True
) -> tuple[ServiceRegistry, SessionServiceImpl, MemoryServiceImpl]:
    from harness.modules.model_manager.provider_registry import ProviderRegistry

    services = ServiceRegistry()
    services.register(Database, db, owner="test")
    session_service = SessionServiceImpl(db)
    services.register(SessionService, session_service, owner="test")
    memory_service = MemoryServiceImpl(db)
    services.register(MemoryService, memory_service, owner="test")
    if with_provider:
        services.register(ProviderRegistry, _FakeRegistry(_FakeProvider(reply)), owner="test")
    return services, session_service, memory_service


class TestSummarizer:
    """长期记忆的定时总结。"""

    def test_parse_memories_variants(self) -> None:
        from harness.modules.memory_manager.summarizer import MemorySummarizer

        parse = MemorySummarizer._parse_memories
        assert parse('[{"key":"a","value":"b"}]') == [{"key": "a", "value": "b"}]
        assert parse('```json\n[{"key":"a","value":"b"}]\n```') == [{"key": "a", "value": "b"}]
        assert parse('这是说明文字 [{"key":"a","value":"b"}] 结束') == [{"key": "a", "value": "b"}]
        assert parse("[]") == []
        assert parse("不是 JSON") == []
        # 缺 key/value 的条目被丢弃
        assert parse('[{"key":"a"},{"value":"b"}]') == []

    async def test_summarize_creates_memories_and_advances_cursor(self, db: Database, tmp_path: Path) -> None:
        from harness.modules.memory_manager.summarizer import MemorySummarizer

        services, session_service, memory_service = _build_summary_env(
            db, '[{"key":"user.language","value":"常用中文","scope":"global"}]'
        )
        session = session_service.create_session(title="总结测试")
        session_service.append_message(session.id, "user", "我一直用中文交流")
        session_service.append_message(session.id, "assistant", "好的，明白了")

        summarizer = MemorySummarizer(services, tmp_path / "memory_state.json")
        out = await summarizer.summarize_recent()
        assert out.saved == 1
        assert out.summarized == 1
        saved = memory_service.search("user.language")
        assert saved and saved[0].value == "常用中文"
        assert "auto-summary" in saved[0].tags

        # 游标已推进：没有新消息 → 第二次不再总结
        out2 = await summarizer.summarize_recent()
        assert out2.summarized == 0
        assert out2.saved == 0

    async def test_summarize_continues_with_new_messages(self, db: Database, tmp_path: Path) -> None:
        from harness.modules.memory_manager.summarizer import MemorySummarizer

        services, session_service, _ms = _build_summary_env(db, '[{"key":"k","value":"v"}]')
        session = session_service.create_session(title="增量")
        session_service.append_message(session.id, "user", "问题")
        session_service.append_message(session.id, "assistant", "回答")

        summarizer = MemorySummarizer(services, tmp_path / "st.json")
        await summarizer.summarize_recent()
        session_service.append_message(session.id, "user", "又一个问题")
        session_service.append_message(session.id, "assistant", "又一个回答")
        out = await summarizer.summarize_recent()
        assert out.summarized == 1

    async def test_skip_without_provider(self, db: Database, tmp_path: Path) -> None:
        from harness.modules.memory_manager.summarizer import MemorySummarizer

        services, session_service, _ms = _build_summary_env(db, "[]", with_provider=False)
        session = session_service.create_session(title="无 provider")
        session_service.append_message(session.id, "user", "a")
        session_service.append_message(session.id, "assistant", "b")

        summarizer = MemorySummarizer(services, tmp_path / "st.json")
        out = await summarizer.summarize_recent()
        assert out.skipped_reason
        assert out.saved == 0


class TestSummaryAPI:
    """REST：总结器已在完整应用中注册。"""

    def test_summarizer_registered_in_full_app(self) -> None:
        from fastapi.testclient import TestClient

        from harness.main import _services, app
        from harness.modules.memory_manager.summarizer import MemorySummarizer

        with TestClient(app):
            assert isinstance(_services.get(MemorySummarizer), MemorySummarizer)
