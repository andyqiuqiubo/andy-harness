"""P2#40 telegram allowed_users 为空时 fail-closed 拒绝入站。"""

from __future__ import annotations

import asyncio

from harness.modules.channel_manager.telegram import TelegramChannel


class _Spy:
    def __init__(self) -> None:
        self.calls: list[object] = []

    async def __call__(self, inbound: object) -> None:
        self.calls.append(inbound)


def _update(user_id: str, text: str = "hi") -> dict:
    return {"message": {"text": text, "from": {"id": user_id}, "chat": {"id": user_id}}}


def test_empty_allowlist_rejects_all() -> None:
    spy = _Spy()
    ch = TelegramChannel(token="x", allowed_users=[], inbound_handler=spy)
    asyncio.run(ch._handle_update(_update("123")))
    assert spy.calls == []


def test_allowed_user_passes() -> None:
    spy = _Spy()
    ch = TelegramChannel(token="x", allowed_users=["123"], inbound_handler=spy)
    asyncio.run(ch._handle_update(_update("123")))
    assert len(spy.calls) == 1


def test_disallowed_user_rejected() -> None:
    spy = _Spy()
    ch = TelegramChannel(token="x", allowed_users=["123"], inbound_handler=spy)
    asyncio.run(ch._handle_update(_update("999")))
    assert spy.calls == []
