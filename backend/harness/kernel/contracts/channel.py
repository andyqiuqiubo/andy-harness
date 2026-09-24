"""ChannelPlugin 契约 —— 外部接入渠道。"""

from __future__ import annotations

from harness.kernel.contracts.base import BasePlugin


class ChannelPlugin(BasePlugin):
    """外部接入渠道插件契约（CLI / 飞书 / Telegram 等）。"""
