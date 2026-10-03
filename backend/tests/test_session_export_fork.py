"""会话导出 / 导入 / 分叉测试。"""

from __future__ import annotations

import os
import tempfile
from typing import Any

import pytest
from fastapi.testclient import TestClient

from harness.infra.database import Database
from harness.modules.session_manager.service import SessionServiceImpl


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
def service(db: Database) -> SessionServiceImpl:
    """会话服务。"""
    return SessionServiceImpl(db)


def _seed(service: SessionServiceImpl) -> tuple[str, list[str]]:
    """建一个有 3 条消息的会话，返回 (session_id, message_ids)。"""
    session = service.create_session(title="原始会话")
    ids = []
    for role, content in (
        ("user", "第一个问题"),
        ("assistant", "第一个回答"),
        ("user", "第二个问题"),
    ):
        ids.append(service.append_message(session.id, role, content).id)
    return session.id, ids


class TestExport:
    """导出测试。"""

    def test_export_structure(self, service: SessionServiceImpl) -> None:
        """导出结构含版本、会话与消息。"""
        sid, _ids = _seed(service)
        payload = service.export_session(sid)
        assert payload is not None
        assert payload["version"] == 1
        assert payload["session"]["title"] == "原始会话"
        assert len(payload["messages"]) == 3
        assert payload["messages"][0]["content"] == "第一个问题"

    def test_export_unknown_session(self, service: SessionServiceImpl) -> None:
        """不存在的会话返回 None。"""
        assert service.export_session("nope") is None

    def test_export_keeps_tool_calls(self, service: SessionServiceImpl) -> None:
        """工具调用消息（tool_calls / tool_call_id）被完整导出。"""
        session = service.create_session(title="工具会话")
        service.append_message(
            session.id,
            "assistant",
            "",
            tool_calls=[{"id": "c1", "function": {"name": "calculator"}}],
        )
        service.append_message(session.id, "tool", "42", tool_call_id="c1")
        payload = service.export_session(session.id)
        assert payload is not None
        assert payload["messages"][0]["tool_calls"]
        assert payload["messages"][1]["tool_call_id"] == "c1"


class TestImport:
    """导入测试。"""

    def test_import_restores_messages(self, service: SessionServiceImpl) -> None:
        """导入后消息内容与顺序一致，且是新会话。"""
        sid, _ids = _seed(service)
        payload = service.export_session(sid)
        assert payload is not None

        imported = service.import_session(payload)
        assert imported.id != sid
        assert imported.title == "原始会话"
        messages = service.list_messages(imported.id)
        assert [m.content for m in messages] == [
            "第一个问题",
            "第一个回答",
            "第二个问题",
        ]

    def test_import_with_custom_title(self, service: SessionServiceImpl) -> None:
        """可指定导入后的标题。"""
        sid, _ids = _seed(service)
        payload = service.export_session(sid)
        assert payload is not None
        imported = service.import_session(payload, title="我的副本")
        assert imported.title == "我的副本"

    def test_import_isolated_from_origin(self, service: SessionServiceImpl) -> None:
        """导入的会话与原始会话互不影响。"""
        sid, _ids = _seed(service)
        payload = service.export_session(sid)
        assert payload is not None
        imported = service.import_session(payload)
        service.append_message(imported.id, "user", "只在副本里")

        assert service.count_messages(sid) == 3
        assert service.count_messages(imported.id) == 4

    def test_import_tolerates_bad_payload(self, service: SessionServiceImpl) -> None:
        """非法条目被跳过，不抛异常。"""
        imported = service.import_session(
            {
                "session": {"title": "脏数据"},
                "messages": [
                    {"role": "unknown_role", "content": "奇怪角色"},
                    "not-a-dict",
                    {"role": "user", "content": "正常"},
                ],
            }
        )
        messages = service.list_messages(imported.id)
        assert len(messages) == 2
        assert messages[0].role == "user"  # 非法角色回落为 user


