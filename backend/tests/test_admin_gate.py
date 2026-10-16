"""管理面门禁（require_admin）与插件配置脱敏测试（SEC-13 / API-02）。

覆盖两类回归：
1. 会改变系统状态的写端点必须挂 `require_admin`：认证启用时非管理员 403；
2. 插件配置 GET 必须脱敏：schema 标记 `"secret": true`（或键名含 key/token
   等）的字段不得原样返回给调用方。
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from harness.api.rest.plugins import _mask_secret_fields

ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "secret123"
# 用户名必须全局唯一且与 tests/test_auth.py 错开：Database() 是进程单例，
# 多个测试文件共用同一个库，重名会让后跑的用例撞上「用户名已存在」。
ALICE_USERNAME = "gate_alice"
ALICE_PASSWORD = "alice1234"


@pytest.fixture
def auth_client(tmp_path: str, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """启用认证的全应用客户端（临时 DB，固定管理员凭据）。"""
    monkeypatch.setenv("HARNESS_AUTH", "1")
    monkeypatch.setenv("HARNESS_DB_PATH", str(tmp_path / "gate.db"))
    monkeypatch.setenv("HARNESS_AUTH_ADMIN_USERNAME", ADMIN_USERNAME)
    monkeypatch.setenv("HARNESS_AUTH_ADMIN_PASSWORD", ADMIN_PASSWORD)
    monkeypatch.setenv("HARNESS_AUTH_SECRET", "test-fixed-secret")

    # SEC-28：登录限速计数按 IP+用户名累计（模块全局），TestClient 的 client.host
    # 恒为 "testclient"。若跨测试文件/类不清零，先跑的限速测试残留会误限速本文件的
    # admin 登录。每个测试前后清零，与 test_auth.py 的 auth_client 保持一致。
    from harness.api.rest import auth as auth_route
    from harness.main import app

    auth_route._login_attempts.clear()
    with TestClient(app) as client:
        yield client
    auth_route._login_attempts.clear()


def _login(client: TestClient, username: str, password: str) -> str:
    r = client.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return str(r.json()["token"])


@pytest.fixture
def alice_token(auth_client: TestClient) -> str:
    admin = _login(auth_client, ADMIN_USERNAME, ADMIN_PASSWORD)
    r = auth_client.post(
        "/api/users",
        headers={"Authorization": f"Bearer {admin}"},
        json={"username": ALICE_USERNAME, "password": ALICE_PASSWORD, "is_admin": False},
    )
    assert r.status_code == 200, r.text
    token = _login(auth_client, ALICE_USERNAME, ALICE_PASSWORD)
    yield token
    # 库是进程共享的单例，用完必须删掉，否则会污染后续测试文件
    user_id = r.json().get("id")
    if user_id:
        auth_client.delete(f"/api/users/{user_id}", headers={"Authorization": f"Bearer {admin}"})


@pytest.fixture
def admin_token(auth_client: TestClient) -> str:
    return _login(auth_client, ADMIN_USERNAME, ADMIN_PASSWORD)


# ── 1) 写端点管理员门禁 ────────────────────────────────
class TestAdminGate:
    GATED = [
        ("POST", "/api/skills/reload"),
        ("POST", "/api/skills/marketplace/does-not-exist/install"),
        ("POST", "/api/skills/install-external"),
        ("DELETE", "/api/skills/marketplace/does-not-exist"),
        ("PATCH", "/api/skills/code-review"),
        ("POST", "/api/providers"),
        ("DELETE", "/api/providers/custom_whatever"),
    ]

    def test_non_admin_rejected(self, auth_client: TestClient, alice_token: str) -> None:
        """非管理员调用写端点一律 403。"""
        headers = {"Authorization": f"Bearer {alice_token}"}
        for method, path in self.GATED:
            resp = auth_client.request(method, path, headers=headers, json={})
            assert resp.status_code == 403, f"{method} {path} -> {resp.status_code}"

    def test_admin_allowed(self, auth_client: TestClient, admin_token: str) -> None:
        """管理员不得被门禁拦截（403 才说明门禁误伤）。"""
        headers = {"Authorization": f"Bearer {admin_token}"}
        for method, path in self.GATED:
            resp = auth_client.request(method, path, headers=headers, json={})
            assert resp.status_code != 403, f"{method} {path} 被误拦: {resp.status_code}"

    def test_unauthenticated_rejected(self, auth_client: TestClient) -> None:
        """未认证访问写端点 → 401。"""
        for method, path in self.GATED:
            resp = auth_client.request(method, path, json={})
            assert resp.status_code == 401, f"{method} {path} -> {resp.status_code}"


# ── 2) 插件配置脱敏 ────────────────────────────────────
def test_mask_secret_fields_by_schema() -> None:
    """schema 标记 secret 的字段应被替换为占位符，非敏感字段保持原样。"""
    data = {
        "config_schema": {
            "type": "object",
            "properties": {
                "api_key": {"type": "string", "secret": True},
                "model": {"type": "string"},
            },
        },
        "config": {"api_key": "sk-live-abcdef", "model": "gpt-4o"},
    }
    out = _mask_secret_fields(dict(data))
    assert out["config"]["api_key"] != "sk-live-abcdef"
    assert "sk-live" not in str(out["config"]["api_key"])
    assert out["config"]["model"] == "gpt-4o"


def test_mask_secret_fields_by_name() -> None:
    """schema 缺失时，键名含 key/token/password 的字段同样脱敏。"""
    data = {
        "config": {
            "access_token": "tok-123",
            "app_secret": "sec-456",
            "password": "pw",
            "working_dir": "/data/ws",
        }
    }
    out = _mask_secret_fields(dict(data))
    for k in ("access_token", "app_secret", "password"):
        assert out["config"][k] != data["config"][k]
    assert out["config"]["working_dir"] == "/data/ws"


def test_mask_keeps_empty_values() -> None:
    """空值无需脱敏（前端要区分『未配置』与『已配置但隐藏』）。"""
    data = {"config": {"api_key": ""}}
    assert _mask_secret_fields(dict(data))["config"]["api_key"] == ""


def test_plugin_config_endpoint_masks_secret(auth_client: TestClient, admin_token: str) -> None:
    """GET /api/plugins/{id}/config 不得返回真实密钥。"""
    headers = {"Authorization": f"Bearer {admin_token}"}
    resp = auth_client.get("/api/plugins/computer_use/config", headers=headers)
    if resp.status_code == 404:
        pytest.skip("computer_use 插件未加载")
    assert resp.status_code == 200
    cfg = resp.json().get("config", {})
    api_key = cfg.get("api_key")
    # 未配置时为空；已配置时必须是占位符，绝不能是真实值
    if api_key:
        assert "sk-" not in str(api_key)
        assert os.environ.get("COMPUTER_USE_API_KEY", "") != api_key
