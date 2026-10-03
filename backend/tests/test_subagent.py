"""子代理委派测试：服务 / task 工具 / AgentLoop 上下文隔离。"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import AsyncIterator
from typing import Any

import pytest

from harness.engine.agent_loop import AgentLoop, AgentLoopConfig
from harness.engine.runtime import AgentRuntime, reset_runtime, set_runtime
from harness.engine.tool_registry import ToolRegistry
from harness.infra.database import Database
from harness.kernel.contracts.base import PluginManifest
from harness.kernel.contracts.tool import ToolPlugin
from harness.kernel.hooks import HookManager
from harness.kernel.services import ServiceRegistry
from harness.modules.context_manager.service import ContextService, ContextServiceImpl
from harness.modules.session_manager.service import SessionService, SessionServiceImpl
from harness.modules.subagent.service import SubagentService, SubagentServiceImpl

BIG = "X" * 3000


@pytest.fixture
def db() -> Database:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    database = Database(path)
    yield database
    database.close()
    os.unlink(path)


class CollectTool(ToolPlugin):
    """模拟"读取大量材料"的工具，返回超长文本。"""

    @property
    def tool_name(self) -> str:
        return "collect"

    @property
    def description(self) -> str:
        return "收集大量信息"

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self, args: dict[str, Any]) -> str:
        return BIG


class ScenarioProvider:
    """按会话角色分支：子代理（SUB: 标记）先调 collect 再给摘要；主代理先调 task 再收尾。"""

    def __init__(self) -> None:
        self.parent_calls = 0
        self.child_calls = 0

    async def chat(
        self, messages: list[dict[str, str]], model: str, stream: bool = True, **kwargs: Any
    ) -> AsyncIterator[dict[str, Any]]:
        user_msgs = [m.get("content") or "" for m in messages if m.get("role") == "user"]
        is_child = any(x.startswith("SUB:") for x in user_msgs)
        if is_child:
            self.child_calls += 1
            if self.child_calls == 1:
                yield {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "c1",
                            "function": {"name": "collect", "arguments": "{}"},
                        }
                    ]
                }
            else:
                yield {"delta": "子代理摘要：收集完成"}
            return
        self.parent_calls += 1
        if self.parent_calls == 1:
            yield {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "p1",
                        "function": {
                            "name": "task",
                            "arguments": json.dumps({"prompt": "SUB: 去收集信息"}, ensure_ascii=False),
                        },
                    }
                ]
            }
        else:
            yield {"delta": "收到摘要，已处理"}


def _build_services(db: Database) -> tuple[ServiceRegistry, SessionServiceImpl, ToolRegistry]:
    services = ServiceRegistry()
    session_service = SessionServiceImpl(db)
    services.register(Database, db, owner="test")
    services.register(SessionService, session_service, owner="test")
    services.register(ContextService, ContextServiceImpl(services=services), owner="test")
    tool_registry = ToolRegistry()
    tool_registry.register(CollectTool(), owner="test")
    services.register(ToolRegistry, tool_registry, owner="kernel")
    return services, session_service, tool_registry


class TestSubagentService:
    """子代理服务测试。"""

    async def test_run_task_returns_summary_and_cleans_up(self, db: Database) -> None:
        services, session_service, _tr = _build_services(db)
        svc = SubagentServiceImpl(services, HookManager())
        result = await svc.run_task("SUB: 去收集信息", provider=ScenarioProvider())
        assert result.error is None
        assert result.content == "子代理摘要：收集完成"
        assert result.tool_calls == 1
        # 子代理会话用完即删，不留痕迹
        assert session_service.list_sessions() == []

    async def test_run_task_without_provider(self, db: Database) -> None:
        services, _ss, _tr = _build_services(db)
        svc = SubagentServiceImpl(services, HookManager())
        result = await svc.run_task("no provider here")
        assert result.error is not None
        assert "provider" in result.error

    async def test_run_task_uses_runtime_provider(self, db: Database) -> None:
        services, _ss, tool_registry = _build_services(db)
        svc = SubagentServiceImpl(services, HookManager())
        services.register(SubagentService, svc, owner="test")
        rt = AgentRuntime(
            provider=ScenarioProvider(),
            model="m",
            session_id="parent",
            services=services,
            hooks=HookManager(),
            tool_registry=tool_registry,
        )
        token = set_runtime(rt)
        try:
            result = await svc.run_task("SUB: 去收集信息")
        finally:
            reset_runtime(token)
        assert result.content == "子代理摘要：收集完成"


class TestTaskTool:
    """task 工具测试。"""

    async def test_tool_contract_and_error_paths(self, db: Database) -> None:
        from plugins.tool_task.main import TaskTool

        services, _ss, _tr = _build_services(db)
        tool = TaskTool(services)
        assert tool.tool_name == "task"
        assert tool.risk_level == "write"
        assert tool.parameters_schema["required"] == ["prompt"]
        # 服务缺失
        assert "不可用" in await tool.execute({"prompt": "x"})
        # 缺 prompt
        services.register(SubagentService, SubagentServiceImpl(services), owner="test")
        assert "缺少 prompt" in await TaskTool(services).execute({})

    async def test_tool_returns_summary_via_runtime(self, db: Database) -> None:
        from plugins.tool_task.main import TaskTool

        services, _ss, tool_registry = _build_services(db)
        services.register(SubagentService, SubagentServiceImpl(services, HookManager()), owner="test")
        rt = AgentRuntime(
            provider=ScenarioProvider(),
            model="m",
            session_id="parent",
            services=services,
            hooks=HookManager(),
            tool_registry=tool_registry,
        )
        token = set_runtime(rt)
        try:
            out = await TaskTool(services).execute({"prompt": "SUB: 去收集信息"})
        finally:
            reset_runtime(token)
        assert "[子代理已完成]" in out
        assert "子代理摘要：收集完成" in out


class RuntimeCapturingProvider:
    """记录 chat 期间可见的运行环境。"""

    def __init__(self) -> None:
        self.seen: Any = None

    async def chat(
        self, messages: list[dict[str, str]], model: str, stream: bool = True, **kwargs: Any
    ) -> AsyncIterator[dict[str, Any]]:
        from harness.engine.runtime import get_runtime

        self.seen = get_runtime()
        yield {"delta": "ok"}


class TestRuntimeContext:
    """运行环境（ContextVar）在 run 期间可见、结束后复位。"""

    async def test_runtime_set_during_and_reset_after(self, db: Database) -> None:
        from harness.engine.runtime import get_runtime

        services, session_service, tool_registry = _build_services(db)
        session = session_service.create_session(title="runtime")
        provider = RuntimeCapturingProvider()
        loop = AgentLoop(services=services, hooks=HookManager(), tool_registry=tool_registry)
        result = await loop.run(session_id=session.id, user_message="hi", provider=provider)
        assert result.error is None
        # run 期间：工具/provider 能看到当前运行环境
        assert provider.seen is not None
        assert provider.seen.session_id == session.id
        # run 结束后：复位为 None（不泄漏）
        assert get_runtime() is None


class TestSubagentIsolation:
    """端到端：主代理只收到摘要，子代理的大输出不进主上下文。"""

    async def test_parent_context_not_polluted(self, db: Database) -> None:
        from harness.kernel.context import PluginContext
        from plugins.subagent.main import SubagentPlugin
        from plugins.tool_task.main import TaskTool

        services, session_service, tool_registry = _build_services(db)

        # 激活 subagent 插件（注册 SubagentService）
        plugin = SubagentPlugin()
        plugin.manifest = PluginManifest(
            id="subagent",
            name="Subagent",
            version="0.1.0",
            type="service",
            entry="plugins.subagent.main:SubagentPlugin",
        )
        await plugin.activate(PluginContext(plugin_id="subagent", services=services, hooks=HookManager()))
        # 注册 task 工具
        tool_registry.register(TaskTool(services), owner="test")

        session = session_service.create_session(title="主会话")
        loop = AgentLoop(
            services=services,
            hooks=HookManager(),
            tool_registry=tool_registry,
            config=AgentLoopConfig(),
        )
        result = await loop.run(session_id=session.id, user_message="帮我干活", provider=ScenarioProvider())
        assert result.error is None
        assert result.content == "收到摘要，已处理"

        messages = session_service.list_messages(session.id)
        contents = [m.content or "" for m in messages]
        joined = "\n".join(contents)

        # 主上下文里出现了"子代理已完成"的摘要
        assert any("子代理已完成" in c for c in contents)
        # 但子代理收集到的 3000 字符大输出**没有**进入主上下文
        assert BIG not in joined
        assert "X" * 100 not in joined

        # 子代理临时会话已清理
        assert session_service.list_sessions() == [] or all(
            not (s.title or "").startswith("[子代理]") for s in session_service.list_sessions()
        )