class TestFork:
    """分叉测试。"""

    def test_fork_copies_all_messages(self, service: SessionServiceImpl) -> None:
        """不带 at_message_id 时复制全部历史。"""
        sid, _ids = _seed(service)
        forked = service.fork_session(sid)
        assert forked is not None
        assert forked.title == "原始会话（分支）"
        assert service.count_messages(forked.id) == 3

    def test_fork_at_message(self, service: SessionServiceImpl) -> None:
        """指定消息时只复制到该条（含）。"""
        sid, ids = _seed(service)
        forked = service.fork_session(sid, at_message_id=ids[1])
        assert forked is not None
        assert service.count_messages(forked.id) == 2
        assert [m.content for m in service.list_messages(forked.id)] == [
            "第一个问题",
            "第一个回答",
        ]

    def test_fork_isolated(self, service: SessionServiceImpl) -> None:
        """分支里继续对话不影响原会话。"""
        sid, _ids = _seed(service)
        forked = service.fork_session(sid)
        assert forked is not None
        service.append_message(forked.id, "user", "分支上的新问题")

        assert service.count_messages(sid) == 3
        assert service.count_messages(forked.id) == 4

    def test_fork_unknown_session(self, service: SessionServiceImpl) -> None:
        """会话不存在返回 None。"""
        assert service.fork_session("nope") is None

    def test_fork_unknown_message(self, service: SessionServiceImpl) -> None:
        """消息不存在返回 None（不做部分复制）。"""
        sid, _ids = _seed(service)
        assert service.fork_session(sid, at_message_id="bad-id") is None


class TestSessionPortabilityAPI:
    """导出/导入/分叉 REST 测试。"""

    @pytest.fixture
    def client(self) -> TestClient:
        from harness.main import app

        with TestClient(app) as c:
            yield c

    def _create(self, client: TestClient) -> str:
        return str(client.post("/api/sessions", json={"title": "API 会话"}).json()["id"])

    def test_export_endpoint(self, client: TestClient) -> None:
        """GET /api/sessions/{id}/export 返回可下载结构。"""
        sid = self._create(client)
        client.post(
            f"/api/sessions/{sid}/messages",
            json={"role": "user", "content": "你好"},
        )
        resp = client.get(f"/api/sessions/{sid}/export")
        assert resp.status_code == 200
        data = resp.json()
        assert data["version"] == 1
        assert data["messages"][0]["content"] == "你好"

    def test_export_unknown(self, client: TestClient) -> None:
        """导出不存在的会话返回错误码。"""
        resp = client.get("/api/sessions/nope/export")
        assert resp.status_code in (400, 404, 500)

    def test_import_endpoint(self, client: TestClient) -> None:
        """POST /api/sessions/import 导入并生成新会话。"""
        sid = self._create(client)
        client.post(
            f"/api/sessions/{sid}/messages",
            json={"role": "user", "content": "导出我"},
        )
        payload = client.get(f"/api/sessions/{sid}/export").json()

        resp = client.post("/api/sessions/import", json={"payload": payload, "title": "导入副本"})
        assert resp.status_code == 200
        new_id = resp.json()["id"]
        assert new_id != sid
        assert resp.json()["title"] == "导入副本"
        assert len(client.get(f"/api/sessions/{new_id}/messages").json()) == 1

    def test_fork_endpoint(self, client: TestClient) -> None:
        """POST /api/sessions/{id}/fork 生成分支会话。"""
        sid = self._create(client)
        for content in ("A", "B", "C"):
            client.post(
                f"/api/sessions/{sid}/messages",
                json={"role": "user", "content": content},
            )
        messages = client.get(f"/api/sessions/{sid}/messages").json()
        target = messages[1]["id"]

        resp = client.post(f"/api/sessions/{sid}/fork", json={"at_message_id": target})
        assert resp.status_code == 200
        forked_id = resp.json()["id"]
        assert forked_id != sid
        assert len(client.get(f"/api/sessions/{forked_id}/messages").json()) == 2

    def test_fork_unknown_message(self, client: TestClient) -> None:
        """分叉到不存在的消息返回 404。"""
        sid = self._create(client)
        resp = client.post(f"/api/sessions/{sid}/fork", json={"at_message_id": "bad"})
        assert resp.status_code in (400, 404, 500)


def test_export_includes_todos(db: Database) -> None:
    """导出包含任务清单（Todo 服务可用时）。"""
    from harness.kernel.services import ServiceRegistry
    from harness.modules.todo_manager.service import TodoService, TodoServiceImpl

    registry = ServiceRegistry()
    registry.register(TodoService, TodoServiceImpl(db), owner="test")
    service = SessionServiceImpl(db, services=registry)

    session = service.create_session(title="带任务")
    service.append_message(session.id, "user", "拆解任务")
    registry.get(TodoService).replace_todos(session.id, [{"content": "步骤一", "status": "completed"}])

    payload = service.export_session(session.id)
    assert payload is not None
    assert payload["todos"][0]["content"] == "步骤一"

    imported = service.import_session(payload, title="副本")
    todos: list[dict[str, Any]] = [t.to_dict() for t in registry.get(TodoService).list_todos(imported.id)]
    assert todos[0]["content"] == "步骤一"


