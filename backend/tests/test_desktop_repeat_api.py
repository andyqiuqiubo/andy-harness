"""desktop_repeat REST / 鉴权 / 并发 / 边界分支测试（阶段二）。

覆盖点（均对应真实用户路径，非内部实现细节）：
- 请求校验（limit 边界、类型错误、未知工作流/运行）；
- 语义状态码（未知运行 404、SSE 未知运行不挂起、非法裁决 422）；
- 鉴权门禁（认证启用：无 token 401、非管理员 403、管理员放行；SSE ?token=）；
- 并发（同时开始录制 / 同时回放只有一个获得输入租约）；
- 干跑静态校验（非法步骤在零副作用阶段即失败）。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import harness.modules.desktop_repeat.service as svc_mod
from harness.api.rest import desktop_repeat as route
from harness.infra.database import Database
from harness.modules.desktop_repeat.config import RecordConfig, RepeatConfig
from harness.modules.desktop_repeat.errors import RepeatError
from harness.modules.desktop_repeat.service import DesktopRepeatService
from harness.modules.workflows.service import WorkflowService
from harness.kernel.services import ServiceRegistry
from tests.repeat_fakes import FakeEnv, FakeInjector, FakeInspector, FakeLease, FakeWindows


class _WF:
    def __init__(self) -> None:
        self.wfs: dict[str, dict] = {}

    def create_workflow(self, name: str, description: str, graph: dict) -> dict:
        wid = f"wf-{len(self.wfs) + 1}"
        self.wfs[wid] = {"id": wid, "name": name, "description": description,
                         "graph": graph, "version": 1}
        return self.wfs[wid]

    def update_workflow(self, wid: str, name: str, description: str, graph: dict) -> dict:
        self.wfs[wid].update({"graph": graph, "name": name, "description": description})
        return self.wfs[wid]

    def get_workflow(self, wid: str) -> dict | None:
        return self.wfs.get(wid)


def _graph(action: str = "click") -> dict[str, Any]:
    return {
        "nodes": [
            {"id": "n0", "type": "start", "data": {"params": []}},
            {"id": "n1", "type": "gui", "data": {
                "sid": "s01", "action": action, "risk": "read",
                "target": {"locators": [{"kind": "uia", "by": "automation_id",
                                          "value": "btnSave", "confidence": 0.9}]},
                "params": {}, "meta": {"confidence": 0.9}}},
            {"id": "n2", "type": "end", "data": {}},
        ],
        "edges": [{"id": "e0", "source": "n0", "target": "n1"},
                  {"id": "e1", "source": "n1", "target": "n2"}],
        "meta": {"env_base": {}},
    }


@pytest.fixture()
def api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "sessions"
    root.mkdir()
    db = Database(db_path=str(tmp_path / "api.db"))
    wf = _WF()
    svc = DesktopRepeatService(db, root, RepeatConfig.load({"countdown_s": 0, "retries": 0}),
                               workflow_service=wf)

    def fake_platforms(self):
        if self._platform is None:
            self._platform = {
                "capture": None,
                "inspector": FakeInspector({"automation_id:btnSave": "hit"}),
                "grabber": None, "injector": FakeInjector(),
                "windows": FakeWindows(), "env": FakeEnv(), "lease": FakeLease(),
                "takeover": __import__(
                    "harness.modules.desktop_repeat.executor.pacing", fromlist=["NullTakeover"]
                ).NullTakeover(),
            }
        return self._platform

    monkeypatch.setattr(DesktopRepeatService, "_platforms", fake_platforms, raising=False)
    monkeypatch.setattr(route, "_svc", lambda: svc)

    # 进程级全局隔离：harness.main._services 是**模块级单例**。若本文件之前跑过
    # test_auth / test_admin_gate（它们在 monkeypatch HARNESS_AUTH=1 之后才 import
    # harness.main，触发 setup 把 enabled=True 的 AuthService 注册进该全局，且
    # monkeypatch 只回滚 env、不回滚注册表），这里的"裸 app"就会看到认证已启用，
    # SSE 端点返回 401 → 仅全量跑时失败、单跑却全绿。本文件的语义是"未启用认证"，
    # 故显式换成空注册表，使 _require_admin_sse 走"无 AuthService → 放行"分支。
    import sys

    main_mod = sys.modules.get("harness.main")
    if main_mod is not None and getattr(main_mod, "_services", None) is not None:
        monkeypatch.setattr(main_mod, "_services", ServiceRegistry(), raising=False)

    app = FastAPI()
    app.include_router(route.router)
    from harness.api.errors import register_error_handlers

    register_error_handlers(app)  # 生产由 main 装配；单路由测试需同步，否则 APIError 直接抛出
    with TestClient(app) as client:
        yield client, svc, wf
    try:
        svc.shutdown()
    except Exception:
        pass


# ── 请求校验与状态码 ─────────────────────────────────────────────
def test_status_payload(api) -> None:
    client, _svc, _wf = api
    r = client.get("/api/desktop-repeat/status")
    assert r.status_code == 200
    body = r.json()
    assert {"available", "active_session", "running_runs", "config"} <= set(body)


@pytest.mark.parametrize("qs", ["limit=0", "limit=201", "limit=abc", "offset=-1"])
def test_session_list_rejects_bad_paging(api, qs: str) -> None:
    client, _svc, _wf = api
    assert client.get(f"/api/desktop-repeat/sessions?{qs}").status_code == 422


def test_record_body_type_error(api) -> None:
    client, _svc, _wf = api
    assert client.post("/api/desktop-repeat/sessions",
                       json={"countdown_s": "abc"}).status_code == 422


def test_delete_missing_session_404(api) -> None:
    client, _svc, _wf = api
    r = client.delete("/api/desktop-repeat/sessions/r-not-exist")
    assert r.status_code == 404
    assert r.json()["code"] == "NOT_FOUND"


def test_get_missing_run_404(api) -> None:
    client, _svc, _wf = api
    r = client.get("/api/desktop-repeat/runs/run-nope")
    assert r.status_code == 404


def test_sse_unknown_run_404_not_hang(api) -> None:
    client, _svc, _wf = api
    # 若实现错误（先建流再判断），该请求会永久挂起；TestClient 会卡死。
    r = client.get("/api/desktop-repeat/runs/run-nope/events")
    assert r.status_code == 404


def test_resume_invalid_decision_422(api) -> None:
    client, _svc, _wf = api
    r = client.post("/api/desktop-repeat/runs/run-nope/resume", json={"decision": "bogus"})
    assert r.status_code == 422
    assert r.json()["code"] == "RP-6004"


def test_resume_missing_run_422(api) -> None:
    client, _svc, _wf = api
    r = client.post("/api/desktop-repeat/runs/run-nope/resume", json={"decision": "skip"})
    assert r.status_code == 422


def test_abort_missing_run_422(api) -> None:
    client, _svc, _wf = api
    assert client.post("/api/desktop-repeat/runs/run-nope/abort").status_code == 422


def test_dry_run_unknown_workflow_422(api) -> None:
    client, _svc, _wf = api
    r = client.post("/api/desktop-repeat/dry-run", json={"workflow_id": "wf-nope"})
    assert r.status_code == 422


def test_run_missing_params_returns_checklist(api) -> None:
    client, _svc, wf = api
    g = _graph()
    g["nodes"][0]["data"]["params"] = [
        {"name": "cust", "type": "string", "required": True, "source": "runtime_only"}]
    g["nodes"][0]["data"]["variables"] = g["nodes"][0]["data"]["params"]
    w = wf.create_workflow("带参", "", g)
    r = client.post("/api/desktop-repeat/runs", json={"workflow_id": w["id"], "params": {}})
    assert r.status_code == 200
    body = r.json()
    assert body.get("rejected") is True
    assert body["params"][0]["name"] == "cust"


def test_run_graph_hash_mismatch_422(api) -> None:
    client, _svc, wf = api
    w = wf.create_workflow("哈希", "", _graph())
    r = client.post("/api/desktop-repeat/runs",
                    json={"workflow_id": w["id"], "params": {},
                          "expected_graph_sha256": "0" * 64})
    assert r.status_code == 422


def test_dry_run_reports_invalid_step(api) -> None:
    """非法步骤必须在干跑阶段暴露（而不是体检全绿、回放才失败）。"""
    client, _svc, wf = api
    g = _graph()
    g["nodes"][1]["data"]["action"] = "explode"   # 非法动作
    w = wf.create_workflow("非法", "", g)
    r = client.post("/api/desktop-repeat/dry-run", json={"workflow_id": w["id"]})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "RP-6004"
    assert "动作非法" in body["error"]["message"]


def test_sse_terminal_run_closes_immediately(api) -> None:
    """总线为空（重启后）的已终结运行：SSE 必须合成终态并立即收束。"""
    client, svc, _wf = api
    svc._repo.insert_run("run-done", "wf-x", {"status": "running", "vars": {}}, steps_total=1)
    svc._repo.finish_run("run-done", {"status": "succeeded", "stats": {"done": 1},
                                      "duration_ms": 5, "finished_at": ""})
    r = client.get("/api/desktop-repeat/runs/run-done/events")
    assert r.status_code == 200
    assert "run_done" in r.text


# ── 并发 ─────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_concurrent_start_recording_single_winner(monkeypatch) -> None:
    import tempfile

    root = Path(tempfile.mkdtemp(prefix="repeat-conc-")) / "s"
    root.mkdir(parents=True)
    svc = DesktopRepeatService(Database(db_path=str(root / "c.db")), root,
                               RepeatConfig.load({"countdown_s": 0}))
    monkeypatch.setattr(svc, "_spawn_recorder", lambda sdir: None)
    lease = FakeLease()

    def _platforms(_self):
        # 必须复用同一个 lease 实例：每次调用新建会让并发双方各拿一把"锁"
        return {"capture": None, "inspector": FakeInspector({}), "grabber": None,
                "injector": FakeInjector(), "windows": FakeWindows(), "env": FakeEnv(),
                "lease": lease, "takeover": __import__(
                    "harness.modules.desktop_repeat.executor.pacing",
                    fromlist=["NullTakeover"]).NullTakeover()}

    monkeypatch.setattr(type(svc), "_platforms", _platforms, raising=False)
    results = await asyncio.gather(
        svc.start_recording(RecordConfig(label="A", countdown_s=0)),
        svc.start_recording(RecordConfig(label="B", countdown_s=0)),
        return_exceptions=True)
    winners = [r for r in results if isinstance(r, dict)]
    losses = [r for r in results if isinstance(r, RepeatError)]
    assert len(winners) == 1 and len(losses) == 1
    assert losses[0].code == "RP-5004"
    # 清理：停掉心跳任务并释放租约
    svc._active_session = None
    if svc._hb_task:
        svc._hb_task.cancel()
    svc._release_lease()


@pytest.mark.asyncio
async def test_concurrent_runs_single_winner(monkeypatch) -> None:
    import tempfile

    root = Path(tempfile.mkdtemp(prefix="repeat-conc2-")) / "s"
    root.mkdir(parents=True)
    db = Database(db_path=str(root / "c.db"))
    wf = _WF()
    svc = DesktopRepeatService(db, root, RepeatConfig.load({"retries": 0}), workflow_service=wf)
    lease = FakeLease()

    def _platforms(_self):
        return {"capture": None,
                "inspector": FakeInspector({"automation_id:btnSave": "hit"}),
                "grabber": None, "injector": FakeInjector(), "windows": FakeWindows(),
                "env": FakeEnv(), "lease": lease, "takeover": __import__(
                    "harness.modules.desktop_repeat.executor.pacing",
                    fromlist=["NullTakeover"]).NullTakeover()}

    monkeypatch.setattr(type(svc), "_platforms", _platforms, raising=False)
    w1 = wf.create_workflow("一", "", _graph())
    w2 = wf.create_workflow("二", "", _graph())
    r1, r2 = await asyncio.gather(
        svc.start_run(w1["id"], {}, unattended=True),
        svc.start_run(w2["id"], {}, unattended=True),
        return_exceptions=True)
    ok = [r for r in (r1, r2) if isinstance(r, dict) and r.get("run_id")]
    bad = [r for r in (r1, r2) if isinstance(r, Exception)]
    assert len(ok) == 1 and len(bad) == 1
    assert isinstance(bad[0], RepeatError) and bad[0].code == "RP-5004"
    await asyncio.sleep(0.25)
    svc.shutdown()


# ── 鉴权（整应用 + 认证启用） ─────────────────────────────────────
@pytest.fixture()
def auth_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("HARNESS_AUTH", "1")
    monkeypatch.setenv("HARNESS_DB_PATH", str(tmp_path / "auth.db"))
    monkeypatch.setenv("HARNESS_AUTH_ADMIN_USERNAME", "admin")
    monkeypatch.setenv("HARNESS_AUTH_ADMIN_PASSWORD", "secret123")
    monkeypatch.setenv("HARNESS_AUTH_SECRET", "repeat-test-secret")
    from harness.api.rest import auth as auth_route
    from harness.main import app

    auth_route._login_attempts.clear()
    with TestClient(app) as client:
        yield client
    auth_route._login_attempts.clear()


def test_repeat_requires_auth(auth_client: TestClient) -> None:
    r = auth_client.get("/api/desktop-repeat/status")
    assert r.status_code in (401, 403)


def test_repeat_non_admin_forbidden(auth_client: TestClient) -> None:
    admin = auth_client.post("/api/auth/login",
                             json={"username": "admin", "password": "secret123"})
    assert admin.status_code == 200, admin.text
    at = admin.json()["token"]
    u = auth_client.post("/api/users", headers={"Authorization": f"Bearer {at}"},
                         json={"username": "repeat_alice", "password": "alice1234",
                               "is_admin": False})
    assert u.status_code == 200, u.text
    alice = auth_client.post("/api/auth/login",
                              json={"username": "repeat_alice", "password": "alice1234"})
    token = alice.json()["token"]
    h = {"Authorization": f"Bearer {token}"}
    assert auth_client.get("/api/desktop-repeat/status", headers=h).status_code == 403
    # SSE 走 ?token=（EventSource 不能带自定义头）
    assert auth_client.get(
        f"/api/desktop-repeat/runs/x/events?token={token}").status_code == 403
    uid = u.json().get("id")
    if uid:
        auth_client.delete(f"/api/users/{uid}", headers={"Authorization": f"Bearer {at}"})
