"""Qwen Provider 插件。

qwen-max / qwen-plus / qwen-turbo 等。
"""

from __future__ import annotations

import logging
from typing import Any

from harness.kernel.context import PluginContext
from harness.kernel.contracts.base import BasePlugin, PluginManifest
from harness.kernel.contracts.token_counter import TokenCounter
from harness.modules.model_manager.openai_compatible import (
    OpenAICompatibleProvider,
    ProviderError,
)
from harness.modules.model_manager.provider_registry import ProviderRegistry

logger = logging.getLogger("harness.provider.qwen")


class QwenProvider(OpenAICompatibleProvider):
    """Qwen provider 实现。"""

    base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    default_models = ["qwen-max", "qwen-plus", "qwen-turbo", "qwen-long"]
    provider_name = "qwen"


class QwenProviderPlugin(BasePlugin):
    """Qwen provider 插件。"""

    manifest: PluginManifest
    _ctx: PluginContext | None = None

    def __init__(self) -> None:
        self._ctx = None
        self._provider_registry: ProviderRegistry | None = None

    async def activate(self, ctx: PluginContext) -> None:
        """激活：注册 provider 到 ProviderRegistry。"""
        self._ctx = ctx

        if not ctx.services.has(ProviderRegistry):
            ctx.services.register(ProviderRegistry, ProviderRegistry(), owner=self.plugin_id)
        self._provider_registry = ctx.services.get(ProviderRegistry)

        api_key = ctx.config.get("api_key", "")
        base_url = ctx.config.get("base_url", QwenProvider.base_url)

        config: dict[str, Any] = {
            "name": "Qwen",
            "api_key": api_key,
            "base_url": base_url,
            "models": QwenProvider.default_models,
        }

        self._provider_registry.register_provider("qwen", QwenProvider, config)
        # 注册组合式 TokenCounter（动态委托到正确的 provider）
        if not ctx.services.has(TokenCounter):
            from harness.modules.model_manager.token_counter_adapter import (
                ProviderTokenCounter,
            )

            ctx.services.register(
                TokenCounter,
                ProviderTokenCounter(ctx.services),
                owner=self.plugin_id,
            )
        ctx.logger.info("Qwen provider 已激活")

    async def deactivate(self, ctx: PluginContext) -> None:
        """停用。"""
        if self._provider_registry:
            self._provider_registry.unregister_provider("qwen")
        ctx.logger.info("Qwen provider 已停用")

    def count(self, text: str, model: str) -> int:
        """Token 计数（使用缓存的 tiktoken 编码器，避免创建临时实例）。"""
        return OpenAICompatibleProvider.estimate_tokens(text)

    async def chat(
        self,
        messages: list[dict[str, str]],
        model: str,
        stream: bool = True,
        **kwargs: Any,
    ) -> Any:
        """对话。"""
        if not self._provider_registry:
            raise ProviderError("Provider 未激活")
        provider = self._provider_registry.get_provider("qwen")
        return provider.chat(messages, model, stream, **kwargs)

    async def list_models(self) -> list[str]:
        """返回可用模型列表。"""
        return list(QwenProvider.default_models)

    def count_tokens(self, text: str, model: str) -> int:
        """计算 token 数。"""
        return self.count(text, model)

    async def health_check(self) -> bool:
        """健康检查。"""
        if not self._provider_registry:
            return False
        try:
            provider = self._provider_registry.get_provider("qwen")
            return await provider.health_check()
        except Exception as e:
            logger.warning("Qwen 健康检查失败: %s", e)
            return False
