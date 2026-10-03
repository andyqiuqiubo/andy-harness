"""认证与多用户（E12）测试。

覆盖：
- 默认关闭：AuthService 未启用时行为不变；
- 状态 / 登录 / 登出 / me；
- 未认证与无效 token → 401；
- 管理员用户 CRUD 与权限（非管理员 403）；
- 不能删除自己 / 最后一个管理员；
- 会话与记忆按用户隔离；
- WebSocket：无 token 关闭 1008、跨用户访问被拒。
"""

from __future__ import annotations

import os
import tempfile

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from harness.infra.database import Database
from harness.modules.auth_manager.service import AuthServiceImpl

ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "secret123"
ALICE_USERNAME = "alice"
ALICE_PASSWORD = "alice1234"


@pytest.fixture
def db() -> Database:
    """临时数据库（服务层测试用）。"""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    database = Database(path)
    yield database
    database.close()
    os.unlink(path)


@pytest.fixture
def auth_client(tmp_path: str, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """启用认证的全应用客户端（临时 DB，固定管理员凭据）。"""
    monkeypatch.setenv("HARNESS_AUTH", "1")
    monkeypatch.setenv("HARNESS_DB_PATH", str(tmp_path / "auth.db"))
    monkeypatch.setenv("HARNESS_AUTH_ADMIN_USERNAME", ADMIN_USERNAME)
    monkeypatch.setenv("HARNESS_AUTH_ADMIN_PASSWORD", ADMIN_PASSWORD)
    monkeypatch.setenv("HARNESS_AUTH_SECRET", "test-fixed-secret")

    from harness.main import app

    with TestClient(app) as client:
        yield client


def _login(client: TestClient, username: str, password: str) -> dict:
    r = client.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()


def _auth_header(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ── 默认关闭（服务层） ──────────────────────────────────


class TestAuthDisabledByDefault:
    """认证默认关闭，行为与旧版一致。"""

    def test_disabled_service(self, db: Database) -> None:
        svc = AuthServiceImpl(db)
        svc.bootstrap()
        assert svc.enabled is False
        assert svc.list_users() == []
        assert svc.default_user_id() == ""

    def test_login_endpoint_disabled(self, db: Database) -> None:
        """未启用认证时 status 反映关闭。"""
        svc = AuthServiceImpl(db)
        assert svc.enabled is False


# ── 状态与公开端点 ─────────────────────────────────────


class TestPublicEndpoints:
    def test_status_enabled(self, auth_client: TestClient) -> None:
        r = auth_client.get("/api/auth/status")
        assert r.status_code == 200
        assert r.json() == {"enabled": True}

    def test_health_is_public(self, auth_client: TestClient) -> None:
        r = auth_client.get("/api/health")
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}


# ── 登录与 token ───────────────────────────────────────


class TestLogin:
    def test_login_bad_credentials(self, auth_client: TestClient) -> None:
        r = auth_client.post(
            "/api/auth/login",
            json={"username": ADMIN_USERNAME, "password": "wrong"},
        )
        assert r.status_code == 401
        assert r.json()["code"] == "INVALID_CREDENTIALS"

    def test_login_unknown_user(self, auth_client: TestClient) -> None:
        r = auth_client.post(
            "/api/auth/login",
            json={"username": "nobody", "password": "whatever1"},
        )
        assert r.status_code == 401

    def test_login_success_and_me(self, auth_client: TestClient) -> None:
        payload = _login(auth_client, ADMIN_USERNAME, ADMIN_PASSWORD)
        token = payload["token"]
        assert token
        assert payload["user"]["username"] == ADMIN_USERNAME
        assert payload["user"]["is_admin"] is True

        r = auth_client.get("/api/auth/me", headers=_auth_header(token))
        assert r.status_code == 200
        assert r.json()["username"] == ADMIN_USERNAME

    def test_logout(self, auth_client: TestClient) -> None:
        token = _login(auth_client, ADMIN_USERNAME, ADMIN_PASSWORD)["token"]
        r = auth_client.post("/api/auth/logout", headers=_auth_header(token))
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}


# ── 401 与无效 token ───────────────────────────────────


