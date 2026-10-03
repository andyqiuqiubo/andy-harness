"""大工具输出落盘（Artifact Offload）测试：服务 / 钩子 / 工具 / AgentLoop 集成。"""

from __future__ import annotations

import os
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from harness.engine.agent_loop import AgentLoop
from harness.engine.hook_types import ToolCallContext
from harness.engine.tool_registry import ToolRegistry
from harness.infra.database import Database
from harness.kernel.contracts.base import PluginManifest
from harness.kernel.contracts.hook import HookContext
from harness.kernel.contracts.tool import ToolPlugin
from harness.kernel.hooks import HookManager
from harness.kernel.services import ServiceRegistry
from harness.modules.artifact_store.service import (
    ArtifactStore,
    ArtifactStoreImpl,
    summarize,
)
from harness.modules.context_manager.service import ContextService, ContextServiceImpl
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
def artifacts_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """隔离的工件目录与低阈值。"""
    d = tmp_path / "artifacts"
    monkeypatch.setenv("HARNESS_ARTIFACTS_DIR", str(d))
    monkeypatch.setenv("HARNESS_OFFLOAD_THRESHOLD", "100")
    return d


@pytest.fixture
def store(db: Database, artifacts_dir: Path) -> ArtifactStoreImpl:
    return ArtifactStoreImpl(db)


class TestArtifactStore:
    """工件存储服务测试。"""

    def test_should_offload_threshold(self, store: ArtifactStoreImpl) -> None:
        assert store.should_offload("x" * 50) is False
        assert store.should_offload("x" * 101) is True

    def test_offload_writes_file_and_record(self, store: ArtifactStoreImpl, artifacts_dir: Path) -> None:
        content = "行1\n行2\n" + "Y" * 200
        rec = store.offload("s1", "code_runner", content)
        assert rec.id.startswith("art_")
        assert rec.char_count == len(content)
        assert rec.size_bytes == len(content.encode("utf-8"))
        assert rec.tool_name == "code_runner"
        assert Path(rec.path).exists()
        assert Path(rec.path).read_text(encoding="utf-8") == content
        # 元数据入库
        assert store.get(rec.id) is not None
        assert store.list_artifacts("s1")[0].id == rec.id

    def test_read_full_and_paged(self, store: ArtifactStoreImpl) -> None:
        content = "".join(str(i % 10) for i in range(500))
        rec = store.offload("s1", "web_fetch", content)
        assert store.read(rec.id) == content
        assert store.read(rec.id, offset=10, limit=20) == content[10:30]
        assert store.read(rec.id, offset=490) == content[490:]

    def test_get_and_read_missing(self, store: ArtifactStoreImpl) -> None:
        assert store.get("nope") is None
        assert store.read("nope") is None

    def test_list_filter_and_delete(self, store: ArtifactStoreImpl) -> None:
        a = store.offload("s1", "t", "A" * 200)
        b = store.offload("s2", "t", "B" * 200)
        assert {r.id for r in store.list_artifacts("s1")} == {a.id}
        assert store.delete(a.id) is True
        assert store.get(a.id) is None
        assert not Path(a.path).exists()
        assert store.delete(a.id) is False
        assert {r.id for r in store.list_artifacts()} == {b.id}

    def test_retention_prunes_oldest(self, db: Database, artifacts_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """回归：超过保留上限时自动清理最旧的工件（文件 + 元数据）。"""
        monkeypatch.setenv("HARNESS_ARTIFACTS_MAX", "3")
        store = ArtifactStoreImpl(db)
        recs = [store.offload("s1", "t", f"payload-{i}-" + "x" * 200) for i in range(5)]
        remaining = {r.id for r in store.list_artifacts()}
        assert len(remaining) == 3
        for r in recs[:2]:
            assert r.id not in remaining
            assert store.get(r.id) is None
            assert not Path(r.path).exists()
        for r in recs[-3:]:
            assert r.id in remaining

    def test_summarize(self) -> None:
        assert summarize("hello world") == "hello world"
        long = summarize("a" * 500, head=10)
        assert long.startswith("a" * 10)
        assert "已截断" in long


class TestOffloadHook:
    """post_tool_call 钩子测试。"""

    @staticmethod
    async def _activate(db: Database) -> tuple[HookManager, ArtifactStoreImpl]:
        from harness.kernel.context import PluginContext
        from plugins.artifact_store.main import ArtifactStorePlugin

        services = ServiceRegistry()
        services.register(Database, db, owner="test")
        hooks = HookManager()
        plugin = ArtifactStorePlugin()
        plugin.manifest = PluginManifest(
            id="artifact_store",
            name="Artifact Store",
            version="0.1.0",
            type="service",
            entry="plugins.artifact_store.main:ArtifactStorePlugin",
        )
        ctx = PluginContext(plugin_id="artifact_store", services=services, hooks=hooks)
        await plugin.activate(ctx)
        return hooks, services.get(ArtifactStore)

    async def test_hook_offloads_large_result(self, db: Database, artifacts_dir: Path) -> None:
        hooks, store = await self._activate(db)
        ctx = ToolCallContext(tool_name="web_fetch", args={}, result="Z" * 5000)
        result = await hooks.execute(
            "post_tool_call",
            HookContext(hook_name="post_tool_call", data=ctx, session_id="s1"),
        )
        out = result.data.result
        assert "[输出已落盘]" in out
        assert "read_artifact" in out
        assert len(out) < 2000  # 上下文里的内容被显著压缩
        assert result.metadata.get("offloaded")
        assert len(store.list_artifacts("s1")) == 1

    async def test_hook_keeps_small_result(self, db: Database, artifacts_dir: Path) -> None:
        hooks, store = await self._activate(db)
        ctx = ToolCallContext(tool_name="calculator", result="42")
        result = await hooks.execute(
            "post_tool_call",
            HookContext(hook_name="post_tool_call", data=ctx, session_id="s1"),
        )
        assert result.data.result == "42"
        assert store.list_artifacts() == []

    async def test_hook_ignores_error_and_read_tool(self, db: Database, artifacts_dir: Path) -> None:
        hooks, store = await self._activate(db)
        err_ctx = ToolCallContext(tool_name="code_runner", result="E" * 5000, error="boom")
        await hooks.execute(
            "post_tool_call",
            HookContext(hook_name="post_tool_call", data=err_ctx, session_id="s1"),
        )
        read_ctx = ToolCallContext(tool_name="read_artifact", result="R" * 5000)
        await hooks.execute(
            "post_tool_call",
            HookContext(hook_name="post_tool_call", data=read_ctx, session_id="s1"),
        )
        assert store.list_artifacts() == []


class TestReadArtifactTool:
    """read_artifact 工具测试。"""

    @staticmethod
    def _build(db: Database) -> tuple[ServiceRegistry, ArtifactStoreImpl]:
        services = ServiceRegistry()
        services.register(Database, db, owner="test")
        store = ArtifactStoreImpl(db)
        services.register(ArtifactStore, store, owner="test")
        return services, store

    async def test_tool_contract_and_read(self, db: Database, artifacts_dir: Path) -> None:
        from plugins.tool_read_artifact.main import ReadArtifactTool

        services, store = self._build(db)
        rec = store.offload("s1", "code_runner", "CONTENT-" + "x" * 500)
        tool = ReadArtifactTool(services)
        assert tool.tool_name == "read_artifact"
        assert tool.risk_level == "read"
        assert tool.parameters_schema["required"] == ["artifact_id"]

        out = await tool.execute({"artifact_id": rec.id, "offset": 0, "limit": 20})
        assert "CONTENT-" in out
        assert rec.id in out

    async def test_tool_errors(self, db: Database, artifacts_dir: Path) -> None:
        from plugins.tool_read_artifact.main import ReadArtifactTool

        services, _store = self._build(db)
        tool = ReadArtifactTool(services)
        assert "缺少 artifact_id" in await tool.execute({})
        assert "未找到工件" in await tool.execute({"artifact_id": "art_missing"})

    async def test_tool_service_unavailable(self) -> None:
        from plugins.tool_read_artifact.main import ReadArtifactTool

        out = await ReadArtifactTool(ServiceRegistry()).execute({"artifact_id": "x"})
        assert "不可用" in out


class BigOutputTool(ToolPlugin):
    """返回超大输出的测试工具。"""

    @property
    def tool_name(self) -> str:
        return "big_tool"

    @property
    def description(self) -> str:
        return "返回一大段文本"

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self, args: dict[str, Any]) -> str:
        return "BIG-" + "z" * 8000


