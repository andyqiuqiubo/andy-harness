"""SEC-31：定时任务 REST 路由门禁（require_admin + 归属）。

回归防护：
- 认证启用时，非管理员对任意定时任务写/读端点一律 403；
- 管理员可正常 CRUD；
- 认证关闭（单用户模式）时，require_admin 返回 None 放行（由 test_scheduler.py 覆盖）。
"""

from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient

# 测试隔离：harness.main.app 与 Database() 都是进程级单例，全量 pytest 顺序下若本
# 模块之前已有其他模块 import 并启动过该 app，全局 _services / 插件加载状态 / 数据库
# 单例会被遗留，导致本模块 ERR（bootstrap 在错误的库上建管理员，登录即失败）。
# 此处 fixture 内 reload 模块 + 清空 Database 单例，确保每次都拿到干净的应用与库。
import harness.infra.database as _db_mod
from harness import main as _harness_main

ADMIN_USERNAME = "sched_admin"
ADMIN_PASSWORD = "secret123"
ALICE_USERNAME = "sched_alice"
ALICE_PASSWORD = "alice1234"


@pytest.fixture
def auth_client(tmp_path: object, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("HARNESS_AUTH", "1")
    monkeypatch.setenv("HARNESS_DB_PATH", str(tmp_path / "sched_gate.db"))
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


def _make_task(client: TestClient, token: str) -> object:
    return client.post(
        "/api/schedules",
        headers={"Authorization": f"Bearer {token}"},
        json={"name": "t", "prompt": "p", "schedule": {"type": "daily", "time": "09:00"}},
    )


def test_non_admin_cannot_create_schedule(auth_client: TestClient, alice_token: str) -> None:
    r = _make_task(auth_client, alice_token)
    assert r.status_code == 403


def test_non_admin_cannot_list_schedules(auth_client: TestClient, alice_token: str) -> None:
    r = auth_client.get("/api/schedules", headers={"Authorization": f"Bearer {alice_token}"})
    assert r.status_code == 403


def test_non_admin_cannot_delete_schedule(auth_client: TestClient, alice_token: str) -> None:
    r = auth_client.delete(
        "/api/schedules/nope", headers={"Authorization": f"Bearer {alice_token}"}
    )
    assert r.status_code == 403


def test_non_admin_cannot_run_or_clear(auth_client: TestClient, alice_token: str) -> None:
    assert (
        auth_client.post(
            "/api/schedules/nope/run", headers={"Authorization": f"Bearer {alice_token}"}
        ).status_code
        == 403
    )
    assert (
        auth_client.delete(
            "/api/schedules/nope/runs", headers={"Authorization": f"Bearer {alice_token}"}
        ).status_code
        == 403
    )


def test_admin_can_create_list_and_delete(auth_client: TestClient, admin_token: str) -> None:
    r = _make_task(auth_client, admin_token)
    assert r.status_code == 200, r.text
    tid = str(r.json()["task"]["id"])
    lst = auth_client.get("/api/schedules", headers={"Authorization": f"Bearer {admin_token}"})
    assert lst.status_code == 200
    assert any(t["id"] == tid for t in lst.json()["tasks"])
    d = auth_client.delete(
        f"/api/schedules/{tid}", headers={"Authorization": f"Bearer {admin_token}"}
    )
    assert d.status_code == 200


def test_owner_can_access_own_task_but_other_admin_cannot(tmp_path: object) -> None:
    """归属校验（service 层 fail-closed）：用错误 user_id 查询应视为不存在。"""
    from harness.infra.database import Database
    from harness.modules.scheduler.service import SchedulerService

    db = Database(str(tmp_path / "own.db"))
    svc = SchedulerService(db)
    task = svc.create_task(
        {"name": "own", "prompt": "p", "schedule": {"type": "daily", "time": "09:00"}},
        user_id="owner-x",
    )
    assert svc.get_task(task.id, user_id="owner-x") is not None
    assert svc.get_task(task.id, user_id="someone-else") is None
    # 越权删除应失败（返回 False），不误删他人任务
    assert svc.delete_task(task.id, user_id="someone-else") is False
    svc.delete_task(task.id, user_id="owner-x")
