"""API-03 接口层加固回归测试。

覆盖：
- runs resume / memories summarize 需管理员（非管理员 403）。
- RequestValidationError 统一返回 422 格式。
- artifacts 读取端点 offset/limit 边界校验。
"""

from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient

# 测试隔离：本模块依赖 HARNESS_AUTH / HARNESS_DB_PATH 等环境变量来构造应用，
# 但 harness.main.app 与 Database() 都是进程级单例——若其他测试模块已先行 import
# 并启动过该 app，全局 _services / 插件加载状态 / 数据库单例会被遗留，导致本模块在
# 全量 pytest 顺序下 ERR（bootstrap 在错误（或已关闭的）库上建管理员，登录即失败）。
# 这里在 fixture 内 reload 模块 + 清空 Database 单例，确保每次都拿到干净的应用与库。
import harness.infra.database as _db_mod
from harness import main as _harness_main

ADMIN_USERNAME = "api_hardening_admin"
ADMIN_PASSWORD = "api_hardening_secret123"
ALICE_USERNAME = "api_hardening_alice"
ALICE_PASSWORD = "api_hardening_alice1234"


@pytest.fixture
def auth_client(tmp_path: object, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("HARNESS_AUTH", "1")
    monkeypatch.setenv("HARNESS_DB_PATH", str(tmp_path / "api_hardening.db"))
    monkeypatch.setenv("HARNESS_AUTH_ADMIN_USERNAME", ADMIN_USERNAME)
    monkeypatch.setenv("HARNESS_AUTH_ADMIN_PASSWORD", ADMIN_PASSWORD)
    monkeypatch.setenv("HARNESS_AUTH_SECRET", "test-fixed-secret")
    importlib.reload(_harness_main)
    _db_mod._DB_SINGLETON = None
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
def admin_token(auth_client: TestClient) -> str:
    return _login(auth_client, ADMIN_USERNAME, ADMIN_PASSWORD)


@pytest.fixture
def alice_token(auth_client: TestClient) -> str:
    admin = _login(auth_client, ADMIN_USERNAME, ADMIN_PASSWORD)
    r = auth_client.post(
        "/api/users",
        headers={"Authorization": f"Bearer {admin}"},
        json={"username": ALICE_USERNAME, "password": ALICE_PASSWORD, "is_admin": False},
    )
    assert r.status_code == 200, r.text
    tok = _login(auth_client, ALICE_USERNAME, ALICE_PASSWORD)
    yield tok
    user_id = r.json().get("id")
    if user_id:
        auth_client.delete(f"/api/users/{user_id}", headers={"Authorization": f"Bearer {admin}"})


@pytest.fixture
def no_auth_client(tmp_path: object, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """认证关闭（单用户模式）的客户端，用于测试无需鉴权的边界校验。"""
    monkeypatch.delenv("HARNESS_AUTH", raising=False)
    monkeypatch.setenv("HARNESS_DB_PATH", str(tmp_path / "api_hardening_noauth.db"))
    importlib.reload(_harness_main)
    _db_mod._DB_SINGLETON = None
    from harness.main import app

    with TestClient(app) as client:
        yield client


def test_non_admin_cannot_resume_run(auth_client: TestClient, alice_token: str) -> None:
    r = auth_client.post(
        "/api/runs/abc/resume", headers={"Authorization": f"Bearer {alice_token}"}
    )
    assert r.status_code == 403


def test_admin_can_reach_resume_gate(auth_client: TestClient, admin_token: str) -> None:
    # 通过门禁后，因运行期组件未注入而返回 error 字段（200），证明 require_admin 已放行。
    r = auth_client.post(
        "/api/runs/abc/resume", headers={"Authorization": f"Bearer {admin_token}"}
    )
    assert r.status_code == 200
    assert r.json().get("error") is not None


def test_non_admin_cannot_summarize(auth_client: TestClient, alice_token: str) -> None:
    r = auth_client.post(
        "/api/memories/summarize", headers={"Authorization": f"Bearer {alice_token}"}
    )
    assert r.status_code == 403


def test_admin_summarize_reaches_service(auth_client: TestClient, admin_token: str) -> None:
    # 通过门禁后，因总结器未启用而 503（或统一错误码），证明 require_admin 已放行。
    r = auth_client.post(
        "/api/memories/summarize", headers={"Authorization": f"Bearer {admin_token}"}
    )
    assert r.status_code in (503, 200)


def test_validation_error_unified_format(auth_client: TestClient, admin_token: str) -> None:
    # 发送类型错误的参数，验证返回统一 422 格式（含 code/message/detail/trace_id）。
    r = auth_client.post(
        "/api/memories/summarize?max_sessions=not-a-number",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 422
    body = r.json()
    assert body["code"] == "VALIDATION_ERROR"
    assert "message" in body and "trace_id" in body


def test_artifact_offset_limit_validation(no_auth_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    # 认证关闭场景下，直接验证 offset/limit 边界校验（无需真实 ArtifactStore）。
    import harness.api.rest.artifacts as art_mod

    class FakeRecord:
        session_id = ""

        def to_dict(self) -> dict[str, object]:
            return {"id": "abc", "session_id": ""}

    fake_record = FakeRecord()

    class FakeStore:
        def get(self, _aid: str) -> object:
            return fake_record

        def read(self, _aid: str, offset: int = 0, limit: int | None = None) -> str:
            return "content"

    monkeypatch.setattr(art_mod, "_get_service", lambda _reg: FakeStore())
    # 认证关闭语义：归属校验跳过（owner 为空），只测 offset/limit 边界。
    monkeypatch.setattr(art_mod, "_owner_user_id", lambda _req: "")

    # offset 为负 → 400
    r = no_auth_client.get("/api/artifacts/abc?offset=-1")
    assert r.status_code == 400
    assert r.json()["code"] == "ARTIFACT_BAD_RANGE"

    # limit 超限 → 400
    r = no_auth_client.get("/api/artifacts/abc?limit=99999999")
    assert r.status_code == 400
    assert r.json()["code"] == "ARTIFACT_BAD_RANGE"

    # 合法范围 → 200
    r = no_auth_client.get("/api/artifacts/abc?offset=0&limit=100")
    assert r.status_code == 200
    assert r.json()["content"] == "content"
