"""多渠道 Channel 测试（E1）—— ChannelManager + Webhook + REST。"""

from __future__ import annotations

import os
import tempfile
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from harness.api.errors import register_error_handlers
from harness.api.rest.channels import router as channels_router
from harness.api.rest.channels import setup_channel_routes
from harness.engine.tool_registry import ToolRegistry
from harness.infra.database import Database
from harness.kernel.hooks import HookManager
from harness.kernel.services import ServiceRegistry
from harness.modules.channel_manager.service import (
    ChannelError,
    ChannelManager,
    ChannelManagerConfig,
    InboundMessage,
    WebhookChannel,
)
from harness.modules.context_manager.service import (
    ContextService,
    ContextServiceImpl,
)
from harness.modules.model_manager.provider_registry import ProviderRegistry
from harness.modules.session_manager.service import (
    SessionService,
    SessionServiceImpl,
)


class FakeProvider:
    """假 provider：把用户消息原样回显。"""

    def __init__(self, **kwargs: Any) -> None:
        self.calls = 0

    async def chat(
        self,
        messages: list[dict[str, Any]],
        model: str,
        stream: bool = True,
        **kwargs: Any,
    ) -> Any:
        self.calls += 1
        last = messages[-1].get("content", "") if messages else ""
        yield {"delta": f"回复:{last}"}


@pytest.fixture
def services() -> Any:
    """带完整最小栈的 ServiceRegistry。"""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db = Database(path)
    registry = ServiceRegistry()

    registry.register(Database, db, owner="test")
    session_service = SessionServiceImpl(db)
    registry.register(SessionService, session_service, owner="test")
    registry.register(
        ContextService,
        ContextServiceImpl(services=registry),
        owner="test",
    )
    registry.register(HookManager, HookManager(), owner="test")
    registry.register(ToolRegistry, ToolRegistry(), owner="test")

    provider_registry = ProviderRegistry()
    provider_registry.register_provider(
        "fake",
        FakeProvider,  # type: ignore[arg-type]
        {"api_key": "k", "name": "fake", "enabled": True},
    )
    registry.register(ProviderRegistry, provider_registry, owner="test")

    yield registry

    db.close()
    os.unlink(path)


@pytest.fixture
def manager(services: ServiceRegistry) -> ChannelManager:
    """带 Webhook 渠道的管理器。"""
    mgr = ChannelManager(
        services,
        config=ChannelManagerConfig(provider_id="fake", model="fake-model"),
    )
    mgr.register(WebhookChannel(name="webhook"))
    return mgr


