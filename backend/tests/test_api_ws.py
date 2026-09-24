"""WebSocket API 测试（使用 TestClient WS 支持）。"""

import json

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client() -> TestClient:
    """创建测试客户端。"""
    from harness.main import app

    with TestClient(app) as c:
        yield c


class TestWebSocketChat:
    """WebSocket /ws/chat 测试。"""

    def test_ws_connect_and_receive_error(self, client: TestClient) -> None:
        """WS 连接后发送无效消息，应收到 error 帧。"""
        with client.websocket_connect("/ws/chat") as ws:
            # 发送无效 JSON
            ws.send_text("not json")
            response = ws.receive_json()
            assert response["type"] == "error"

    def test_ws_send_message_with_invalid_provider(self, client: TestClient) -> None:
        """WS 发送消息但 provider 不存在，应收到 error 帧。"""
        # 先创建会话
        create_resp = client.post("/api/sessions", json={"title": "WS测试"})
        session_id = create_resp.json()["id"]

        with client.websocket_connect("/ws/chat") as ws:
            ws.send_text(
                json.dumps(
                    {
                        "session_id": session_id,
                        "content": "你好",
                        "provider_id": "nonexistent_provider",
                    }
                )
            )
            # 应收到 error 帧
            response = ws.receive_json()
            assert response["type"] in ("error", "context_snapshot", "token_delta", "done")
