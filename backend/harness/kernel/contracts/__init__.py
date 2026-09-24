"""契约接口包 —— 所有跨模块协作的抽象接口定义。"""

from __future__ import annotations

from harness.kernel.contracts.base import BasePlugin, PluginManifest, PluginType
from harness.kernel.contracts.channel import ChannelPlugin
from harness.kernel.contracts.hook import HookContext, HookHandler, HookPlugin, HookResult
from harness.kernel.contracts.provider import ModelProviderPlugin
from harness.kernel.contracts.service import ServicePlugin
from harness.kernel.contracts.token_counter import TokenCounter
from harness.kernel.contracts.tool import ToolPlugin

__all__ = [
    "BasePlugin",
    "ChannelPlugin",
    "HookContext",
    "HookHandler",
    "HookPlugin",
    "HookResult",
    "ModelProviderPlugin",
    "PluginManifest",
    "PluginType",
    "ServicePlugin",
    "TokenCounter",
    "ToolPlugin",
]
