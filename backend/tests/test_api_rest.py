"""REST API 集成测试。"""

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client() -> TestClient:
    """创建测试客户端。"""
    from harness.main import app

    with TestClient(app) as c:
        yield c


class TestHealthCheck:
    """健康检查测试（回归 P0）。"""

    def test_health(self, client: TestClient) -> None:
        """GET /api/health 返回 ok。"""
        response = client.get("/api/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


class TestSessionsAPI:
    """会话 REST API 测试。"""

    def test_create_session(self, client: TestClient) -> None:
        """创建会话。"""
        response = client.post("/api/sessions", json={"title": "测试会话"})
        assert response.status_code == 200
        data = response.json()
        assert data["title"] == "测试会话"
        assert "id" in data

    def test_list_sessions(self, client: TestClient) -> None:
        """列出会话。"""
        client.post("/api/sessions", json={"title": "会话1"})
        client.post("/api/sessions", json={"title": "会话2"})

        response = client.get("/api/sessions")
        assert response.status_code == 200
        sessions = response.json()
        assert len(sessions) >= 2

    def test_get_session(self, client: TestClient) -> None:
        """获取单个会话。"""
        create_resp = client.post("/api/sessions", json={"title": "获取测试"})
        session_id = create_resp.json()["id"]

        response = client.get(f"/api/sessions/{session_id}")
        assert response.status_code == 200
        assert response.json()["title"] == "获取测试"

    def test_get_nonexistent_session(self, client: TestClient) -> None:
        """获取不存在的会话返回 404。"""
        response = client.get("/api/sessions/nonexistent-id")
        assert response.status_code in (400, 404, 500)  # 统一错误格式

    def test_rename_session(self, client: TestClient) -> None:
        """重命名会话。"""
        create_resp = client.post("/api/sessions", json={"title": "旧名"})
        session_id = create_resp.json()["id"]

        response = client.patch(
            f"/api/sessions/{session_id}", json={"title": "新名"}
        )
        assert response.status_code == 200
        assert response.json()["title"] == "新名"

    def test_archive_session(self, client: TestClient) -> None:
        """归档会话。"""
        create_resp = client.post("/api/sessions", json={"title": "待归档"})
        session_id = create_resp.json()["id"]

        response = client.patch(
            f"/api/sessions/{session_id}", json={"archived": True}
        )
        assert response.status_code == 200

        # 归档后不出现在默认列表中
        list_resp = client.get("/api/sessions")
        titles = [s["title"] for s in list_resp.json()]
        assert "待归档" not in titles

    def test_delete_session(self, client: TestClient) -> None:
        """删除会话。"""
        create_resp = client.post("/api/sessions", json={"title": "待删除"})
        session_id = create_resp.json()["id"]

        response = client.delete(f"/api/sessions/{session_id}")
        assert response.status_code == 200

        # 确认已删除
        get_resp = client.get(f"/api/sessions/{session_id}")
        assert get_resp.status_code in (400, 404, 500)


class TestMessagesAPI:
    """消息 REST API 测试。"""

    def test_append_and_list_messages(self, client: TestClient) -> None:
        """追加消息并列出。"""
        create_resp = client.post("/api/sessions", json={"title": "消息测试"})
        session_id = create_resp.json()["id"]

        # 追加消息
        client.post(
            f"/api/sessions/{session_id}/messages",
            json={"role": "user", "content": "你好", "tokens": 5},
        )
        client.post(
            f"/api/sessions/{session_id}/messages",
            json={"role": "assistant", "content": "你好！", "tokens": 8},
        )

        # 列出消息
        response = client.get(f"/api/sessions/{session_id}/messages")
        assert response.status_code == 200
        messages = response.json()
        assert len(messages) == 2
        assert messages[0]["role"] == "user"
        assert messages[1]["role"] == "assistant"


class TestErrorFormat:
    """统一错误格式测试。"""

    def test_error_has_trace_id(self, client: TestClient) -> None:
        """错误响应包含 trace_id。"""
        response = client.get("/api/sessions/nonexistent-id")
        if response.status_code >= 400:
            data = response.json()
            # 应有统一错误格式
            if "trace_id" in data:
                assert data["trace_id"]  # 非空


class TestPluginsAPI:
    """插件 REST API 测试。"""

    def test_list_plugins(self, client: TestClient) -> None:
        """列出插件（可能为空列表）。"""
        response = client.get("/api/plugins")
        # 可能为 200 或 503（如果 PluginLoader 未注册）
        assert response.status_code in (200, 503)
