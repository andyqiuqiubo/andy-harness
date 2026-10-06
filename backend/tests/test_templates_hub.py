"""模板分享中心 API 测试。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client() -> TestClient:
    from harness.main import app

    with TestClient(app) as c:
        yield c


def _seed_session(client: TestClient) -> str:
    sid = client.post("/api/sessions", json={"title": "模板源会话"}).json()["id"]
    client.post(f"/api/sessions/{sid}/messages", json={"role": "user", "content": "帮 {{产品名}} 写一段宣传语"})
    client.post(f"/api/sessions/{sid}/messages", json={"role": "assistant", "content": "好的，宣传语如下……"})
    return sid


class TestTemplates:
    def test_session_template_roundtrip(self, client: TestClient) -> None:
        sid = _seed_session(client)

        # 从会话打包
        tpl = client.post(
            "/api/templates/from-session",
            json={
                "session_id": sid,
                "name": "宣传语会话模板",
                "description": "换个产品名复用",
                "variables": ["产品名"],
            },
        ).json()
        assert tpl["type"] == "session"
        assert "产品名" in tpl["vars"]

        # 实例化：占位符渲染；末尾 user 提问不落库，作为 pending_message 返回
        inst = client.post(
            f"/api/templates/{tpl['id']}/instantiate",
            json={"vars": {"产品名": "超级水杯"}},
        ).json()
        assert inst["type"] == "session"
        assert inst["pending_message"] is None  # 源会话以 assistant 回答结尾，无需自动发送
        new_sid = inst["session_id"]

        msgs = client.get(f"/api/sessions/{new_sid}/messages").json()
        assert any("超级水杯" in str(m.get("content")) for m in msgs)
        assert not any("{{产品名}}" in str(m.get("content")) for m in msgs)

        # 列表与删除
        listed = client.get("/api/templates?type=session").json()
        assert any(t["id"] == tpl["id"] for t in listed)
        assert client.delete(f"/api/templates/{tpl['id']}").json()["status"] == "deleted"

    def test_prompt_template_instantiate_defers_last_user_message(self, client: TestClient) -> None:
        """提示词模板：末尾提问不落库（pending_message 返回），由会话页走真实发送链路。"""
        tpl = client.post(
            "/api/templates",
            json={
                "type": "prompt",
                "name": "代码评审提问",
                "payload": {"messages": [{"role": "user", "content": "请评审以下 {{语言}} 代码，关注性能与可读性。"}]},
            },
        ).json()
        inst = client.post(f"/api/templates/{tpl['id']}/instantiate", json={"vars": {"语言": "Python"}}).json()
        assert inst["type"] == "prompt"
        assert "Python" in inst["pending_message"]
        # 提问未落库：新会话当前没有任何消息
        msgs = client.get(f"/api/sessions/{inst['session_id']}/messages").json()
        assert msgs == []

    def test_session_template_with_trailing_user_message_defers(self, client: TestClient) -> None:
        """会话模板以 user 提问结尾时同样返回 pending_message（前端自动发送）。"""
        payload = {
            "messages": [
                {"role": "user", "content": "第一问"},
                {"role": "assistant", "content": "第一答"},
                {"role": "user", "content": "请继续，{{重点}} 方向展开"},
            ]
        }
        tpl = client.post(
            "/api/templates",
            json={"type": "session", "name": "续聊模板", "payload": payload},
        ).json()
        inst = client.post(f"/api/templates/{tpl['id']}/instantiate", json={"vars": {"重点": "性能"}}).json()
        assert "性能" in inst["pending_message"]
        msgs = client.get(f"/api/sessions/{inst['session_id']}/messages").json()
        # 前两轮落库；末尾提问不落库
        assert [m["role"] for m in msgs] == ["user", "assistant"]

    def test_workflow_template_instantiate(self, client: TestClient) -> None:
        tpl = client.post(
            "/api/templates",
            json={
                "type": "workflow",
                "name": "周报生成流水线",
                "payload": {
                    "vars": ["本周要点"],
                    "steps": [
                        {
                            "name": "整理",
                            "provider_id": "deepseek",
                            "model": "deepseek-flash",
                            "user_prompt": "整理：{{本周要点}}",
                        }
                    ],
                },
            },
        ).json()
        inst = client.post(f"/api/templates/{tpl['id']}/instantiate", json={"vars": {}}).json()
        assert inst["type"] == "workflow"
        assert inst["workflow_id"]

    def test_invalid_type_rejected(self, client: TestClient) -> None:
        res = client.post("/api/templates", json={"type": "skill", "name": "x"})
        assert res.status_code == 400

    def test_unknown_template_404(self, client: TestClient) -> None:
        res = client.post("/api/templates/nope/instantiate", json={"vars": {}})
        assert res.status_code == 404
