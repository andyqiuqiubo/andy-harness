"""Telegram 渠道测试（E1）。"""

from __future__ import annotations

import asyncio
import os
import tempfile
from typing import Any

import pytest

from harness.engine.tool_registry import ToolRegistry
from harness.infra.database import Database
from harness.kernel.hooks import HookManager
from harness.kernel.services import ServiceRegistry
from harness.modules.channel_manager.service import (
    ChannelManager,
    ChannelManagerConfig,
    InboundMessage,
)
from harness.modules.channel_manager.telegram import (
    HttpResponse,
    HttpTransport,
    TelegramChannel,
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
from tests.test_channels import FakeProvider


def _update(update_id: int, user_id: int, text: str | None, chat_id: int | None = None) -> dict[str, Any]:
    """构造一个 Telegram update。"""
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id + 1,
            "from": {"id": user_id, "username": "tester", "is_bot": False},
            "chat": {"id": chat_id if chat_id is not None else user_id, "type": "private"},
            "text": text,
        },
    }


class FakeTransport(HttpTransport):
    """脚本化 HTTP 传输。"""

    def __init__(self, batches: list[list[dict[str, Any]]] | None = None) -> None:
        self._batches = batches or []
        self._idx = 0
        self.get_requests: list[dict[str, Any]] = []
        self.sent: list[dict[str, Any]] = []
        self.fail_get = False

    async def request(
        self,
        method: str,
        url: str,
        *,
        json_body: dict[str, Any] | None = None,
        timeout: float = 30.0,
    ) -> HttpResponse:
        if "getUpdates" in url:
            self.get_requests.append(json_body or {})
            if self.fail_get:
                return HttpResponse(200, {"ok": False})
            if self._idx < len(self._batches):
                batch = self._batches[self._idx]
                self._idx += 1
                return HttpResponse(200, {"ok": True, "result": batch})
            return HttpResponse(200, {"ok": True, "result": []})
        if "sendMessage" in url:
            self.sent.append(json_body or {})
            return HttpResponse(200, {"ok": True, "result": {"message_id": 1}})
        return HttpResponse(200, {"ok": True})


@pytest.fixture
def services() -> Any:
    """完整最小栈。"""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db = Database(path)
    registry = ServiceRegistry()
    registry.register(Database, db, owner="test")
    registry.register(SessionService, SessionServiceImpl(db), owner="test")
    registry.register(
        ContextService,
        ContextServiceImpl(services=registry),
        owner="test",
    )
    registry.register(HookManager, HookManager(), owner="test")
    registry.register(ToolRegistry, ToolRegistry(), owner="test")
    pr = ProviderRegistry()
    pr.register_provider(
        "fake",
        FakeProvider,  # type: ignore[arg-type]
        {"api_key": "k", "name": "fake", "enabled": True},
    )
    registry.register(ProviderRegistry, pr, owner="test")
    yield registry
    db.close()
    os.unlink(path)