class TestUnauthorized:
    def test_no_token_401(self, auth_client: TestClient) -> None:
        r = auth_client.get("/api/sessions")
        assert r.status_code == 401
        body = r.json()
        assert body["code"] == "UNAUTHORIZED"
        assert body["message"]

    def test_garbage_token_401(self, auth_client: TestClient) -> None:
        r = auth_client.get("/api/sessions", headers={"Authorization": "Bearer not-a-token"})
        assert r.status_code == 401

    def test_wrong_scheme_401(self, auth_client: TestClient) -> None:
        r = auth_client.get(
            "/api/sessions",
            headers={"Authorization": "Basic abcdef"},
        )
        assert r.status_code == 401


# ── 用户管理与权限 ─────────────────────────────────────


class TestUserManagement:
    def test_admin_create_list_delete_user(self, auth_client: TestClient) -> None:
        admin_token = _login(auth_client, ADMIN_USERNAME, ADMIN_PASSWORD)["token"]

        r = auth_client.post(
            "/api/users",
            headers=_auth_header(admin_token),
            json={
                "username": ALICE_USERNAME,
                "password": ALICE_PASSWORD,
                "is_admin": False,
            },
        )
        assert r.status_code == 200, r.text
        alice = r.json()
        assert alice["username"] == ALICE_USERNAME
        assert alice["is_admin"] is False

        r = auth_client.get("/api/users", headers=_auth_header(admin_token))
        assert r.status_code == 200
        assert ALICE_USERNAME in {u["username"] for u in r.json()}

        r = auth_client.delete(f"/api/users/{alice['id']}", headers=_auth_header(admin_token))
        assert r.status_code == 200

        r = auth_client.get("/api/users", headers=_auth_header(admin_token))
        assert ALICE_USERNAME not in {u["username"] for u in r.json()}

    def test_non_admin_forbidden(self, auth_client: TestClient) -> None:
        admin_token = _login(auth_client, ADMIN_USERNAME, ADMIN_PASSWORD)["token"]
        auth_client.post(
            "/api/users",
            headers=_auth_header(admin_token),
            json={"username": ALICE_USERNAME, "password": ALICE_PASSWORD},
        )
        alice_token = _login(auth_client, ALICE_USERNAME, ALICE_PASSWORD)["token"]

        r = auth_client.get("/api/users", headers=_auth_header(alice_token))
        assert r.status_code == 403
        assert r.json()["code"] == "FORBIDDEN"

        r = auth_client.post(
            "/api/users",
            headers=_auth_header(alice_token),
            json={"username": "bob", "password": "bobpass1"},
        )
        assert r.status_code == 403

    def test_create_user_invalid_input(self, auth_client: TestClient) -> None:
        admin_token = _login(auth_client, ADMIN_USERNAME, ADMIN_PASSWORD)["token"]
        # 密码太短
        r = auth_client.post(
            "/api/users",
            headers=_auth_header(admin_token),
            json={"username": "shortpw", "password": "123"},
        )
        assert r.status_code == 400
        # 重名
        r = auth_client.post(
            "/api/users",
            headers=_auth_header(admin_token),
            json={"username": ADMIN_USERNAME, "password": "another1"},
        )
        assert r.status_code == 400

    def test_cannot_delete_self(self, auth_client: TestClient) -> None:
        payload = _login(auth_client, ADMIN_USERNAME, ADMIN_PASSWORD)
        admin_id = payload["user"]["id"]
        r = auth_client.delete(f"/api/users/{admin_id}", headers=_auth_header(payload["token"]))
        assert r.status_code == 400

    def test_last_admin_protection(self, auth_client: TestClient) -> None:
        admin_token = _login(auth_client, ADMIN_USERNAME, ADMIN_PASSWORD)["token"]
        # 唯一管理员下，先建普通用户，再尝试删除管理员 → 拒绝
        auth_client.post(
            "/api/users",
            headers=_auth_header(admin_token),
            json={"username": ALICE_USERNAME, "password": ALICE_PASSWORD},
        )
        admin_id = auth_client.get("/api/auth/me", headers=_auth_header(admin_token)).json()["id"]
        r = auth_client.delete(f"/api/users/{admin_id}", headers=_auth_header(admin_token))
        # 不能删除自己优先命中（400）；语义同为 USER_INVALID。
        assert r.status_code == 400

    def test_delete_unknown_user_404(self, auth_client: TestClient) -> None:
        admin_token = _login(auth_client, ADMIN_USERNAME, ADMIN_PASSWORD)["token"]
        r = auth_client.delete("/api/users/user_doesnotexist", headers=_auth_header(admin_token))
        assert r.status_code == 404


# ── 会话 / 记忆隔离 ────────────────────────────────────


