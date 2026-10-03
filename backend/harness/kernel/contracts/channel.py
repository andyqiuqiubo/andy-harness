"""ChannelPlugin 契约 —— 外部接入渠道。

channel 类型插件（如 plugins/channel_telegram）负责创建运行时 Channel 并挂入
ChannelManager；会话映射与 AgentLoop 驱动由
`harness/modules/channel_manager` 统一实现。
"""

from __future__ import annotations

from harness.kernel.contracts.base import BasePlugin


class ChannelPlugin(BasePlugin):
    """外部接入渠道插件契约（飞书 / Telegram / Webhook 等）。

    运行时「收发」原语（start/stop/send）见
    `harness.modules.channel_manager.service.Channel`，
    本契约只作为 channel 类型插件的统一基类。
    """

    #: 渠道名（注册到 ChannelManager 的键；由子类赋值）
    channel_name: str = ""
