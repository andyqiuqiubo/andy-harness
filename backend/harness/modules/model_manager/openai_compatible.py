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


def _get_tiktoken_encoding() -> Any:
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


class _ResumeCursor:
    """流式重试的**增量游标**（SEC-17）。

    问题：流中途失败（读超时、连接被重置）后 :meth:`_stream_with_retry` 会整条
    请求重发。原实现没有任何游标，已推给前端的正文/思维链被**再次全量发送**——
    用户看到重复文本，且实时展示与落库记录错位（重复内容不落库）。DeepSeek /
    OpenRouter 首 token 常达 30–120s，该重试路径是常态而非边缘。

    方案：记住已送达调用方的内容，重试时先**校验**新内容的前缀与已产出一致，
    再跳过等长前缀只续传增量；一旦检测到内容漂移（重试生成了不同文本）就放弃
    裁剪、整体重发——宁可重复也不丢字。
    """

    # 单个游标最多记住的字符数；超出后退化为「整体重发」（不裁剪），避免无界内存
    MAX_PREFIX = 256 * 1024

    def __init__(self) -> None:
        self._prefix = ""
        self._matched = 0
        self._skipping = False
        # 已产出内容超过上限：无法再校验前缀，本游标作废
        self._overflow = False

    def begin_attempt(self) -> None:
        """开始新一轮请求：重置匹配进度，准备跳过已产出前缀。"""
        self._matched = 0
        self._skipping = not self._overflow and bool(self._prefix)

    def filter(self, text: str) -> str:
        """裁剪本轮重试的增量，返回**应产出给调用方**的部分。"""
        if not self._skipping or not text:
            return text
        n = len(self._prefix)
        i = 0
        while i < len(text) and self._matched < n:
            if text[i] != self._prefix[self._matched]:
                logger.warning(
                    "流式重试内容与上次产出不一致（偏移 %d），放弃增量续传、整体重发",
                    self._matched,
                )
                self._skipping = False
                return text[i:]
            self._matched += 1
            i += 1
        if self._matched >= n:
            self._skipping = False
        return text[i:]

    def record(self, text: str) -> None:
        """记账：``text`` 已确认送达调用方。"""
        if not text:
            return
        if self._overflow:
            return
        if len(self._prefix) + len(text) > self.MAX_PREFIX:
            self._overflow = True
            self._prefix = ""
            return
        self._prefix += text

    @property
    def pending(self) -> int:
        """当前仍待跳过的字符数（仅用于测试与日志）。"""
        return 0 if self._overflow else len(self._prefix)


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
    # 是否支持在 assistant 消息中回传 reasoning_content（DeepSeek 推理模型要求）。
    # 子类（如 DeepSeekProvider）覆盖为 True；其余 OpenAI 兼容端点默认剥离，避免 400。
    supports_reasoning_content: bool = False

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
        if stream:
            # 流式响应在最后一个块携带 usage（token 用量统计）
            body["stream_options"] = {"include_usage": True}
        # 合并额外参数：extra_params 是默认值，kwargs 优先覆盖。
        # SEC-21：过滤私有保留键——以 ``_`` 开头或以 ``_encrypted`` 结尾的键是
        # 集成层回填的元数据（如 OortCloud 授权令牌密文），合入请求体等于把
        # 密钥类字段泄漏给上游服务，此处一律排除。
        body.update(
            {
                k: v
                for k, v in (self.extra_params or {}).items()
                if not k.startswith("_") and not k.endswith("_encrypted")
            }
        )
        body.update(kwargs)
        # 不支持 reasoning_content 的 provider（OpenAI/Qwen/Doubao 等）需从 assistant
        # 消息中剥离该字段，否则请求会被拒绝；仅 DeepSeek 等推理模型保留。
        if not self.supports_reasoning_content:
            for msg in body.get("messages", []):
                if msg.get("role") == "assistant" and "reasoning_content" in msg:
                    del msg["reasoning_content"]
        else:
            # DeepSeek 思考模式（实测 1.0.97 / v4 系列）：带 tool_calls 的 assistant
            # 消息**必须**回传 reasoning_content，否则 400 "The reasoning_content in
            # the thinking mode must be passed back to the API"；实测空字符串即可通过。
            # 兜底填充：历史消息可能由旧版本落库（未存思维链）或模型本轮未输出思考链，
            # 在此统一补空串，保证含工具轮历史的请求（多轮对话 / 重新回答）不被拒绝。
            for msg in body.get("messages", []):
                if msg.get("role") == "assistant" and msg.get("tool_calls") and not msg.get("reasoning_content"):
                    msg["reasoning_content"] = ""
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
        """带指数退避重试的流式请求。

        SEC-17：重试携带增量游标。流中途失败后重试会重新生成同一段内容，若不裁剪
        就会把已推给前端的正文/思维链再发一遍。这里用 :class:`_ResumeCursor`
        记住已产出量，重试时跳过等长前缀；工具调用按条目数去重。
        """
        last_error: Exception | None = None
        text_cursor = _ResumeCursor()
        reasoning_cursor = _ResumeCursor()
        emitted_tool_calls = 0

        for attempt in range(max_retries):
            text_cursor.begin_attempt()
            reasoning_cursor.begin_attempt()
            skip_tools = emitted_tool_calls
            try:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    async with client.stream("POST", url, json=body, headers=self._headers()) as response:
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
                            except json.JSONDecodeError:
                                logger.debug("跳过无法解析的 SSE 行: %s", data_str[:100])
                                continue
                            parsed = self._parse_stream_chunk(data)
                            if not parsed:
                                continue
                            # ── 增量续传：裁剪掉本轮重试中已产出过的前缀 ──
                            if parsed.get("delta"):
                                kept = text_cursor.filter(parsed["delta"])
                                if kept:
                                    parsed["delta"] = kept
                                else:
                                    del parsed["delta"]
                            if parsed.get("reasoning_content"):
                                kept_r = reasoning_cursor.filter(parsed["reasoning_content"])
                                if kept_r:
                                    parsed["reasoning_content"] = kept_r
                                else:
                                    del parsed["reasoning_content"]
                            if parsed.get("tool_calls"):
                                calls = parsed["tool_calls"]
                                if skip_tools >= len(calls):
                                    skip_tools -= len(calls)
                                    del parsed["tool_calls"]
                                elif skip_tools:
                                    parsed["tool_calls"] = calls[skip_tools:]
                                    skip_tools = 0
                            if not parsed:
                                continue
                            yield parsed
                            # 记账放在 yield 之后：调用方若在 yield 处取消，
                            # 该内容并未送达，不计入已产出（避免下次重试丢字）。
                            text_cursor.record(parsed.get("delta", ""))
                            reasoning_cursor.record(parsed.get("reasoning_content", ""))
                            if parsed.get("tool_calls"):
                                emitted_tool_calls += len(parsed["tool_calls"])

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
            except (httpx.TimeoutException, httpx.TransportError) as e:
                # TransportError 覆盖 ConnectError / ReadError / RemoteProtocolError：
                # 流中途连接被重置原来会被漏掉，既不重试也不上报，直接静默截断。
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

        raise ProviderError(f"{self.provider_name} 请求失败，已重试 {max_retries} 次: {last_error}")

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
        # 最后一个块携带 usage（token 用量统计），无新增内容也需透传
        if "usage" in data and data.get("usage"):
            result["usage"] = data["usage"]

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
