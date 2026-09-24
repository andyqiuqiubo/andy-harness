"""OpenAICompatibleProvider —— OpenAI 兼容端点的基类。

提供统一的流式解析、超时、指数退避重试、统一错误处理。
三家厂商（DeepSeek / Qwen / Doubao）均继承此基类。
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

import httpx

from harness.kernel.contracts.provider import ModelProviderPlugin
from harness.kernel.contracts.token_counter import TokenCounter

if TYPE_CHECKING:
    from harness.kernel.context import PluginContext

logger = logging.getLogger("harness.model_manager")

_tiktoken_encoding = None


def _get_tiktoken_encoding():
    """获取（缓存）tiktoken 编码器，避免每次调用都重新加载。"""
    global _tiktoken_encoding
    if _tiktoken_encoding is None:
        import tiktoken

        _tiktoken_encoding = tiktoken.encoding_for_model("gpt-4o")
    return _tiktoken_encoding


class ProviderError(Exception):
    """Provider 统一错误。"""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        self.status_code = status_code
        super().__init__(message)


class OpenAICompatibleProvider(TokenCounter, ModelProviderPlugin):
    """OpenAI 兼容端点的基类。

    子类需设置:
    - base_url: API 基础 URL
    - default_models: 默认模型列表
    - provider_name: provider 名称（用于日志）
    """

    base_url: str = ""
    default_models: list[str] = []
    provider_name: str = "openai-compatible"

    def __init__(
        self,
        api_key: str,
        base_url: str | None = None,
        models: list[str] | None = None,
        extra_params: dict[str, Any] | None = None,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url or self.base_url
        self.models = models or list(self.default_models)
        self.extra_params = extra_params or {}

    async def activate(self, ctx: PluginContext) -> None:
        """激活（no-op，由插件类负责实际注册）。"""
        pass

    async def deactivate(self, ctx: PluginContext) -> None:
        """停用（no-op，由插件类负责实际注销）。"""
        pass

    def _headers(self) -> dict[str, str]:
        """构建请求头。"""
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def _build_request_body(
        self,
        messages: list[dict[str, str]],
        model: str,
        stream: bool = True,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """构建请求体。"""
        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": stream,
        }
        # 合并额外参数：extra_params 是默认值，kwargs 优先覆盖
        body.update(self.extra_params)
        body.update(kwargs)
        return body

    async def chat(
        self,
        messages: list[dict[str, str]],
        model: str,
        stream: bool = True,
        **kwargs: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        """流式对话。

        Yields:
            流式响应块 {"delta": "...", "tool_calls": [...], "reasoning_content": "..."}
        """
        body = self._build_request_body(messages, model, stream, **kwargs)
        url = f"{self.base_url}/chat/completions"

        async for chunk in self._stream_with_retry(url, body):
            yield chunk

    async def _stream_with_retry(
        self,
        url: str,
        body: dict[str, Any],
        max_retries: int = 3,
        timeout: float = 60.0,
    ) -> AsyncIterator[dict[str, Any]]:
        """带指数退避重试的流式请求。"""
        last_error: Exception | None = None

        for attempt in range(max_retries):
            try:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    async with client.stream(
                        "POST", url, json=body, headers=self._headers()
                    ) as response:
                        if response.status_code != 200:
                            error_text = await response.aread()
                            error_msg = self._parse_error_response(
                                error_text.decode("utf-8"),
                                response.status_code,
                            )
                            if response.status_code == 401:
                                raise ProviderError(
                                    f"API Key 无效或已过期: {error_msg}",
                                    status_code=401,
                                )
                            if response.status_code == 429:
                                # 限流，可重试
                                raise ProviderError(
                                    f"请求被限流: {error_msg}",
                                    status_code=429,
                                )
                            raise ProviderError(
                                f"API 请求失败 ({response.status_code}): {error_msg}",
                                status_code=response.status_code,
                            )

                        async for line in response.aiter_lines():
                            if not line or not line.startswith("data: "):
                                continue
                            data_str = line[6:]  # 移除 "data: " 前缀
                            if data_str.strip() == "[DONE]":
                                return
                            try:
                                data = json.loads(data_str)
                                parsed = self._parse_stream_chunk(data)
                                if parsed:
                                    yield parsed
                            except json.JSONDecodeError:
                                logger.debug("跳过无法解析的 SSE 行: %s", data_str[:100])
                                continue

                return  # 成功完成，退出重试循环

            except ProviderError as e:
                if e.status_code == 401:
                    # 401 不可重试
                    raise
                last_error = e
                logger.warning(
                    "%s 请求失败 (attempt %d/%d): %s",
                    self.provider_name,
                    attempt + 1,
                    max_retries,
                    e,
                )
            except (httpx.TimeoutException, httpx.ConnectError) as e:
                last_error = e
                logger.warning(
                    "%s 网络错误 (attempt %d/%d): %s",
                    self.provider_name,
                    attempt + 1,
                    max_retries,
                    e,
                )

            # 指数退避
            if attempt < max_retries - 1:
                backoff = 2**attempt
                logger.info("%s 等待 %ds 后重试...", self.provider_name, backoff)
                await asyncio.sleep(backoff)

        raise ProviderError(
            f"{self.provider_name} 请求失败，已重试 {max_retries} 次: {last_error}"
        )

    def _parse_stream_chunk(self, data: dict[str, Any]) -> dict[str, Any] | None:
        """解析单个 SSE 流式数据块。

        子类可覆盖此方法以处理厂商特有字段（如 DeepSeek 的 reasoning_content）。
        """
        choices = data.get("choices", [])
        if not choices:
            return None

        delta = choices[0].get("delta", {})
        result: dict[str, Any] = {}

        if "content" in delta and delta["content"]:
            result["delta"] = delta["content"]
        if "tool_calls" in delta:
            result["tool_calls"] = delta["tool_calls"]
        if "reasoning_content" in delta and delta["reasoning_content"]:
            result["reasoning_content"] = delta["reasoning_content"]

        return result if result else None

    def _parse_error_response(self, text: str, status_code: int) -> str:
        """解析错误响应。"""
        try:
            data = json.loads(text)
            error = data.get("error", {})
            return str(error.get("message", text))
        except (json.JSONDecodeError, AttributeError):
            return text

    async def list_models(self) -> list[str]:
        """返回可用模型列表。"""
        return list(self.models)

    def count_tokens(self, text: str, model: str) -> int:
        """计算 token 数（使用 tiktoken 估算）。

        注意：tiktoken 是 OpenAI 的 tokenizer，对非 OpenAI 模型为估算值。
        """
        return self.estimate_tokens(text)

    @staticmethod
    def estimate_tokens(text: str) -> int:
        """估算 token 数（使用类级缓存的 tiktoken 编码器，避免重复加载）。"""
        try:
            encoding = _get_tiktoken_encoding()
            return len(encoding.encode(text))
        except Exception:
            # 回退到字符数估算
            return len(text) // 4

    async def health_check(self) -> bool:
        """健康检查（测试连接）。

        调用 /models 端点验证 API Key 和连通性，不消耗 token。
        """
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(
                    f"{self.base_url}/models",
                    headers=self._headers(),
                )
                return response.status_code == 200
        except Exception as e:
            logger.warning("%s 健康检查失败: %s", self.provider_name, e)
            return False
