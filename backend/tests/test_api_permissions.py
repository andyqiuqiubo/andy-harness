"""权限 REST API 与 WebSocket 人工确认链路测试。"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client() -> TestClient:
    """创建测试客户端（触发 lifespan 加载全部插件）。"""
    from harness.main import app

    with TestClient(app) as c:
        yield c


class TestPermissionsAPI:
    """权限 REST API 测试。"""

    def test_get_permissions(self, client: TestClient) -> None:
        """GET /api/permissions 返回策略与工具风险清单。"""
        resp = client.get("/api/permissions")
        assert resp.status_code == 200
        data = resp.json()
        assert data["available"] is True
        assert data["mode"] == "confirm_dangerous"
        assert "auto" in data["modes"]
        assert "deny" in data["actions"]

        risks = {t["name"]: t["risk"] for t in data["tools"]}
        assert risks.get("code_runner") == "dangerous"
        assert risks.get("calculator") == "read"
        assert risks.get("theme_switcher") == "write"

    def test_mode_round_trip(self, client: TestClient) -> None:
        """切换策略模式后再改回默认值。"""
        original = client.get("/api/permissions").json()["mode"]
        try:
            resp = client.put("/api/permissions", json={"mode": "confirm_all"})
            assert resp.status_code == 200
            assert resp.json()["mode"] == "confirm_all"
            # 生效：只读工具也需确认
            tools = client.get("/api/permissions").json()["tools"]
            calc = next(t for t in tools if t["name"] == "calculator")
            assert calc["action"] == "confirm"

            # 无效模式被拒绝
            bad = client.put("/api/permissions", json={"mode": "nonsense"})
            assert bad.status_code == 400
        finally:
            client.put("/api/permissions", json={"mode": original})

    def test_put_returns_full_state(self, client: TestClient) -> None:
        """PUT /api/permissions 必须回传与 GET 一致完整状态（含 modes/actions/available）。

        回归保护：曾因 PUT 只回传 mode/overrides/tools，前端整体替换 state 后
        modes 清空导致全局策略下拉选中文字消失、available 丢失导致下拉被禁用。
        """
        original = client.get("/api/permissions").json()["mode"]
        try:
            resp = client.put("/api/permissions", json={"mode": "confirm_all"})
            assert resp.status_code == 200
            data = resp.json()
            # 关键字段必须存在，否则前端合并后仍可能缺字段
            assert data["available"] is True
            assert "modes" in data and "confirm_all" in data["modes"]
            assert "actions" in data and "deny" in data["actions"]
            assert data["mode"] == "confirm_all"
            # 与 GET 形状一致
            get_data = client.get("/api/permissions").json()
            assert set(data.keys()) == set(get_data.keys())
        finally:
            client.put("/api/permissions", json={"mode": original})

    def test_tool_override_round_trip(self, client: TestClient) -> None:
        """单工具覆盖：deny → 清除。"""
        try:
            resp = client.put("/api/permissions/calculator", json={"action": "deny"})
            assert resp.status_code == 200
            tools = client.get("/api/permissions").json()["tools"]
            calc = next(t for t in tools if t["name"] == "calculator")
            assert calc["action"] == "deny"

            resp = client.put("/api/permissions/calculator", json={"action": None})
            assert resp.status_code == 200
            tools = client.get("/api/permissions").json()["tools"]
            calc = next(t for t in tools if t["name"] == "calculator")
            assert calc["action"] == "allow"
        finally:
            client.put("/api/permissions/calculator", json={"action": None})


class FakeProvider:
    """Mock provider —— 首轮调用 code_runner，次轮给出终答。"""

    def __init__(self) -> None:
        self.calls = 0

    async def chat(
        self,
        messages: list[dict[str, str]],
        model: str,
        stream: bool = True,
        **kwargs: Any,
    ) -> Any:
        self.calls += 1
        if self.calls == 1:
            yield {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call_1",
                        "function": {
                            "name": "code_runner",
                            "arguments": '{"code": "print(1)", "language": "python"}',
                        },
                    }
                ]
            }
        else:
            yield {"delta": "执行完毕"}


class TestWebSocketConfirmFlow:
    """WebSocket 人工确认链路（confirm_request → confirm_reply）。"""

    def test_confirm_request_and_approve(self, client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
        """危险工具触发确认请求，用户批准后继续执行并收到工具结果。"""
        from harness.api.ws import chat as ws_chat
        from harness.modules.model_manager.provider_registry import ProviderRegistry

        monkeypatch.setattr(ws_chat, "CONFIRM_TIMEOUT", 15.0)
        fake = FakeProvider()
        monkeypatch.setattr(
            ProviderRegistry,
            "get_provider",
            lambda self, provider_id, **kwargs: fake,
        )

        session_id = client.post("/api/sessions", json={"title": "WS 确认测试"}).json()["id"]

        with client.websocket_connect("/ws/chat") as ws:
            ws.send_text(
                json.dumps(
                    {
                        "session_id": session_id,
                        "content": "帮我跑一段代码",
                        "provider_id": "deepseek",
                        "model": "test-model",
                    }
                )
            )

            # 收集帧直到收到 confirm_request
            request_frame = None
            for _ in range(20):
                frame = ws.receive_json()
                if frame["type"] == "confirm_request":
                    request_frame = frame
                    break
                if frame["type"] in ("done", "error"):
                    break

            assert request_frame is not None, "未收到 confirm_request 帧"
            data = request_frame["data"]
            assert data["tool_name"] == "code_runner"
            assert data["risk"] == "dangerous"
            assert data["request_id"]

            # 用户批准
            ws.send_text(
                json.dumps(
                    {
                        "type": "confirm_reply",
                        "data": {"request_id": data["request_id"], "approved": True},
                    }
                )
            )

            # 批准后应继续执行并结束
            saw_tool_event = False
            for _ in range(20):
                frame = ws.receive_json()
                if frame["type"] == "tool_event":
                    saw_tool_event = True
                if frame["type"] in ("done", "error"):
                    break

            assert saw_tool_event, "批准后未产生工具事件"

    def test_confirm_request_and_reject(self, client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
        """用户拒绝时工具不执行，错误信息回传模型。"""
        from harness.api.ws import chat as ws_chat
        from harness.modules.model_manager.provider_registry import ProviderRegistry

        monkeypatch.setattr(ws_chat, "CONFIRM_TIMEOUT", 15.0)
        fake = FakeProvider()
        monkeypatch.setattr(
            ProviderRegistry,
            "get_provider",
            lambda self, provider_id, **kwargs: fake,
        )

        session_id = client.post("/api/sessions", json={"title": "WS 拒绝测试"}).json()["id"]

        with client.websocket_connect("/ws/chat") as ws:
            ws.send_text(
                json.dumps(
                    {
                        "session_id": session_id,
                        "content": "帮我跑一段代码",
                        "provider_id": "deepseek",
                        "model": "test-model",
                    }
                )
            )

            request_frame = None
            for _ in range(20):
                frame = ws.receive_json()
                if frame["type"] == "confirm_request":
                    request_frame = frame
                    break
                if frame["type"] in ("done", "error"):
                    break
            assert request_frame is not None

            ws.send_text(
                json.dumps(
                    {
                        "type": "confirm_reply",
                        "data": {
                            "request_id": request_frame["data"]["request_id"],
                            "approved": False,
                        },
                    }
                )
            )

            tool_error = ""
            for _ in range(20):
                frame = ws.receive_json()
                if frame["type"] == "tool_event":
                    payload = frame["data"]
                    tool_error = str(payload.get("error") or "")
                if frame["type"] in ("done", "error"):
                    break

            assert "拒绝" in tool_error