class TestChannelManager:
    """ChannelManager 核心测试。"""

    @pytest.mark.asyncio
    async def test_webhook_process_and_reply(self, manager: ChannelManager) -> None:
        """入站消息跑出终答，且会话与消息落库。"""
        result = await manager.process(InboundMessage(channel="webhook", user_id="u1", text="你好"))
        assert result.error is None
        assert result.reply == "回复:你好"

        session_service = manager._services.get(SessionService)  # noqa: SLF001
        session = session_service.get_session(result.session_id)
        assert session.title == "webhook · u1"
        messages = session_service.list_messages(result.session_id)
        assert [m.role for m in messages] == ["user", "assistant"]

    @pytest.mark.asyncio
    async def test_session_reuse_same_user(self, manager: ChannelManager) -> None:
        """同一渠道用户复用同一会话；不同用户不同会话。"""
        r1 = await manager.process(InboundMessage(channel="webhook", user_id="u1", text="第一条"))
        r2 = await manager.process(InboundMessage(channel="webhook", user_id="u1", text="第二条"))
        r3 = await manager.process(InboundMessage(channel="webhook", user_id="u2", text="第一条"))
        assert r1.session_id == r2.session_id
        assert r1.session_id != r3.session_id

    @pytest.mark.asyncio
    async def test_link_persists_across_managers(self, services: ServiceRegistry) -> None:
        """channel_links 持久化：新管理器仍解析到同一会话。"""
        mgr1 = ChannelManager(
            services,
            config=ChannelManagerConfig(provider_id="fake"),
        )
        mgr1.register(WebhookChannel(name="webhook"))
        r1 = await mgr1.process(InboundMessage(channel="webhook", user_id="u9", text="hi"))

        mgr2 = ChannelManager(
            services,
            config=ChannelManagerConfig(provider_id="fake"),
        )
        mgr2.register(WebhookChannel(name="webhook"))
        r2 = await mgr2.process(InboundMessage(channel="webhook", user_id="u9", text="again"))
        assert r1.session_id == r2.session_id

    @pytest.mark.asyncio
    async def test_unknown_channel_raises(self, manager: ChannelManager) -> None:
        """未知渠道报错。"""
        with pytest.raises(ChannelError) as exc:
            await manager.process(InboundMessage(channel="nope", user_id="u1", text="hi"))
        assert exc.value.code == "CHANNEL_NOT_FOUND"

    @pytest.mark.asyncio
    async def test_empty_message_raises(self, manager: ChannelManager) -> None:
        """空消息报错。"""
        with pytest.raises(ChannelError) as exc:
            await manager.process(InboundMessage(channel="webhook", user_id="u1", text="   "))
        assert exc.value.code == "EMPTY_MESSAGE"

    @pytest.mark.asyncio
    async def test_no_provider_raises(self, services: ServiceRegistry) -> None:
        """无任何 provider 时报 NO_PROVIDER。"""
        empty = ServiceRegistry()
        empty.register(ProviderRegistry, ProviderRegistry(), owner="test")
        mgr = ChannelManager(empty)
        mgr.register(WebhookChannel(name="webhook"))
        with pytest.raises(ChannelError) as exc:
            await mgr.process(InboundMessage(channel="webhook", user_id="u1", text="hi"))
        assert exc.value.code == "NO_PROVIDER"

    @pytest.mark.asyncio
    async def test_webhook_allowed_users(self, services: ServiceRegistry) -> None:
        """Webhook 允许名单：名单外用户被拒。"""
        mgr = ChannelManager(
            services,
            config=ChannelManagerConfig(provider_id="fake"),
        )
        mgr.register(WebhookChannel(name="webhook", allowed_users=["ok-user"]))
        with pytest.raises(ChannelError) as exc:
            await mgr.process(InboundMessage(channel="webhook", user_id="bad-user", text="hi"))
        assert exc.value.code == "USER_NOT_ALLOWED"

        result = await mgr.process(InboundMessage(channel="webhook", user_id="ok-user", text="hi"))
        assert result.reply == "回复:hi"

    def test_webhook_secret_check(self) -> None:
        """Webhook 密钥校验。"""
        ch = WebhookChannel(secret="abc")
        assert ch.check_secret("abc") is True
        assert ch.check_secret("wrong") is False
        # 未配置密钥时一律放行
        assert WebhookChannel().check_secret("") is True

    @pytest.mark.asyncio
    async def test_unregister(self, manager: ChannelManager) -> None:
        """注销渠道后不可再用。"""
        await manager.unregister("webhook")
        assert manager.get("webhook") is None


class TestChannelREST:
    """渠道 REST 路由测试。"""

    @pytest.fixture
    def client(self, services: ServiceRegistry) -> Any:
        """仅含渠道路由的测试应用。"""
        mgr = ChannelManager(
            services,
            config=ChannelManagerConfig(provider_id="fake"),
        )
        mgr.register(WebhookChannel(name="webhook", secret="s3cr3t"))
        services.register(ChannelManager, mgr, owner="test")

        app = FastAPI()
        register_error_handlers(app)
        setup_channel_routes(services)
        app.include_router(channels_router)
        with TestClient(app) as c:
            yield c

    def test_list_channels(self, client: TestClient) -> None:
        """GET /api/channels 列出渠道。"""
        resp = client.get("/api/channels")
        assert resp.status_code == 200
        names = [item["name"] for item in resp.json()]
        assert "webhook" in names

    def test_inbound_returns_reply(self, client: TestClient) -> None:
        """POST inbound 同步返回终答。"""
        resp = client.post(
            "/api/channels/inbound/webhook",
            json={"user_id": "u1", "text": "你好"},
            headers={"X-Channel-Secret": "s3cr3t"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["reply"] == "回复:你好"
        assert data["session_id"]

    def test_inbound_bad_secret(self, client: TestClient) -> None:
        """密钥错误返回 403。"""
        resp = client.post(
            "/api/channels/inbound/webhook",
            json={"user_id": "u1", "text": "你好"},
            headers={"X-Channel-Secret": "wrong"},
        )
        assert resp.status_code == 403
        assert resp.json()["code"] == "INVALID_SECRET"

    def test_inbound_unknown_channel(self, client: TestClient) -> None:
        """不存在的 Webhook 渠道返回 404。"""
        resp = client.post(
            "/api/channels/inbound/nope",
            json={"user_id": "u1", "text": "你好"},
        )
        assert resp.status_code == 404
        assert resp.json()["code"] == "CHANNEL_NOT_FOUND"

    def test_channel_detail(self, client: TestClient) -> None:
        """GET /{name} 返回渠道状态。"""
        resp = client.get("/api/channels/webhook")
        assert resp.status_code == 200
        assert resp.json()["name"] == "webhook"
