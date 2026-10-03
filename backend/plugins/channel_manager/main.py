"""channel_manager 插件 —— 多渠道接入服务。

激活时创建 ChannelManager 并注册内置 Webhook 渠道（同步应答）；
Telegram 等其它渠道由各自插件在激活时通过 `ChannelManager.register` 挂入。

环境变量：
- HARNESS_CHANNEL_PROVIDER / HARNESS_CHANNEL_MODEL：渠道默认使用的 provider / 模型
  （未配置时自动取第一个启用的 provider）
- HARNESS_CHANNEL_SYSTEM_PROMPT：渠道会话系统提示词
- HARNESS_WEBHOOK_NAME（默认 webhook）/ HARNESS_WEBHOOK_SECRET
- HARNESS_WEBHOOK_ALLOWED_USERS：逗号分隔的用户名单（为空则不限制）
"""

from __future__ import annotations

import logging
import os

from harness.engine.tool_registry import ToolRegistry
from harness.kernel.context import PluginContext
from harness.kernel.contracts.base import BasePlugin, PluginManifest
from harness.kernel.hooks import HookManager
from harness.modules.channel_manager.service import (
    ChannelManager,
    ChannelManagerConfig,
    WebhookChannel,
)

logger = logging.getLogger("harness.plugin.channel_manager")


def _csv_env(name: str) -> list[str]:
    raw = os.environ.get(name, "")
    return [item.strip() for item in raw.split(",") if item.strip()]


def _channel_enabled() -> bool:
    return os.environ.get("HARNESS_CHANNELS", "1") not in ("0", "false", "False")


class ChannelManagerPlugin(BasePlugin):
    """多渠道管理插件。"""

    manifest: PluginManifest
    _manager: ChannelManager | None = None

    def __init__(self) -> None:
        self._manager = None

    async def activate(self, ctx: PluginContext) -> None:
        """激活：创建管理器、注册 Webhook 渠道并启动。"""
        try:
            hooks = ctx.services.get(HookManager)
        except Exception:  # noqa: BLE001
            hooks = None
        try:
            tool_registry = ctx.services.get(ToolRegistry)
        except Exception:  # noqa: BLE001
            tool_registry = None

        config = ChannelManagerConfig(
            provider_id=os.environ.get("HARNESS_CHANNEL_PROVIDER", ""),
            model=os.environ.get("HARNESS_CHANNEL_MODEL", ""),
            system_prompt=os.environ.get("HARNESS_CHANNEL_SYSTEM_PROMPT", ""),
        )
        manager = ChannelManager(
            ctx.services,
            hooks=hooks,
            tool_registry=tool_registry,
            config=config,
        )

        # 内置 Webhook 渠道（同步应答）
        wh_name = os.environ.get("HARNESS_WEBHOOK_NAME", "webhook")
        wh_secret = os.environ.get("HARNESS_WEBHOOK_SECRET", "")
        manager.register(
            WebhookChannel(
                name=wh_name,
                secret=wh_secret,
                allowed_users=_csv_env("HARNESS_WEBHOOK_ALLOWED_USERS"),
            )
        )

        ctx.services.register(ChannelManager, manager, owner=self.plugin_id)
        self._manager = manager

        if _channel_enabled():
            await manager.start_all()
            ctx.logger.info("channel_manager 已激活（Webhook 渠道：%s）", wh_name)
        else:
            ctx.logger.info("channel_manager 已激活（渠道总开关已关闭）")

    async def deactivate(self, ctx: PluginContext) -> None:
        """停用：停止全部渠道。"""
        if self._manager is not None:
            await self._manager.stop_all()
            self._manager = None
        ctx.logger.info("channel_manager 已停用")