class TestForkKeepsToolMessages:
    """分叉截断在「带 tool_calls 的 assistant 消息」上时必须带上后续 tool 消息。"""

    def test_fork_at_assistant_with_tool_calls(self, service: SessionServiceImpl) -> None:
        session = service.create_session(title="工具分叉")
        service.append_message(session.id, "user", "查一下天气")
        assistant = service.append_message(
            session.id,
            "assistant",
            "",
            tool_calls=[
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "get_weather", "arguments": "{}"},
                }
            ],
        )
        service.append_message(session.id, "tool", "晴 26 度", tool_call_id="call_1")
        service.append_message(session.id, "assistant", "今天晴天")
        service.append_message(session.id, "user", "谢谢")

        forked = service.fork_session(session.id, at_message_id=assistant.id)
        assert forked is not None
        msgs = service.list_messages(forked.id)
        # user + assistant(tool_calls) + tool —— 不能把 tool 响应丢掉
        assert [m.role for m in msgs] == ["user", "assistant", "tool"]
        assert msgs[2].tool_call_id == "call_1"


class TestDeleteTurn:
    """物理删除整轮问答。"""

    def test_delete_turn_removes_only_that_turn(self, service: SessionServiceImpl) -> None:
        session = service.create_session(title="删轮")
        u1 = service.append_message(session.id, "user", "问题一")
        service.append_message(session.id, "assistant", "回答一")
        u2 = service.append_message(session.id, "user", "问题二")
        service.append_message(session.id, "assistant", "回答二")

        removed = service.delete_turn(session.id, u1.id)
        assert removed == 2
        msgs = service.list_messages(session.id)
        assert [m.content for m in msgs] == ["问题二", "回答二"]

        # 删除第二轮（最后一轮）
        assert service.delete_turn(session.id, u2.id) == 2
        assert service.list_messages(session.id) == []

    def test_delete_turn_includes_tool_messages(self, service: SessionServiceImpl) -> None:
        session = service.create_session(title="带工具")
        u1 = service.append_message(session.id, "user", "跑代码")
        service.append_message(
            session.id,
            "assistant",
            "",
            tool_calls=[
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "code_runner", "arguments": "{}"},
                }
            ],
        )
        service.append_message(session.id, "tool", "结果", tool_call_id="c1")
        service.append_message(session.id, "assistant", "跑完了")

        assert service.delete_turn(session.id, u1.id) == 4
        assert service.list_messages(session.id) == []

    def test_delete_turn_errors(self, service: SessionServiceImpl) -> None:
        session = service.create_session(title="错误分支")
        assistant = service.append_message(session.id, "assistant", "回答")
        # 不存在的消息
        assert service.delete_turn(session.id, "nope") == -1
        # 不是 user 消息
        assert service.delete_turn(session.id, assistant.id) == -2


class TestMessageDeleteAPI:
    """REST：物理删除消息 / 整轮。"""

    @pytest.fixture
    def client(self) -> Any:
        from fastapi.testclient import TestClient

        from harness.main import app

        with TestClient(app) as c:
            yield c

    def test_delete_turn_via_api(self, client: Any) -> None:
        sid = client.post("/api/sessions", json={"title": "API 删轮"}).json()["id"]
        try:
            u1 = client.post(
                f"/api/sessions/{sid}/messages",
                json={"role": "user", "content": "问一"},
            ).json()["id"]
            client.post(
                f"/api/sessions/{sid}/messages",
                json={"role": "assistant", "content": "答一"},
            )
            u2 = client.post(
                f"/api/sessions/{sid}/messages",
                json={"role": "user", "content": "问二"},
            ).json()["id"]
            client.post(
                f"/api/sessions/{sid}/messages",
                json={"role": "assistant", "content": "答二"},
            )

            resp = client.delete(f"/api/sessions/{sid}/messages/{u1}?with_turn=true")
            assert resp.status_code == 200
            assert resp.json()["mode"] == "turn"
            assert resp.json()["removed"] == 2

            left = client.get(f"/api/sessions/{sid}/messages").json()
            assert [m["content"] for m in left] == ["问二", "答二"]

            # 单条删除（with_turn=false）
            resp = client.delete(f"/api/sessions/{sid}/messages/{u2}?with_turn=false")
            assert resp.json()["mode"] == "message"
            assert len(client.get(f"/api/sessions/{sid}/messages").json()) == 1
        finally:
            client.delete(f"/api/sessions/{sid}")

    def test_delete_message_404(self, client: Any) -> None:
        sid = client.post("/api/sessions", json={"title": "404"}).json()["id"]
        try:
            assert client.delete(f"/api/sessions/{sid}/messages/nope").status_code == 404
            assert client.delete("/api/sessions/nope/messages/nope").status_code == 404
        finally:
            client.delete(f"/api/sessions/{sid}")