class BigToolProvider:
    """首轮调用 big_tool，次轮给终答。"""

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
                        "id": "call_big",
                        "function": {"name": "big_tool", "arguments": "{}"},
                    }
                ]
            }
        else:
            yield {"delta": "数据已获取"}


class TestAgentLoopOffload:
    """AgentLoop 端到端：大输出被落盘，上下文中的 tool 消息显著缩短。"""

    async def test_large_output_offloaded_in_loop(self, db: Database, artifacts_dir: Path) -> None:
        from harness.kernel.context import PluginContext
        from plugins.artifact_store.main import ArtifactStorePlugin

        services = ServiceRegistry()
        session_service = SessionServiceImpl(db)
        services.register(Database, db, owner="test")
        services.register(SessionService, session_service, owner="test")
        services.register(ContextService, ContextServiceImpl(services=services), owner="test")

        tool_registry = ToolRegistry()
        tool_registry.register(BigOutputTool(), owner="test")
        services.register(ToolRegistry, tool_registry, owner="kernel")

        hooks = HookManager()
        plugin = ArtifactStorePlugin()
        plugin.manifest = PluginManifest(
            id="artifact_store",
            name="Artifact Store",
            version="0.1.0",
            type="service",
            entry="plugins.artifact_store.main:ArtifactStorePlugin",
        )
        await plugin.activate(PluginContext(plugin_id="artifact_store", services=services, hooks=hooks))

        session = session_service.create_session(title="offload 测试")
        loop = AgentLoop(services=services, hooks=hooks, tool_registry=tool_registry)
        result = await loop.run(session_id=session.id, user_message="取数据", provider=BigToolProvider())
        assert result.error is None

        # 落盘工件已生成
        store: ArtifactStore = services.get(ArtifactStore)
        artifacts = store.list_artifacts(session.id)
        assert len(artifacts) == 1
        assert artifacts[0].tool_name == "big_tool"
        assert "BIG-" in store.read(artifacts[0].id, 0, 50)

        # 上下文中的 tool 消息是"已落盘"短消息，而非 8000 字符原文
        messages = session_service.list_messages(session.id)
        tool_msgs = [m for m in messages if m.role == "tool"]
        assert tool_msgs, "应有 tool 消息"
        assert "[输出已落盘]" in tool_msgs[-1].content
        assert len(tool_msgs[-1].content) < 2000
