"""第三方系统集成 —— REST 接口与飞书状态持久化测试。

HTTP / 子进程通过 monkeypatch 替换为本地桩，无需真实网络或飞书 CLI。
"""

from __future__ import annotations

import json
import os
import sys

import pytest

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from harness.api.rest.integrations import router as integrations_router  # noqa: E402
from plugins.integration_hub.feishu_provider import FeishuIntegration  # noqa: E402
from plugins.integration_hub.integration_base import (  # noqa: E402
    ActionSpec,
    AuthResult,
    IntegrationProvider,
    IntegrationResult,
    get_registry,
)


class _StubProvider(IntegrationProvider):
    provider_id = "stub"
    provider_name = "Stub System"
    supported_modes = ["api"]

    def __init__(self) -> None:
        self._authed = False
        self._last_action: tuple[str, dict] | None = None

    def list_actions(self):
        return [ActionSpec(name="ping", description="ping the system")]

    async def authenticate(self, mode, credentials):
        self._authed = True
        return AuthResult(ok=True, mode=mode, detail="stub authed")

    def is_authenticated(self) -> bool:
        return self._authed

    def disconnect(self) -> None:
        self._authed = False

    async def call(self, action, params):
        self._last_action = (action, params)
        return IntegrationResult(ok=True, data={"echo": action})


@pytest.fixture
def client():
    reg = get_registry()
    stub = _StubProvider()
    reg.register(stub)
    app = FastAPI()
    app.include_router(integrations_router)
    with TestClient(app) as c:
        yield c
    reg._providers.pop("stub", None)


# ── REST 接口 ───────────────────────────────────────
def test_list_integrations(client):
    data = client.get("/api/integrations").json()
    assert any(p["provider_id"] == "stub" for p in data["providers"])


def test_get_integration(client):
    data = client.get("/api/integrations/stub").json()
    assert data["provider_name"] == "Stub System"
    assert data["authenticated"] is False


def test_get_unknown_provider_404(client):
    assert client.get("/api/integrations/nope").status_code == 404


def test_connect_and_call_and_disconnect(client):
    r = client.post("/api/integrations/stub/connect", json={"mode": "api", "credentials": {}})
    assert r.status_code == 200 and r.json()["ok"] is True

    r = client.get("/api/integrations/stub").json()
    assert r["authenticated"] is True

    r = client.post("/api/integrations/stub/call", json={"action": "ping", "params": {"x": 1}})
    assert r.json()["data"]["echo"] == "ping"

    r = client.post("/api/integrations/stub/disconnect", json={})
    assert r.json()["ok"] is True
    assert client.get("/api/integrations/stub").json()["authenticated"] is False


def test_connect_unknown_404(client):
    assert client.post("/api/integrations/nope/connect", json={"mode": "api"}).status_code == 404


def test_call_unknown_404(client):
    assert client.post("/api/integrations/nope/call", json={"action": "x"}).status_code == 404


# ── 飞书状态持久化 / 断开 ────────────────────────────
async def test_feishu_persists_secret_and_token(monkeypatch, tmp_path):
    async def fake_api(*_a, **_k):
        return {"code": 0, "tenant_access_token": "t-xyz", "expire": 7200}

    monkeypatch.setattr(FeishuIntegration, "_api_call", fake_api)
    f = FeishuIntegration(state_dir=str(tmp_path))
    res = await f.authenticate("api", {"app_id": "APP", "app_secret": "SEC"})
    assert res.ok

    state_file = tmp_path / "feishu_state.json"
    assert state_file.exists()
    saved = json.loads(state_file.read_text(encoding="utf-8"))
    assert saved["app_secret"] == "SEC"
    assert saved["app_id"] == "APP"
    assert saved["token"] == "t-xyz"


async def test_feishu_disconnect_clears_state(monkeypatch, tmp_path):
    async def fake_api(*_a, **_k):
        return {"code": 0, "tenant_access_token": "t-xyz", "expire": 7200}

    monkeypatch.setattr(FeishuIntegration, "_api_call", fake_api)
    f = FeishuIntegration(state_dir=str(tmp_path))
    await f.authenticate("api", {"app_id": "APP", "app_secret": "SEC"})
    assert f.is_authenticated()

    f.disconnect()
    assert not f.is_authenticated()
    assert not (tmp_path / "feishu_state.json").exists()


async def test_feishu_load_restores_secret_for_refresh(monkeypatch, tmp_path):
    async def fake_api(*_a, **_k):
        return {"code": 0, "tenant_access_token": "t-new", "expire": 7200}

    monkeypatch.setattr(FeishuIntegration, "_api_call", fake_api)
    f = FeishuIntegration(state_dir=str(tmp_path))
    await f.authenticate("api", {"app_id": "APP", "app_secret": "SEC"})
    # 模拟重启：新建实例从状态文件恢复（含 app_secret，可自动刷新）
    g = FeishuIntegration(state_dir=str(tmp_path))
    assert g._app_secret == "SEC"
    # token 过期后仍能凭借恢复出的 app_secret 刷新
    g._token_expire_at = 0.0
    ok = await g._ensure_token()
    assert ok and g._token == "t-new"


# ── 入站 Channel 端点 ───────────────────────────────
class _ChannelStub(IntegrationProvider):
    provider_id = "chanstub"
    provider_name = "Channel Stub"
    supported_modes = ["channel"]
    channel_capabilities = [{"name": "群聊", "description": "群聊"}]

    def __init__(self) -> None:
        self._running = False
        self._detail = "未启动"

    def list_actions(self):
        return []

    async def authenticate(self, mode, credentials):
        return AuthResult(ok=True, mode=mode)

    def is_authenticated(self) -> bool:
        return True

    async def call(self, action, params):
        return IntegrationResult(ok=False, error="stub")

    def channel_status(self):
        return {"running": self._running, "detail": self._detail}

    async def start_channel(self, credentials):
        self._running = True
        self._detail = "运行"
        return AuthResult(ok=True, mode="channel", detail="已启动")

    async def stop_channel(self):
        self._running = False
        self._detail = "已停止"
        return AuthResult(ok=True, mode="channel", detail="已停止")


@pytest.fixture
def channel_client():
    reg = get_registry()
    stub = _ChannelStub()
    reg.register(stub)
    app = FastAPI()
    app.include_router(integrations_router)
    with TestClient(app) as c:
        yield c
    reg._providers.pop("chanstub", None)


def test_channel_start_stop_status(channel_client):
    r = channel_client.post(
        "/api/integrations/chanstub/channel/start", json={"credentials": {"app_id": "a", "app_secret": "s"}}
    )
    assert r.status_code == 200 and r.json()["ok"] is True

    r = channel_client.post("/api/integrations/chanstub/channel/status", json={}).json()
    assert r["running"] is True

    r = channel_client.post("/api/integrations/chanstub/channel/stop", json={}).json()
    assert r["ok"] is True

    r = channel_client.post("/api/integrations/chanstub/channel/status", json={}).json()
    assert r["running"] is False


def test_channel_unknown_404(channel_client):
    assert channel_client.post("/api/integrations/nope/channel/start", json={}).status_code == 404
