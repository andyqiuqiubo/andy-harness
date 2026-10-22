"""网关免费模型 Provider 插件 —— 把 Cline / OpenCode / WorkBuddy 三家免费模型接入会话管线。

设计（进程内直连，非 HTTP 回环）：
- 三家各注册为一个普通 provider（id 见 `contracts.CHANNEL_PROVIDER_IDS`），
  于是会话页「厂商选择 → 模型列表 → 选定会话」全部复用既有 provider 管线，
  设置页的启用/停用语义也天然生效；
- provider.chat() **不走 HTTP**，直接调用进程内的 `GatewayService.chat()`，
  复用其路由 / 熔断 / 故障转移 / 用量记账 / 模型级可用性；
- 模型可用性 = 网关 `HealthTracker` 模型级冷却 ∧ 通道清单探测结果。provider 与
  网关管理台看到同一份状态，不存在「界面显示可用、一调用就挂」的错位。

模型 id 约定：`<channel>/<model>`（如 `workbuddy/hy3`），chat() 原样放进
`model` 字段即可被网关路由（`Router.expand` 支持带前缀名）。
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from harness.kernel.context import PluginContext
from harness.kernel.contracts.base import BasePlugin, PluginManifest
from harness.modules.gateway.contracts import (
    CHANNEL_PROVIDER_IDS,
    CHANNEL_PROVIDER_LABELS,
    GatewayError,
)
from harness.modules.model_manager.openai_compatible import (
    OpenAICompatibleProvider,
    ProviderError,
)
from harness.modules.model_manager.provider_registry import ProviderRegistry

logger = logging.getLogger("harness.provider.gateway")


class GatewayProvider(OpenAICompatibleProvider):
    """进程内 provider：把会话请求转交给 GatewayService.chat()。

    继承 OpenAICompatibleProvider 以复用实例化签名、TokenCounter 与注册形态
    （ProviderRegistry 期望 `type[OpenAICompatibleProvider]`）；但 `chat()` 完全
    覆写——不走 HTTP，改为进程内调网关。services/channel 经 extra_params 注入。
    """

    base_url = "in-process://gateway"
    default_models: list[str] = []
    provider_name = "gateway"
    supports_reasoning_content = True

    def __init__(
        self,
        api_key: str = "",
        base_url: str | None = None,
        models: list[str] | None = None,
        extra_params: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            api_key=api_key or "gateway-internal",
            base_url=base_url,
            models=models,
            extra_params=extra_params,
        )
        # services/channel 经 extra_params 注入（注册时由本插件写入 config）
        self._services = (extra_params or {}).get("_gateway_services")
        self._channel = str((extra_params or {}).get("_gateway_channel") or "")

    def _service(self) -> Any:
        if self._services is None:
            raise ProviderError("网关 provider 未正确装配（services 缺席）", status_code=503)
        from harness.modules.gateway.service import GatewayService

        try:
            return self._services.get(GatewayService)
        except Exception as e:  # noqa: BLE001
            raise ProviderError(f"网关服务不可用: {e}", status_code=503) from e

    async def chat(
        self,
        messages: list[dict[str, Any]],
        model: str,
        stream: bool = True,
        **kwargs: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        """流式对话：进程内调网关，把网关 SSE 重新解析为会话块协议。

        网关侧任何 GatewayError 都转成 ProviderError——AgentLoop 的重试圈会
        捕获所有异常，若不转换会让一次瞬时失败被外层再乘一遍重试次数。
        """
        svc = self._service()
        if not getattr(svc, "started", False):
            raise ProviderError("LLM 网关未启动（请检查 backend/data/gateway.json 配置）", status_code=503)

        ctx = svc.internal_context()
        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": True,  # 网关统一强制流式上游；块协议在这里还原
            **{
                k: v
                for k, v in kwargs.items()
                if not k.startswith("_") and k not in ("stream",) and v is not None
            },
        }
        try:
            result = await svc.chat(body, ctx, "")
        except GatewayError as e:
            raise ProviderError(str(e), status_code=e.http_status) from e

        if result.iterator is None:
            # 非流式载荷：拼成单块交给消费方
            payload = result.payload or {}
            choices = payload.get("choices") or [{}]
            msg = (choices[0] or {}).get("message", {}) or {}
            final_block: dict[str, Any] = {}
            if msg.get("content"):
                final_block["delta"] = msg["content"]
            if msg.get("reasoning_content"):
                final_block["reasoning_content"] = msg["reasoning_content"]
            if msg.get("tool_calls"):
                final_block["tool_calls"] = msg["tool_calls"]
            if payload.get("usage"):
                final_block["usage"] = payload["usage"]
            if final_block:
                yield final_block
            return

        async for raw in result.iterator:
            for line in raw.decode("utf-8", errors="replace").splitlines():
                if not line.startswith("data: "):
                    continue  # keepalive 注释 / 空行丢弃
                data_str = line[6:].strip()
                if data_str == "[DONE]":
                    return
                try:
                    data = json.loads(data_str)
                except json.JSONDecodeError:
                    continue
                if isinstance(data, dict) and data.get("error"):
                    # 流内错误（如 Qoder 排队/额度）：必须抛成 ProviderError，
                    # 否则 _parse_chunk 无 choices 会静默跳过 → 会话页无任何反馈。
                    err = data.get("error") or {}
                    msg = str(err.get("message") or "网关流式错误")
                    code = str(err.get("code") or "")
                    status = {"UPSTREAM_BUSY": 503, "CRED_RATE_LIMITED": 429}.get(code, 502)
                    raise ProviderError(msg, status_code=status)
                chunk_block = self._parse_chunk(data)
                if chunk_block:
                    yield chunk_block

    @staticmethod
    def _parse_chunk(data: dict[str, Any]) -> dict[str, Any] | None:
        """网关 SSE chunk（OpenAI 形状）→ 会话块协议。"""
        result: dict[str, Any] = {}
        for choice in data.get("choices") or []:
            delta = choice.get("delta") or {}
            if delta.get("content"):
                result["delta"] = (result.get("delta", "") or "") + delta["content"]
            if delta.get("reasoning_content"):
                result["reasoning_content"] = (result.get("reasoning_content", "") or "") + delta["reasoning_content"]
            if delta.get("tool_calls"):
                result["tool_calls"] = delta["tool_calls"]
        if data.get("usage"):
            result["usage"] = data["usage"]
        return result or None

    async def list_models(self) -> list[str]:
        return list(self.models)

    async def health_check(self) -> bool:
        try:
            svc = self._service()
        except ProviderError:
            return False
        return bool(getattr(svc, "started", False))


class GatewayProviderPlugin(BasePlugin):
    """把网关已启用的免费通道注册为会话可选的 provider。"""

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

        try:
            from harness.modules.gateway.service import GatewayService

            svc = ctx.services.get(GatewayService)
        except Exception as e:  # noqa: BLE001 —— 网关缺席不阻断其它插件
            ctx.logger.warning("网关服务不可用，跳过免费模型 provider 激活: %s", e)
            return

        if not getattr(svc, "started", False):
            ctx.logger.warning("网关未启动（配置损坏？），跳过免费模型 provider 激活")
            return

        registered = 0
        for channel, provider_id in CHANNEL_PROVIDER_IDS.items():
            models = self._models_of(svc, channel)
            if not models:
                continue
            self._provider_registry.register_provider(
                provider_id,
                GatewayProvider,
                {
                    "name": f"{CHANNEL_PROVIDER_LABELS.get(channel, channel)}（免费）",
                    "api_key": "gateway-internal",
                    "base_url": GatewayProvider.base_url,
                    "models": models,
                    "enabled": True,
                    "extra_params": {"_gateway_services": ctx.services, "_gateway_channel": channel},
                },
            )
            registered += 1
        ctx.logger.info("网关免费模型 provider 已激活：%d 家", registered)

    async def deactivate(self, ctx: PluginContext) -> None:
        if self._provider_registry:
            for provider_id in CHANNEL_PROVIDER_IDS.values():
                self._provider_registry.unregister_provider(provider_id)
        ctx.logger.info("网关免费模型 provider 已停用")

    @staticmethod
    def _models_of(svc: Any, channel: str) -> list[str]:
        """取通道当前配置的模型（带 `channel/` 前缀，供选择器展示与网关路由）。"""
        try:
            ch = svc.snapshot.channels.get(channel)
            if ch is None or not ch.enabled:
                return []
            return [f"{channel}/{m}" for m in ch.models]
        except Exception as e:  # noqa: BLE001
            logger.warning("读取网关通道 %s 模型失败: %s", channel, e)
            return []
