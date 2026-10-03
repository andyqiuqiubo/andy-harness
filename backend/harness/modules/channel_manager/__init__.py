"""channel_manager 模块 —— 多渠道接入（Webhook / Telegram 等）。"""

from harness.modules.channel_manager.service import (
    Channel,
    ChannelError,
    ChannelManager,
    ChannelManagerConfig,
    InboundMessage,
    WebhookChannel,
)
from harness.modules.channel_manager.telegram import (
    HttpTransport,
    TelegramChannel,
    UrllibTransport,
)

__all__ = [
    "Channel",
    "ChannelError",
    "ChannelManager",
    "ChannelManagerConfig",
    "HttpTransport",
    "InboundMessage",
    "TelegramChannel",
    "UrllibTransport",
    "WebhookChannel",
]