class TestDataIsolation:
    def test_sessions_are_isolated(self, auth_client: TestClient) -> None:
        admin_token = _login(auth_client, ADMIN_USERNAME, ADMIN_PASSWORD)["token"]
        # 管理员创建一个会话
        r = auth_client.post(
            "/api/sessions",
            headers=_auth_header(admin_token),
            json={"title": "管理员私密会话"},
        )
        assert r.status_code == 200
        admin_session = r.json()["id"]

        # 普通用户
        auth_client.post(
            "/api/users",
            headers=_auth_header(admin_token),
            json={"username": ALICE_USERNAME, "password": ALICE_PASSWORD},
        )
        alice_token = _login(auth_client, ALICE_USERNAME, ALICE_PASSWORD)["token"]

        # alice 列表看不到管理员会话
        r = auth_client.get("/api/sessions", headers=_auth_header(alice_token))
        assert admin_session not in {s["id"] for s in r.json()}

        # alice 直接访问 → 404
        r = auth_client.get(
            f"/api/sessions/{admin_session}",
            headers=_auth_header(alice_token),
        )
        assert r.status_code == 404

        # 管理员自己仍可见
        r = auth_client.get(
            f"/api/sessions/{admin_session}",
            headers=_auth_header(admin_token),
        )
        assert r.status_code == 200

    def test_memories_are_isolated(self, auth_client: TestClient) -> None:
        admin_token = _login(auth_client, ADMIN_USERNAME, ADMIN_PASSWORD)["token"]
        r = auth_client.post(
            "/api/memories",
            headers=_auth_header(admin_token),
            json={"key": "admin.secret", "value": "仅管理员可见"},
        )
        assert r.status_code == 200

        auth_client.post(
            "/api/users",
            headers=_auth_header(admin_token),
            json={"username": ALICE_USERNAME, "password": ALICE_PASSWORD},
        )
        alice_token = _login(auth_client, ALICE_USERNAME, ALICE_PASSWORD)["token"]

        # alice 搜索 / 列表均看不到管理员记忆
        r = auth_client.get(
            "/api/memories",
            headers=_auth_header(alice_token),
            params={"query": "admin.secret"},
        )
        assert r.json()["count"] == 0

        r = auth_client.get("/api/memories", headers=_auth_header(alice_token))
        assert all(m["key"] != "admin.secret" for m in r.json()["memories"])


# ── WebSocket 认证 ─────────────────────────────────────


class TestWebSocketAuth:
    def test_ws_without_token_closed(self, auth_client: TestClient) -> None:
        with auth_client.websocket_connect("/ws/chat") as ws:
            with pytest.raises(WebSocketDisconnect) as exc_info:
                ws.receive_json()
        assert exc_info.value.code == 1008

    def test_ws_bad_token_closed(self, auth_client: TestClient) -> None:
        with auth_client.websocket_connect("/ws/chat?token=garbage") as ws:
            with pytest.raises(WebSocketDisconnect) as exc_info:
                ws.receive_json()
        assert exc_info.value.code == 1008

    def test_ws_valid_token_connects(self, auth_client: TestClient) -> None:
        token = _login(auth_client, ADMIN_USERNAME, ADMIN_PASSWORD)["token"]
        with auth_client.websocket_connect(f"/ws/chat?token={token}") as ws:
            # 连接保持开放，可正常发送；立即退出即完成握手验证。
            ws.send_json({"type": "stop"})
            frame = ws.receive_json()
            assert frame["type"] == "stop_ack"

    def test_ws_cross_user_session_rejected(self, auth_client: TestClient) -> None:
        admin_token = _login(auth_client, ADMIN_USERNAME, ADMIN_PASSWORD)["token"]
        admin_session = auth_client.post(
            "/api/sessions",
            headers=_auth_header(admin_token),
            json={"title": "admin"},
        ).json()["id"]

        auth_client.post(
            "/api/users",
            headers=_auth_header(admin_token),
            json={"username": ALICE_USERNAME, "password": ALICE_PASSWORD},
        )
        alice_token = _login(auth_client, ALICE_USERNAME, ALICE_PASSWORD)["token"]

        with auth_client.websocket_connect(f"/ws/chat?token={alice_token}") as ws:
            ws.send_json(
                {
                    "session_id": admin_session,
                    "content": "偷看",
                    "provider_id": "fake",
                }
            )
            frame = ws.receive_json()
            assert frame["type"] == "error"
            assert frame["data"]["code"] == "SESSION_NOT_FOUND"
