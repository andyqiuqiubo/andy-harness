"""智谱（BigModel）Provider 插件。

同一把 Key 覆盖三类能力（官方 `/paas/v4` 为 OpenAI 兼容协议）：
- 文本对话：GLM 系列（免费档 glm-4.7-flash）
- 文生图  ：CogView-3-Flash（官方标注**免费模型**）
- 文生/图生视频：CogVideoX-Flash（官方标注**免费模型**，异步任务）

注意：图像/视频模型**不走** chat 接口，由 `modules/media_gen` 服务承担
（会话页选中生成类模型时前端调用 `/api/media/*`）。本插件负责：
① 把 `zhipu` 注册进 ProviderRegistry（模型下拉里可选）；
② 把已配置的 API Key 注入 MediaGenService。
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
from harness.modules.media_gen.service import load_zhipu_api_key

logger = logging.getLogger("harness.provider.zhipu")

# 生成类模型（前端据此判定走 media 接口而非 chat）
MEDIA_MODELS = {"cogview-3-flash", "cogvideox-flash"}


class ZhipuProvider(OpenAICompatibleProvider):
    """智谱 provider（OpenAI 兼容端点）。"""

    base_url = "https://open.bigmodel.cn/api/paas/v4"
    default_models = ["glm-4.7-flash", "cogview-3-flash", "cogvideox-flash"]
    provider_name = "zhipu"


class ZhipuProviderPlugin(BasePlugin):
    """智谱 provider 插件。"""

    manifest: PluginManifest
    _ctx: PluginContext | None = None

    def __init__(self) -> None:
        self._ctx = None
        self._provider_registry: ProviderRegistry | None = None

    async def activate(self, ctx: PluginContext) -> None:
        self._ctx = ctx

        if not ctx.services.has(ProviderRegistry):
            ctx.services.register(ProviderRegistry, ProviderRegistry(), owner=self.plugin_id)
        self._provider_registry = ctx.services.get(ProviderRegistry)

        api_key = ctx.config.get("api_key", "") or load_zhipu_api_key()
        base_url = ctx.config.get("base_url", ZhipuProvider.base_url)

        config: dict[str, Any] = {
            "name": "智谱",
            "api_key": api_key,
            "base_url": base_url,
            "models": list(ZhipuProvider.default_models),
        }
        self._provider_registry.register_provider("zhipu", ZhipuProvider, config)

        # 把 key 注入媒体生成服务（图像/视频走 media_gen，非 chat）
        try:
            from harness.api.rest.media import get_media_service

            svc = get_media_service()
            if api_key:
                svc.set_api_key(api_key)
        except Exception as e:  # noqa: BLE001
            logger.warning("MediaGenService 注入 Key 失败（key.txt 兜底仍可用）: %s", e)

        if not ctx.services.has(TokenCounter):
            from harness.modules.model_manager.token_counter_adapter import (
                ProviderTokenCounter,
            )

            ctx.services.register(
                TokenCounter,
                ProviderTokenCounter(ctx.services),
                owner=self.plugin_id,
            )
        ctx.logger.info("智谱 provider 已激活（对话 + 生图 + 生视频）")

    async def deactivate(self, ctx: PluginContext) -> None:
        if self._provider_registry:
            self._provider_registry.unregister_provider("zhipu")
        ctx.logger.info("智谱 provider 已停用")

    def count(self, text: str, model: str) -> int:
        return OpenAICompatibleProvider.estimate_tokens(text)

    async def chat(
        self,
        messages: list[dict[str, str]],
        model: str,
        stream: bool = True,
        **kwargs: Any,
    ) -> Any:
        """文本对话。**生成类模型不支持 chat**，给出明确错误而非静默失败。"""
        if model in MEDIA_MODELS:
            raise ProviderError(
                f"{model} 是图像/视频生成模型，不支持对话；"
                f"请在会话中选择 GLM 文本模型，或使用 /api/media/generations 生成",
                status_code=400,
            )
        if not self._provider_registry:
            raise ProviderError("Provider 未激活")
        provider = self._provider_registry.get_provider("zhipu")
        return provider.chat(messages, model, stream, **kwargs)

    async def list_models(self) -> list[str]:
        return list(ZhipuProvider.default_models)

    def count_tokens(self, text: str, model: str) -> int:
        return self.count(text, model)

    async def health_check(self) -> bool:
        if not self._provider_registry:
            return False
        try:
            provider = self._provider_registry.get_provider("zhipu")
            return await provider.health_check()
        except Exception as e:  # noqa: BLE001
            logger.warning("智谱健康检查失败: %s", e)
            return False
