"""G4 Checkpoint 断点续跑测试。

覆盖：
1. RunCheckpointService 增删改查（纯 DB）。
2. AgentLoop 在运行中写入检查点（running / done / error）。
3. resume_run：检查点不存在、无可用 provider、正常续跑三条路径。
"""

import os
import tempfile
from collections.abc import AsyncIterator
from typing import Any

import pytest

from harness.engine.agent_loop import AgentLoop, AgentLoopConfig
from harness.engine.checkpoint import RunCheckpointService, resume_run
from harness.engine.tool_registry import ToolRegistry
from harness.infra.database import Database
from harness.kernel.hooks import HookManager
from harness.kernel.services import ServiceRegistry
from harness.modules.context_manager.service import ContextService, ContextServiceImpl
from harness.modules.model_manager.provider_registry import ProviderRegistry
from harness.modules.session_manager.service import SessionService, SessionServiceImpl


class MockProvider:
    """Mock provider —— 模拟模型响应（与 test_agent_loop 同款）。"""

    def __init__(self, responses: list[list[dict[str, Any]]]) -> None:
        self._responses = responses
        self._call_index = 0

    async def chat(
        self,
        messages: list[dict[str, str]],
        model: str,
        stream: bool = True,
        **kwargs: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        if self._call_index < len(self._responses):
            chunks = self._responses[self._call_index]
            self._call_index += 1
        else:
            chunks = [{"delta": "无更多响应"}]
        for chunk in chunks:
            yield chunk


class ResumeProvider:
    """可注册到 ProviderRegistry 的 mock provider（续跑测试用）。"""

    base_url = "http://localhost/mock"
    default_models = ["mock-model"]
    provider_name = "mock"

    def __init__(
        self,
        api_key: str = "",
        base_url: str | None = None,
        models: list[str] | None = None,
        extra_params: dict[str, Any] | None = None,
    ) -> None:
        self.api_key = api_key
        self.models = models or ["mock-model"]

    async def chat(
        self,
        messages: list[dict[str, str]],
        model: str,
        stream: bool = True,
        **kwargs: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        yield {"delta": "（续跑）任务已完成。"}


@pytest.fixture
def db() -> Any:
    """临时数据库。"""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    database = Database(path)
    yield database
    database.close()
    for suffix in ("", "-wal", "-shm"):
        try:
            os.unlink(path + suffix)
        except OSError:
            pass


@pytest.fixture
def services_full() -> Any:
    """含 Database / SessionService / ContextService / ProviderRegistry 的注册表。"""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    database = Database(path)

    session_service = SessionServiceImpl(database)
    registry = ServiceRegistry()
    registry.register(SessionService, session_service, owner="test")
    registry.register(Database, database, owner="test")
    ctx_service = ContextServiceImpl(services=registry)
    registry.register(ContextService, ctx_service, owner="test")

    provider_registry = ProviderRegistry()
    provider_registry.register_provider(
        "mock",
        ResumeProvider,
        {"api_key": "k", "enabled": True, "models": ["mock-model"]},
    )
    registry.register(ProviderRegistry, provider_registry, owner="test")

    yield registry

    database.close()
    for suffix in ("", "-wal", "-shm"):
        try:
            os.unlink(path + suffix)
        except OSError:
            pass


class TestRunCheckpointService:
    """RunCheckpointService 纯 DB 行为。"""

    def test_record_and_get(self, db: Database) -> None:
        svc = RunCheckpointService(db)
        svc.record("r1", "s1", "running", 0, {"phase": "start"})
        cp = svc.get("r1")
        assert cp is not None
        assert cp.run_id == "r1"
        assert cp.session_id == "s1"
        assert cp.status == "running"
        assert cp.iteration == 0
        assert cp.snapshot == {"phase": "start"}

    def test_record_upsert_updates(self, db: Database) -> None:
        svc = RunCheckpointService(db)
        svc.record("r1", "s1", "running", 0)
        svc.record("r1", "s1", "done", 3, {"phase": "finished"})
        cp = svc.get("r1")
        assert cp is not None
        assert cp.status == "done"
        assert cp.iteration == 3
        assert cp.snapshot == {"phase": "finished"}

    def test_update_status(self, db: Database) -> None:
        svc = RunCheckpointService(db)
        svc.record("r1", "s1", "running", 0)
        svc.update_status("r1", "resumed")
        cp = svc.get("r1")
        assert cp is not None
        assert cp.status == "resumed"

    def test_list_by_session(self, db: Database) -> None:
        svc = RunCheckpointService(db)
        svc.record("r1", "s1", "running", 0)
        svc.record("r2", "s1", "done", 2)
        svc.record("r3", "s2", "running", 0)
        rows = svc.list_by_session("s1")
        assert [r.run_id for r in rows] == ["r1", "r2"]

    def test_get_missing_returns_none(self, db: Database) -> None:
        svc = RunCheckpointService(db)
        assert svc.get("nope") is None


class TestAgentLoopCheckpointWriting:
    """AgentLoop 在运行中写入检查点。"""

    @pytest.mark.asyncio
    async def test_done_checkpoint_written(self, services_full: ServiceRegistry) -> None:
        session_service: SessionService = services_full.get(SessionService)
        db: Database = services_full.get(Database)
        session = session_service.create_session("checkpoint-test")

        loop = AgentLoop(
            services=services_full,
            hooks=HookManager(),
            tool_registry=ToolRegistry(),
        )
        result = await loop.run(
            session_id=session.id,
            user_message="你好",
            provider=MockProvider(responses=[[{"delta": "你好！"}]]),
        )
        assert result.error is None

        svc = RunCheckpointService(db)
        cp = svc.get(loop._run_id)  # type: ignore[attr-defined]
        assert cp is not None
        assert cp.session_id == session.id
        assert cp.status == "done"
        assert cp.iteration >= 1

    @pytest.mark.asyncio
    async def test_error_checkpoint_written(self, services_full: ServiceRegistry) -> None:
        session_service: SessionService = services_full.get(SessionService)
        db: Database = services_full.get(Database)
        session = session_service.create_session("checkpoint-error")

        class BoomProvider(MockProvider):
            async def chat(
                self, messages: Any, model: str, stream: bool = True, **kwargs: Any
            ) -> AsyncIterator[dict[str, Any]]:  # noqa: E501
                # 先产出一个空块（使其成为合法 async generator），
                # 再在下一轮迭代抛错，模拟「模型调用失败」。
                yield {"delta": ""}
                raise RuntimeError("model boom")

        loop = AgentLoop(
            services=services_full,
            hooks=HookManager(),
            tool_registry=ToolRegistry(),
            config=AgentLoopConfig(max_retries=1),
        )
        result = await loop.run(
            session_id=session.id,
            user_message="触发错误",
            provider=BoomProvider(responses=[]),
        )
        assert result.error is not None  # 模型持续失败，循环报错

        svc = RunCheckpointService(db)
        cp = svc.get(loop._run_id)  # type: ignore[attr-defined]
        assert cp is not None
        assert cp.status == "error"


class TestResumeRun:
    """resume_run 三条路径。"""

    @pytest.mark.asyncio
    async def test_resume_missing_checkpoint(self, services_full: ServiceRegistry) -> None:
        out = await resume_run(
            run_id="ghost",
            services=services_full,
            hooks=HookManager(),
            tool_registry=ToolRegistry(),
        )
        assert out["error"] is not None
        assert "检查点不存在" in out["error"]
        assert out["content"] == ""

    @pytest.mark.asyncio
    async def test_resume_no_available_provider(self, services_full: ServiceRegistry) -> None:
        session_service: SessionService = services_full.get(SessionService)
        db: Database = services_full.get(Database)
        session = session_service.create_session("no-provider")

        # 写入一个检查点
        RunCheckpointService(db).record("r1", session.id, "running", 0)

        # 把 provider 改成「无可用」：去掉 registry 里的 provider
        empty_registry = ProviderRegistry()
        services_full.register(ProviderRegistry, empty_registry, owner="test")

        out = await resume_run(
            run_id="r1",
            services=services_full,
            hooks=HookManager(),
            tool_registry=ToolRegistry(),
        )
        assert out["error"] is not None
        assert "没有可用的" in out["error"]

    @pytest.mark.asyncio
    async def test_resume_happy_path(self, services_full: ServiceRegistry) -> None:
        session_service: SessionService = services_full.get(SessionService)
        db: Database = services_full.get(Database)
        session = session_service.create_session("resume-happy")

        # 模拟「之前已经跑过一部分、消息已持久化」
        session_service.append_message(session.id, "user", "帮我查一下天气")
        session_service.append_message(session.id, "assistant", "好的，我来查。", tool_calls=[])

        # 写入一个运行中的检查点
        svc = RunCheckpointService(db)
        svc.record("r1", session.id, "running", 1, {"phase": "iteration"})

        out = await resume_run(
            run_id="r1",
            services=services_full,
            hooks=HookManager(),
            tool_registry=ToolRegistry(),
        )
        assert out["error"] is None
        assert "续跑" in (out["content"] or "")
        assert out["session_id"] == session.id

        # 状态应被更新为 resumed
        cp = svc.get("r1")
        assert cp is not None
        assert cp.status == "resumed"
