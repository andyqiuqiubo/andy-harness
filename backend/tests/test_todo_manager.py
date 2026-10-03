"""Todo 任务清单测试（服务 / 工具 / AgentLoop 集成）。"""

from __future__ import annotations

import os
import tempfile
from collections.abc import AsyncIterator
from typing import Any

import pytest

from harness.engine.agent_loop import AgentLoop
from harness.engine.tool_registry import ToolRegistry
from harness.infra.database import Database
from harness.kernel.hooks import HookManager
from harness.kernel.services import ServiceRegistry
from harness.modules.context_manager.service import ContextService, ContextServiceImpl
from harness.modules.session_manager.service import SessionService, SessionServiceImpl
from harness.modules.todo_manager.service import (
    STATUS_COMPLETED,
    STATUS_IN_PROGRESS,
    STATUS_PENDING,
    TodoService,
    TodoServiceImpl,
)


@pytest.fixture
def db() -> Database:
    """临时数据库。"""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    database = Database(path)
    yield database
    database.close()
    os.unlink(path)


@pytest.fixture
def service(db: Database) -> TodoServiceImpl:
    """Todo 服务（预置两个真实会话，满足 todos 表的外键约束）。"""
    db.execute("INSERT INTO sessions (id, title) VALUES (?, ?)", ("s1", "会话1"))
    db.execute("INSERT INTO sessions (id, title) VALUES (?, ?)", ("s2", "会话2"))
    return TodoServiceImpl(db)


class TestTodoService:
    """Todo 服务测试。"""

    def test_replace_and_list(self, service: TodoServiceImpl) -> None:
        """覆盖写入后按顺序读回。"""
        saved = service.replace_todos(
            "s1",
            [
                {"content": "分析需求", "status": STATUS_COMPLETED},
                {"content": "实现功能", "status": STATUS_IN_PROGRESS},
                {"content": "写测试", "status": STATUS_PENDING},
            ],
        )
        assert len(saved) == 3
        items = service.list_todos("s1")
        assert [i.content for i in items] == ["分析需求", "实现功能", "写测试"]
        assert items[1].status == STATUS_IN_PROGRESS
        assert [i.position for i in items] == [0, 1, 2]

    def test_replace_is_idempotent(self, service: TodoServiceImpl) -> None:
        """再次覆盖写入会替换旧列表，不会累积。"""
        service.replace_todos("s1", [{"content": "A"}, {"content": "B"}])
        service.replace_todos("s1", [{"content": "C"}])
        items = service.list_todos("s1")
        assert [i.content for i in items] == ["C"]

    def test_invalid_status_defaults_to_pending(self, service: TodoServiceImpl) -> None:
        """非法状态回落为 pending。"""
        service.replace_todos("s1", [{"content": "A", "status": "sleeping"}])
        assert service.list_todos("s1")[0].status == STATUS_PENDING

    def test_empty_content_skipped(self, service: TodoServiceImpl) -> None:
        """空内容条目被跳过。"""
        service.replace_todos("s1", [{"content": "  "}, {"content": "有效"}])
        assert [i.content for i in service.list_todos("s1")] == ["有效"]

    def test_session_isolation(self, service: TodoServiceImpl) -> None:
        """不同会话的列表互不影响。"""
        service.replace_todos("s1", [{"content": "A"}])
        service.replace_todos("s2", [{"content": "B"}])
        assert [i.content for i in service.list_todos("s1")] == ["A"]
        assert [i.content for i in service.list_todos("s2")] == ["B"]

    def test_summary(self, service: TodoServiceImpl) -> None:
        """状态统计正确。"""
        service.replace_todos(
            "s1",
            [
                {"content": "A", "status": STATUS_COMPLETED},
                {"content": "B", "status": STATUS_IN_PROGRESS},
                {"content": "C", "status": STATUS_PENDING},
            ],
        )
        summary = service.summary("s1")
        assert summary[STATUS_COMPLETED] == 1
        assert summary[STATUS_IN_PROGRESS] == 1
        assert summary[STATUS_PENDING] == 1

    def test_clear(self, service: TodoServiceImpl) -> None:
        """清空返回删除条数。"""
        service.replace_todos("s1", [{"content": "A"}, {"content": "B"}])
        assert service.clear_todos("s1") == 2
        assert service.list_todos("s1") == []

    def test_persistence(self, db: Database) -> None:
        """数据在数据库中持久保存（换实例仍可读）。"""
        db.execute("INSERT INTO sessions (id, title) VALUES (?, ?)", ("s1", "会话1"))
        TodoServiceImpl(db).replace_todos("s1", [{"content": "持久化任务"}])
        again = TodoServiceImpl(db)
        assert [i.content for i in again.list_todos("s1")] == ["持久化任务"]


