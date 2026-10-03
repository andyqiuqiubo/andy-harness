"""channel_telegram 插件 —— 注册并启动 Telegram 渠道。

激活时从 ChannelManager（channel_manager 插件）取管理器，创建
TelegramChannel 并挂入；未配置 token 时渠道保持已注册但不启动。

环境变量：
- HARNESS_TELEGRAM_BOT_TOKEN：BotFather 申请的 token（必填才会真正启动）
- HARNESS_TELEGRAM_ALLOWED_USERS：逗号分隔的允许用户 ID（为空则不限制）
- HARNESS_TELEGRAM_POLL_INTERVAL：空批次后的轮询间隔（秒，默认 1）
"""

from __future__ import annotations

import logging
import os

from harness.kernel.context import PluginContext
from harness.kernel.contracts.base import BasePlugin, PluginManifest
from harness.modules.channel_manager.service import ChannelManager
from harness.modules.channel_manager.telegram import TelegramChannel

logger = logging.getLogger("harness.plugin.channel_telegram")


def _csv_env(name: str) -> list[str]:
    raw = os.environ.get(name, "")
    return [item.strip() for item in raw.split(",") if item.strip()]


class TelegramChannelPlugin(BasePlugin):
    """Telegram 渠道插件。"""

    manifest: PluginManifest
    _channel: TelegramChannel | None = None

    def __init__(self) -> None:
        self._channel = None

    async def activate(self, ctx: PluginContext) -> None:
        """激活：创建 Telegram 渠道并挂入管理器。"""
        try:
            manager = ctx.services.get(ChannelManager)
        except Exception:
            ctx.logger.warning("channel_telegram 激活失败：ChannelManager 尚未就绪")
            return

        token = os.environ.get("HARNESS_TELEGRAM_BOT_TOKEN", "")
        try:
            poll_interval = float(os.environ.get("HARNESS_TELEGRAM_POLL_INTERVAL", "1"))
        except ValueError:
            poll_interval = 1.0

        channel = TelegramChannel(
            token=token,
            allowed_users=_csv_env("HARNESS_TELEGRAM_ALLOWED_USERS"),
            poll_interval=poll_interval,
            inbound_handler=manager.process,
        )
        manager.register(channel)
        self._channel = channel
        await channel.start()

        if token:
            ctx.logger.info("Telegram 渠道已激活")
        else:
            ctx.logger.warning("Telegram 渠道已注册但未配置 HARNESS_TELEGRAM_BOT_TOKEN，未启动")

    async def deactivate(self, ctx: PluginContext) -> None:
        """停用：从管理器注销渠道。"""
        if self._channel is not None:
            try:
                manager = ctx.services.get(ChannelManager)
                await manager.unregister(self._channel.name)
            except Exception:  # noqa: BLE001
                await self._channel.stop()
            self._channel = None
        ctx.logger.info("Telegram 渠道已停用")
