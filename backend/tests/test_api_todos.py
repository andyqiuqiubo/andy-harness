"""Todo REST API 测试。"""

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client() -> TestClient:
    """创建测试客户端。"""
    from harness.main import app

    with TestClient(app) as c:
        yield c


def _todo_service() -> object:
    """获取应用内的 TodoService。"""
    from harness.main import _services
    from harness.modules.todo_manager.service import TodoService

    return _services.get(TodoService)


class TestTodosAPI:
    """会话任务清单 REST 测试。"""

    def test_empty_todos(self, client: TestClient) -> None:
        """新会话任务清单为空。"""
        sid = client.post("/api/sessions", json={"title": "Todo API"}).json()["id"]
        resp = client.get(f"/api/sessions/{sid}/todos")
        assert resp.status_code == 200
        data = resp.json()
        assert data["available"] is True
        assert data["todos"] == []

    def test_write_then_read(self, client: TestClient) -> None:
        """服务写入后可通过 REST 读回（含状态统计）。"""
        sid = client.post("/api/sessions", json={"title": "Todo API 2"}).json()["id"]
        service = _todo_service()
        service.replace_todos(  # type: ignore[attr-defined]
            sid,
            [
                {"content": "分析需求", "status": "completed"},
                {"content": "实现功能", "status": "in_progress"},
                {"content": "补充测试", "status": "pending"},
            ],
        )

        resp = client.get(f"/api/sessions/{sid}/todos")
        data = resp.json()
        assert [t["content"] for t in data["todos"]] == [
            "分析需求",
            "实现功能",
            "补充测试",
        ]
        assert data["todos"][1]["status"] == "in_progress"
        assert data["summary"]["completed"] == 1

    def test_put_replace_via_rest(self, client: TestClient) -> None:
        """PUT 覆盖写入：经 REST 写入并读回，且来源 id 被剥离避免撞主键。"""
        sid = client.post("/api/sessions", json={"title": "Todo API PUT"}).json()["id"]
        resp = client.put(
            f"/api/sessions/{sid}/todos",
            json={
                "todos": [
                    {"id": "stale-1", "content": "任务A", "status": "pending"},
                    {"content": "任务B", "status": "completed"},
                ]
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["saved"] == 2
        # 写入的记录不应保留来源 id（"stale-1" 必须消失，新记录由 DB 分配新 id）
        assert all(t["id"] != "stale-1" for t in body["todos"])
        assert [t["content"] for t in body["todos"]] == ["任务A", "任务B"]
        assert body["todos"][1]["status"] == "completed"

    def test_clear_todos(self, client: TestClient) -> None:
        """DELETE 清空任务清单并返回删除条数。"""
        sid = client.post("/api/sessions", json={"title": "Todo API 3"}).json()["id"]
        _todo_service().replace_todos(sid, [{"content": "A"}, {"content": "B"}])  # type: ignore[attr-defined]

        resp = client.delete(f"/api/sessions/{sid}/todos")
        assert resp.status_code == 200
        assert resp.json()["removed"] == 2
        assert client.get(f"/api/sessions/{sid}/todos").json()["todos"] == []

    def test_unknown_session(self, client: TestClient) -> None:
        """不存在的会话返回 404。"""
        resp = client.get("/api/sessions/not-exist/todos")
        assert resp.status_code in (400, 404, 500)
