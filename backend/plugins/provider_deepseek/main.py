"""DeepSeek Provider 插件。

deepseek-v4-flash / deepseek-v4-pro（思维链字段单独处理）。
兼容旧模型名 deepseek-chat / deepseek-reasoner。
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

logger = logging.getLogger("harness.provider.deepseek")


class DeepSeekProvider(OpenAICompatibleProvider):
    """DeepSeek provider 实现。"""

    base_url = "https://api.deepseek.com"
    default_models = [
        "deepseek-v4-flash",
        "deepseek-v4-pro",
        "deepseek-chat",
        "deepseek-reasoner",
    ]
    provider_name = "deepseek"

    def _parse_stream_chunk(self, data: dict[str, Any]) -> dict[str, Any] | None:
        """解析 DeepSeek 特有的 reasoning_content 字段。"""
        choices = data.get("choices", [])
        if not choices:
            return None

        delta = choices[0].get("delta", {})
        result: dict[str, Any] = {}

        if "content" in delta and delta["content"]:
            result["delta"] = delta["content"]
        if "tool_calls" in delta:
            result["tool_calls"] = delta["tool_calls"]
        # DeepSeek 特有：思维链内容
        if "reasoning_content" in delta and delta["reasoning_content"]:
            result["reasoning_content"] = delta["reasoning_content"]

        return result if result else None


class DeepSeekProviderPlugin(BasePlugin):
    """DeepSeek provider 插件。"""

    manifest: PluginManifest
    _ctx: PluginContext | None = None

    def __init__(self) -> None:
        self._ctx = None
        self._provider_registry: ProviderRegistry | None = None

    async def activate(self, ctx: PluginContext) -> None:
        """激活：注册 provider 到 ProviderRegistry。"""
        self._ctx = ctx

        # 获取或创建 ProviderRegistry
        if not ctx.services.has(ProviderRegistry):
            ctx.services.register(ProviderRegistry, ProviderRegistry(), owner=self.plugin_id)
        self._provider_registry = ctx.services.get(ProviderRegistry)

        # 从配置获取 API Key，回退到环境变量
        api_key = ctx.config.get("api_key", "") or __import__("os").getenv("DEEPSEEK_API_KEY", "")
        base_url = ctx.config.get("base_url", DeepSeekProvider.base_url)

        config: dict[str, Any] = {
            "name": "DeepSeek",
            "api_key": api_key,
            "base_url": base_url,
            "models": DeepSeekProvider.default_models,
        }

        self._provider_registry.register_provider(
            "deepseek", DeepSeekProvider, config
        )

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

        ctx.logger.info("DeepSeek provider 已激活")

    async def deactivate(self, ctx: PluginContext) -> None:
        """停用：注销 provider。"""
        if self._provider_registry:
            self._provider_registry.unregister_provider("deepseek")
        ctx.logger.info("DeepSeek provider 已停用")

    # TokenCounter 委托给 provider 实例
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
        """对话（委托给 provider 实例）。"""
        if not self._provider_registry:
            raise ProviderError("Provider 未激活")
        provider = self._provider_registry.get_provider("deepseek")
        return provider.chat(messages, model, stream, **kwargs)

    async def list_models(self) -> list[str]:
        """返回可用模型列表。"""
        return list(DeepSeekProvider.default_models)

    def count_tokens(self, text: str, model: str) -> int:
        """计算 token 数。"""
        return self.count(text, model)

    async def health_check(self) -> bool:
        """健康检查。"""
        if not self._provider_registry:
            return False
        try:
            provider = self._provider_registry.get_provider("deepseek")
            return await provider.health_check()
        except Exception as e:
            logger.warning("DeepSeek 健康检查失败: %s", e)
            return False