class TodoProvider:
    """Mock provider —— 首轮调用 todo_write，次轮给出终答。"""

    def __init__(self) -> None:
        self.calls = 0

    async def chat(
        self,
        messages: list[dict[str, str]],
        model: str,
        stream: bool = True,
        **kwargs: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        self.calls += 1
        if self.calls == 1:
            import json

            args = json.dumps(
                {
                    "todos": [
                        {"content": "分析需求", "status": "completed"},
                        {"content": "实现功能", "status": "in_progress"},
                        {"content": "补充测试", "status": "pending"},
                    ]
                },
                ensure_ascii=False,
            )
            yield {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call_1",
                        "function": {"name": "todo_write", "arguments": args},
                    }
                ]
            }
        else:
            yield {"delta": "计划已创建"}


class TestTodoWriteTool:
    """todo_write 工具与 AgentLoop 集成测试。"""

    @staticmethod
    def _build(db: Database) -> tuple[ServiceRegistry, ToolRegistry, SessionServiceImpl]:
        registry = ServiceRegistry()
        registry.register(SessionService, SessionServiceImpl(db), owner="test")
        registry.register(ContextService, ContextServiceImpl(services=registry), owner="test")
        todo_service = TodoServiceImpl(db)
        registry.register(TodoService, todo_service, owner="test")

        tool_registry = ToolRegistry()
        from plugins.tool_todo.main import TodoWriteTool

        tool_registry.register(TodoWriteTool(registry), owner="test")
        registry.register(ToolRegistry, tool_registry, owner="kernel")
        return registry, tool_registry, registry.get(SessionService)

    async def test_agent_loop_persists_todos(self, db: Database) -> None:
        """模型调用 todo_write 后，清单持久化到当前会话。"""
        registry, tool_registry, session_service = self._build(db)
        session = session_service.create_session(title="todo 测试")

        loop = AgentLoop(services=registry, hooks=HookManager(), tool_registry=tool_registry)
        result = await loop.run(
            session_id=session.id,
            user_message="帮我拆解这个任务",
            provider=TodoProvider(),
        )

        assert result.error is None
        todos = registry.get(TodoService).list_todos(session.id)
        assert [t.content for t in todos] == ["分析需求", "实现功能", "补充测试"]
        assert todos[1].status == STATUS_IN_PROGRESS

    async def test_tool_result_contains_progress(self, db: Database) -> None:
        """工具返回可读的进度文本，供模型继续推进。"""
        registry, _tool_registry, session_service = self._build(db)
        session = session_service.create_session(title="todo 文本")
        from plugins.tool_todo.main import TodoWriteTool

        tool = TodoWriteTool(registry)
        out = await tool.execute(
            {
                "session_id": session.id,
                "todos": [
                    {"content": "A", "status": "completed"},
                    {"content": "B", "status": "pending"},
                ],
            }
        )
        assert "任务清单已更新" in out
        assert "进度：已完成 1 / 2" in out

    async def test_tool_without_session_id(self, db: Database) -> None:
        """缺少 session_id 时报错而非写脏数据。"""
        registry, _tool_registry, _ss = self._build(db)
        from plugins.tool_todo.main import TodoWriteTool

        out = await TodoWriteTool(registry).execute({"todos": [{"content": "A"}]})
        assert "无法确定当前会话" in out

    async def test_tool_invalid_payload(self, db: Database) -> None:
        """非法参数给出明确错误。"""
        registry, _tool_registry, session_service = self._build(db)
        session = session_service.create_session(title="todo 校验")
        from plugins.tool_todo.main import TodoWriteTool

        tool = TodoWriteTool(registry)
        assert "缺少 todos" in await tool.execute({"session_id": session.id})
        assert "必须是数组" in await tool.execute({"session_id": session.id, "todos": "not-a-list"})

    def test_tool_contract(self, db: Database) -> None:
        """工具契约字段完整。"""
        registry, _tool_registry, _ss = self._build(db)
        from plugins.tool_todo.main import TodoWriteTool

        tool = TodoWriteTool(registry)
        assert tool.tool_name == "todo_write"
        assert tool.risk_level == "write"
        assert tool.needs_session is True
        assert tool.parameters_schema["required"] == ["todos"]

    async def test_todo_service_unavailable(self) -> None:
        """服务不可用时返回友好错误，不抛异常。"""
        from plugins.tool_todo.main import TodoWriteTool

        out = await TodoWriteTool(ServiceRegistry()).execute({"session_id": "x", "todos": []})
        assert "不可用" in out
