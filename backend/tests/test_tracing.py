"""运行轨迹（Tracing）测试：服务 / 插件订阅 / AgentLoop 端到端。"""

from __future__ import annotations

import os
import tempfile
from collections.abc import AsyncIterator
from typing import Any

import pytest

from harness.engine.agent_loop import AgentLoop
from harness.engine.tool_registry import ToolRegistry
from harness.infra.database import Database
from harness.kernel.contracts.base import PluginManifest
from harness.kernel.contracts.tool import ToolPlugin
from harness.kernel.eventbus import EventBus
from harness.kernel.hooks import HookManager
from harness.kernel.services import ServiceRegistry
from harness.modules.context_manager.service import ContextService, ContextServiceImpl
from harness.modules.session_manager.service import SessionService, SessionServiceImpl
from harness.modules.tracing.service import SpanService, SpanServiceImpl


@pytest.fixture
def db() -> Database:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    database = Database(path)
    yield database
    database.close()
    os.unlink(path)


@pytest.fixture
def service(db: Database) -> SpanServiceImpl:
    return SpanServiceImpl(db)


class TestSpanService:
    """span 服务测试。"""

    def test_record_and_list_spans(self, service: SpanServiceImpl) -> None:
        service.record(trace_id="t1", name="agent_run", kind="run", duration_ms=100)
        service.record(
            trace_id="t1",
            name="model_call",
            kind="model",
            duration_ms=60,
            total_tokens=20,
            parent_id="t1",
        )
        service.record(
            trace_id="t1",
            name="tool:calculator",
            kind="tool",
            duration_ms=10,
            parent_id="t1",
        )
        spans = service.list_spans("t1")
        assert [s.kind for s in spans] == ["run", "model", "tool"]
        assert spans[1].total_tokens == 20

    def test_list_traces_aggregates(self, service: SpanServiceImpl) -> None:
        service.record(trace_id="t1", session_id="s1", name="agent_run", kind="run", duration_ms=120)
        service.record(trace_id="t1", session_id="s1", name="model_call", kind="model", duration_ms=80, total_tokens=30)
        service.record(
            trace_id="t2",
            session_id="s1",
            name="agent_run",
            kind="run",
            duration_ms=50,
            status="error",
        )
        traces = {t.trace_id: t for t in service.list_traces("s1")}
        assert traces["t1"].span_count == 2
        assert traces["t1"].total_tokens == 30
        assert traces["t1"].duration_ms == 120
        assert traces["t1"].status == "ok"
        assert traces["t2"].error_count == 1
        assert traces["t2"].status == "error"
        # 会话过滤
        assert service.list_traces("other") == []

    def test_retention_prunes_oldest(self, db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
        """回归：超过保留上限时自动清理最旧的 span。"""
        monkeypatch.setenv("HARNESS_SPANS_MAX", "2")
        svc = SpanServiceImpl(db)
        for i in range(5):
            svc.record(trace_id="t", name=f"n{i}", kind="model", duration_ms=i)
        spans = svc.list_spans("t")
        assert [s.name for s in spans] == ["n3", "n4"]

    def test_get_and_delete_trace(self, service: SpanServiceImpl) -> None:
        service.record(trace_id="t1", name="agent_run", kind="run", duration_ms=10)
        data = service.get_trace("t1")
        assert data is not None
        assert data["summary"]["trace_id"] == "t1"
        assert len(data["spans"]) == 1
        assert service.get_trace("nope") is None
        assert service.delete_trace("t1") == 1
        assert service.delete_trace("t1") == 0


class TestTracingPlugin:
    """tracing 插件订阅 trace.span 事件。"""

    async def test_plugin_persists_emitted_spans(self, db: Database) -> None:
        from harness.kernel.context import PluginContext
        from plugins.tracing.main import TracingPlugin

        services = ServiceRegistry()
        services.register(Database, db, owner="test")
        events = EventBus()
        plugin = TracingPlugin()
        plugin.manifest = PluginManifest(
            id="tracing",
            name="Tracing",
            version="0.1.0",
            type="service",
            entry="plugins.tracing.main:TracingPlugin",
        )
        await plugin.activate(PluginContext(plugin_id="tracing", services=services, events=events))

        await events.publish(
            "trace.span",
            {
                "trace_id": "t9",
                "session_id": "s9",
                "name": "model_call",
                "kind": "model",
                "duration_ms": 42,
                "prompt_tokens": 7,
                "completion_tokens": 3,
                "total_tokens": 10,
                "extra_key": "kept-in-meta",
            },
        )
        svc: SpanService = services.get(SpanService)
        spans = svc.list_spans("t9")
        assert len(spans) == 1
        assert spans[0].total_tokens == 10
        assert spans[0].meta.get("extra_key") == "kept-in-meta"


class EchoTool(ToolPlugin):
    @property
    def tool_name(self) -> str:
        return "echo"

    @property
    def description(self) -> str:
        return "回显"

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self, args: dict[str, Any]) -> str:
        return "pong"


