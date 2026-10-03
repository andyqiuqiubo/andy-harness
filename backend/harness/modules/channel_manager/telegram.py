"""TelegramChannel —— Telegram 长轮询渠道。

通过 Telegram Bot API 的 getUpdates 长轮询收取消息，统一交给
ChannelManager 跑 AgentLoop，再用 sendMessage 回发。

HTTP 传输层可注入（测试用替身），默认实现基于标准库 urllib（无新增依赖）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.error
import urllib.request
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from harness.modules.channel_manager.service import (
    Channel,
    InboundMessage,
)

logger = logging.getLogger("harness.channel.telegram")

# 长轮询超时（秒）：Telegram 建议 25-50
_LONG_POLL_TIMEOUT = 25
# 出错后的退避间隔（秒），避免无 token / 网络错误时热循环
_ERROR_BACKOFF = 5
_TELEGRAM_BASE = "https://api.telegram.org"


@dataclass
class HttpResponse:
    """HTTP 响应（传输层统一返回）。"""

    status: int
    body: dict[str, Any]


class HttpTransport:
    """HTTP 传输层（可替换为测试替身）。"""

    async def request(
        self,
        method: str,
        url: str,
        *,
        json_body: dict[str, Any] | None = None,
        timeout: float = 30.0,
    ) -> HttpResponse:
        raise NotImplementedError


class UrllibTransport(HttpTransport):
    """默认 HTTP 传输：urllib 放在线程池中执行。"""

    async def request(
        self,
        method: str,
        url: str,
        *,
        json_body: dict[str, Any] | None = None,
        timeout: float = 30.0,
    ) -> HttpResponse:
        return await asyncio.to_thread(self._do_request, method, url, json_body, timeout)

    def _do_request(
        self,
        method: str,
        url: str,
        json_body: dict[str, Any] | None,
        timeout: float,
    ) -> HttpResponse:
        data = json.dumps(json_body).encode("utf-8") if json_body else None
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8")
                return HttpResponse(
                    status=resp.status,
                    body=json.loads(raw) if raw else {},
                )
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", errors="replace")
            try:
                body: dict[str, Any] = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                body = {"description": raw}
            return HttpResponse(status=e.code, body=body)


class TelegramChannel(Channel):
    """Telegram 长轮询渠道。"""

    def __init__(
        self,
        token: str,
        *,
        name: str = "telegram",
        allowed_users: list[str] | None = None,
        poll_interval: float = 1.0,
        transport: HttpTransport | None = None,
        inbound_handler: Callable[[InboundMessage], Awaitable[Any]] | None = None,
    ) -> None:
        self.name = name
        self.token = token
        self.allowed_users = [str(u) for u in (allowed_users or [])]
        self.poll_interval = max(0.1, float(poll_interval))
        self.transport = transport or UrllibTransport()
        self._handler = inbound_handler
        self._task: asyncio.Task[None] | None = None
        self._offset: int | None = None
        self._running = False
        self._processed = 0

    # ── 生命周期 ──────────────────────────────────────

    @property
    def running(self) -> bool:
        return self._running

    def set_handler(self, handler: Callable[[InboundMessage], Awaitable[Any]]) -> None:
        """注入入站处理函数（通常是 ChannelManager.process）。"""
        self._handler = handler

    async def start(self) -> None:
        if not self.token:
            logger.warning("Telegram 渠道启动失败：未配置 bot token")
            return
        self._running = True
        self._task = asyncio.create_task(self._poll_loop())
        logger.info("Telegram 渠道已启动（长轮询）")

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None
        logger.info("Telegram 渠道已停止")

    # ── 收发 ──────────────────────────────────────────

    def _api_url(self, method: str) -> str:
        return f"{_TELEGRAM_BASE}/bot{self.token}/{method}"

    async def send(self, target: str, text: str) -> None:
        # Telegram 单条消息上限 4096 字符，超出截断
        payload = text
        if len(payload) > 4096:
            payload = payload[:4095] + "…"
        resp = await self.transport.request(
            "POST",
            self._api_url("sendMessage"),
            json_body={"chat_id": target, "text": payload},
            timeout=15.0,
        )
        if resp.status != 200 or not resp.body.get("ok"):
            logger.warning(
                "Telegram sendMessage 失败: status=%s body=%s",
                resp.status,
                resp.body,
            )

    async def get_updates(self) -> list[dict[str, Any]]:
        """拉取一批更新（成功返回列表，失败抛 TelegramError）。"""
        body: dict[str, Any] = {
            "timeout": _LONG_POLL_TIMEOUT,
            "allowed_updates": ["message"],
        }
        if self._offset is not None:
            body["offset"] = self._offset
        resp = await self.transport.request(
            "POST",
            self._api_url("getUpdates"),
            json_body=body,
            timeout=_LONG_POLL_TIMEOUT + 10.0,
        )
        if resp.status != 200 or not resp.body.get("ok"):
            raise TelegramError(f"getUpdates 失败: status={resp.status} body={resp.body}")
        result = resp.body.get("result")
        return result if isinstance(result, list) else []

    # ── 轮询循环 ──────────────────────────────────────

    async def _poll_loop(self) -> None:
        while self._running:
            try:
                updates = await self.get_updates()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                logger.warning("Telegram 轮询出错（%ss 后重试）: %s", _ERROR_BACKOFF, e)
                await asyncio.sleep(_ERROR_BACKOFF)
                continue

            for update in updates:
                update_id = update.get("update_id")
                if isinstance(update_id, int):
                    # 确认位：下一轮从该 update 之后开始
                    self._offset = update_id + 1
                try:
                    await self._handle_update(update)
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # noqa: BLE001
                    logger.warning("处理 Telegram 更新失败（已忽略）: %s", e)
            if not updates:
                await asyncio.sleep(self.poll_interval)

    async def _handle_update(self, update: dict[str, Any]) -> None:
        message = update.get("message") or {}
        text = message.get("text")
        if not text:
            return  # 非文本（贴纸 / 图片）暂不处理
        from_user = message.get("from") or {}
        user_id = str(from_user.get("id", ""))
        chat = message.get("chat") or {}
        chat_id = str(chat.get("id", user_id))
        if not user_id:
            return

        if self.allowed_users and user_id not in self.allowed_users:
            logger.info("Telegram 用户不在允许名单，已忽略: user=%s", user_id)
            return

        if self._handler is None:
            logger.warning("Telegram 渠道未绑定处理函数，忽略消息")
            return

        inbound = InboundMessage(
            channel=self.name,
            user_id=user_id,
            text=text,
            reply_to=chat_id,
            metadata={"update_id": update.get("update_id"), "username": from_user.get("username", "")},
        )
        self._processed += 1
        await self._handler(inbound)

    def status(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "running": self.running,
            "allowed_users": self.allowed_users,
            "processed": self._processed,
        }


class TelegramError(Exception):
    """Telegram API 错误。"""