class TestTelegramPolling:
    """长轮询循环测试。"""

    @pytest.mark.asyncio
    async def test_poll_delivers_inbound(self) -> None:
        """一批更新被解析并交给处理函数。"""
        tr = FakeTransport(batches=[[_update(1, 123, "hello")]])
        received: list[InboundMessage] = []

        async def handler(msg: InboundMessage) -> None:
            received.append(msg)

        channel = TelegramChannel("token", transport=tr, poll_interval=0.01, inbound_handler=handler)
        await channel.start()
        await asyncio.sleep(0.1)
        await channel.stop()

        assert len(received) == 1
        assert received[0].user_id == "123"
        assert received[0].text == "hello"
        assert received[0].reply_to == "123"

    @pytest.mark.asyncio
    async def test_offset_advances(self) -> None:
        """处理过 update 后，下一轮携带 offset。"""
        tr = FakeTransport(batches=[[_update(100, 1, "a")]])
        channel = TelegramChannel(
            "token",
            transport=tr,
            poll_interval=0.01,
            inbound_handler=lambda m: _noop(),
        )
        await channel.start()
        await asyncio.sleep(0.1)
        await channel.stop()

        assert tr.get_requests
        assert any(req.get("offset") == 101 for req in tr.get_requests)

    @pytest.mark.asyncio
    async def test_nontext_ignored(self) -> None:
        """非文本消息被忽略。"""
        tr = FakeTransport(batches=[[_update(1, 123, None)]])
        received: list[InboundMessage] = []

        async def handler(msg: InboundMessage) -> None:
            received.append(msg)

        channel = TelegramChannel("token", transport=tr, poll_interval=0.01, inbound_handler=handler)
        await channel.start()
        await asyncio.sleep(0.1)
        await channel.stop()
        assert received == []

    @pytest.mark.asyncio
    async def test_allowed_users_filter(self) -> None:
        """不在允许名单的用户被忽略。"""
        tr = FakeTransport(batches=[[_update(1, 999, "hi"), _update(2, 888, "hi")]])
        received: list[InboundMessage] = []

        async def handler(msg: InboundMessage) -> None:
            received.append(msg)

        channel = TelegramChannel(
            "token",
            allowed_users=["888"],
            transport=tr,
            poll_interval=0.01,
            inbound_handler=handler,
        )
        await channel.start()
        await asyncio.sleep(0.1)
        await channel.stop()
        assert len(received) == 1
        assert received[0].user_id == "888"

    @pytest.mark.asyncio
    async def test_poll_failure_survives(self) -> None:
        """getUpdates 持续失败时轮询循环不崩溃。"""
        tr = FakeTransport()
        tr.fail_get = True
        channel = TelegramChannel(
            "token",
            transport=tr,
            poll_interval=0.01,
            inbound_handler=lambda m: _noop(),
        )
        await channel.start()
        await asyncio.sleep(0.1)
        assert channel.running is True
        await channel.stop()

    @pytest.mark.asyncio
    async def test_start_without_token(self) -> None:
        """无 token 不启动。"""
        channel = TelegramChannel("", transport=FakeTransport())
        await channel.start()
        assert channel.running is False
        assert channel._task is None  # noqa: SLF001


class TestTelegramSend:
    """回发测试。"""

    @pytest.mark.asyncio
    async def test_send_message(self) -> None:
        """send 调用 sendMessage。"""
        tr = FakeTransport()
        channel = TelegramChannel("token", transport=tr)
        await channel.send("chat9", "你好")
        assert tr.sent == [{"chat_id": "chat9", "text": "你好"}]

    @pytest.mark.asyncio
    async def test_long_text_truncated(self) -> None:
        """超长文本截断到 4096 以内。"""
        tr = FakeTransport()
        channel = TelegramChannel("token", transport=tr)
        await channel.send("chat9", "x" * 5000)
        assert len(tr.sent[0]["text"]) == 4096


class TestTelegramEndToEnd:
    """与 ChannelManager 端到端。"""

    @pytest.mark.asyncio
    async def test_end_to_end(self, services: ServiceRegistry) -> None:
        """Telegram 消息经 manager 跑出终答并回发。"""
        mgr = ChannelManager(
            services,
            config=ChannelManagerConfig(provider_id="fake", model="m"),
        )
        tr = FakeTransport(batches=[[_update(1, 123, "你好")]])
        channel = TelegramChannel(
            "token",
            transport=tr,
            poll_interval=0.01,
            inbound_handler=mgr.process,
        )
        mgr.register(channel)
        await channel.start()
        await asyncio.sleep(0.1)
        await channel.stop()

        assert tr.sent
        assert tr.sent[0]["chat_id"] == "123"
        assert tr.sent[0]["text"] == "回复:你好"

        session_service = services.get(SessionService)
        sessions = session_service.list_sessions()
        assert any(s.title == "telegram · 123" for s in sessions)


async def _noop() -> None:
    """空处理函数。"""
    return None
