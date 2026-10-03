"""REST API —— 多渠道接入路由。

- GET  /api/channels                     列出渠道及状态
- POST /api/channels/inbound/{name}      Webhook 同步入站（终答随响应返回）
- POST /api/channels/{name}/send         主动向渠道用户发消息（管理面）
"""

from __future__ import annotations

from typing import Any, cast

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from harness.api.errors import APIError
from harness.kernel.services import ServiceRegistry
from harness.modules.channel_manager.service import (
    ChannelError,
    ChannelManager,
    InboundMessage,
    WebhookChannel,
)

router = APIRouter(prefix="/api/channels", tags=["channels"])


class InboundPayload(BaseModel):
    """Webhook 入站载荷。"""

    user_id: str
    text: str
    reply_to: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class SendPayload(BaseModel):
    """主动发送载荷。"""

    target: str
    text: str


def _get_manager(registry: ServiceRegistry) -> ChannelManager:
    """获取 ChannelManager。"""
    try:
        return cast("ChannelManager", registry.get(ChannelManager))
    except Exception:
        raise APIError(
            "CHANNEL_SERVICE_UNAVAILABLE",
            "渠道服务不可用",
            503,
        )


def _map_channel_error(exc: ChannelError) -> APIError:
    """把渠道错误码映射为 HTTP 状态。"""
    status = 400
    if exc.code == "CHANNEL_NOT_FOUND":
        status = 404
    elif exc.code in ("USER_NOT_ALLOWED", "INVALID_SECRET"):
        status = 403
    elif exc.code == "NO_PROVIDER":
        status = 503
    return APIError(exc.code, exc.message, status)


def setup_channel_routes(registry: ServiceRegistry) -> None:
    """注册渠道路由。

    模块级 router 会被多个测试应用复用：先清理此前注册的闭包，避免
    旧应用（不同 ServiceRegistry）的路由优先匹配导致 503。
    """
    router.routes.clear()

    @router.get("", summary="列出渠道")
    async def list_channels() -> list[dict[str, Any]]:
        manager = _get_manager(registry)
        return manager.list_channels()

    @router.post("/inbound/{name}", summary="Webhook 入站")
    async def inbound(
        name: str,
        payload: InboundPayload,
        request: Request,
        secret: str = "",
    ) -> dict[str, Any]:
        manager = _get_manager(registry)
        channel = manager.get(name)
        if not isinstance(channel, WebhookChannel):
            raise APIError(
                "CHANNEL_NOT_FOUND",
                f"Webhook 渠道不存在: {name}",
                404,
            )

        header_secret = request.headers.get("X-Channel-Secret", "")
        if not channel.check_secret(secret or header_secret):
            raise APIError("INVALID_SECRET", "渠道密钥无效", 403)

        msg = InboundMessage(
            channel=name,
            user_id=payload.user_id,
            text=payload.text,
            reply_to=payload.reply_to,
            metadata=payload.metadata,
        )
        try:
            result = await manager.process(msg)
        except ChannelError as e:
            raise _map_channel_error(e)
        return {
            "reply": result.reply,
            "session_id": result.session_id,
            "error": result.error,
        }

    @router.post("/{name}/send", summary="主动发送消息")
    async def send(name: str, payload: SendPayload) -> dict[str, str]:
        manager = _get_manager(registry)
        channel = manager.get(name)
        if channel is None:
            raise APIError("CHANNEL_NOT_FOUND", f"渠道不存在: {name}", 404)
        try:
            await channel.send(payload.target, payload.text)
        except ChannelError as e:
            raise _map_channel_error(e)
        return {"status": "sent"}

    @router.get("/{name}", summary="渠道详情")
    async def channel_detail(name: str) -> dict[str, Any]:
        manager = _get_manager(registry)
        channel = manager.get(name)
        if channel is None:
            raise APIError("CHANNEL_NOT_FOUND", f"渠道不存在: {name}", 404)
        return channel.status()