class TraceProvider:
    """首轮调用 echo 工具，次轮给终答（带 usage）。"""

    def __init__(self) -> None:
        self.calls = 0

    async def chat(
        self, messages: list[dict[str, str]], model: str, stream: bool = True, **kwargs: Any
    ) -> AsyncIterator[dict[str, Any]]:
        self.calls += 1
        if self.calls == 1:
            yield {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call_echo",
                        "function": {"name": "echo", "arguments": "{}"},
                    }
                ]
            }
        else:
            yield {"delta": "完成"}
            yield {
                "usage": {
                    "prompt_tokens": 12,
                    "completion_tokens": 6,
                    "total_tokens": 18,
                }
            }


class TestAgentLoopTracing:
    """AgentLoop 端到端：一次对话产生完整 span 链（含耗时与 token）。"""

    async def test_full_span_chain(self, db: Database) -> None:
        from harness.kernel.context import PluginContext
        from plugins.tracing.main import TracingPlugin

        services = ServiceRegistry()
        session_service = SessionServiceImpl(db)
        services.register(Database, db, owner="test")
        services.register(SessionService, session_service, owner="test")
        services.register(ContextService, ContextServiceImpl(services=services), owner="test")
        tool_registry = ToolRegistry()
        tool_registry.register(EchoTool(), owner="test")
        services.register(ToolRegistry, tool_registry, owner="kernel")

        events = EventBus()
        services.register(EventBus, events, owner="kernel")
        plugin = TracingPlugin()
        plugin.manifest = PluginManifest(
            id="tracing",
            name="Tracing",
            version="0.1.0",
            type="service",
            entry="plugins.tracing.main:TracingPlugin",
        )
        await plugin.activate(PluginContext(plugin_id="tracing", services=services, events=events))

        session = session_service.create_session(title="trace 测试")
        loop = AgentLoop(services=services, hooks=HookManager(), tool_registry=tool_registry)
        result = await loop.run(session_id=session.id, user_message="走一遍", provider=TraceProvider())
        assert result.error is None

        svc: SpanService = services.get(SpanService)
        traces = svc.list_traces(session.id)
        assert len(traces) == 1
        trace = traces[0]
        assert trace.duration_ms >= 0
        assert trace.total_tokens >= 18
        assert trace.error_count == 0

        spans = svc.list_spans(trace.trace_id)
        kinds = [s.kind for s in spans]
        assert "run" in kinds
        assert "model" in kinds
        assert "tool" in kinds

        model_span = next(s for s in spans if s.kind == "model")
        assert max(s.total_tokens for s in spans if s.kind == "model") >= 18
        assert model_span.parent_id == trace.trace_id
        tool_span = next(s for s in spans if s.kind == "tool")
        assert tool_span.name == "tool:echo"
        assert tool_span.status == "ok"
