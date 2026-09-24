"""ProviderTokenCounter —— 根据模型名称动态委托到正确的 provider。

所有 provider 插件注册同一个 ProviderTokenCounter 实例，
该实例通过 ProviderRegistry 查找拥有指定模型的 provider 并委托 count_tokens。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from harness.kernel.contracts.token_counter import TokenCounter

if TYPE_CHECKING:
    from harness.kernel.services import ServiceRegistry

logger = logging.getLogger("harness.model_manager")


class ProviderTokenCounter(TokenCounter):
    """组合式 TokenCounter，根据模型名查找对应 provider 进行 token 计数。

    解决多 provider 同时注册 TokenCounter 导致只有最后一个生效的问题。
    每个 provider 插件注册此适配器（覆盖前一个，但行为一致——动态查找）。
    """

    def __init__(self, services: ServiceRegistry) -> None:
        self._services = services

    def count_tokens(self, text: str, model: str) -> int:
        """计算 token 数。

        1. 通过 ProviderRegistry 查找拥有该模型的 provider
        2. 调用该 provider 的 count_tokens
        3. 找不到则回退到 tiktoken 估算
        """
        from harness.modules.model_manager.openai_compatible import (
            OpenAICompatibleProvider,
        )
        from harness.modules.model_manager.provider_registry import ProviderRegistry

        try:
            registry = self._services.get(ProviderRegistry)
        except Exception:
            registry = None

        if registry:
            for provider_info in registry.list_providers():
                models = provider_info.get("models", [])
                if model in models:
                    try:
                        provider = registry.get_provider(provider_info["id"])
                        return provider.count_tokens(text, model)
                    except Exception as e:
                        logger.debug(
                            "Provider %s 计算 token 失败，回退到估算: %s",
                            provider_info["id"],
                            e,
                        )
                        break

        # 回退到 tiktoken 估算
        return OpenAICompatibleProvider.estimate_tokens(text)
