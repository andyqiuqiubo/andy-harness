"""OpenAICompatibleProvider 测试（mock HTTP）。"""

import json

import pytest

from harness.modules.model_manager.openai_compatible import (
    OpenAICompatibleProvider,
    ProviderError,
)


class TestProvider(OpenAICompatibleProvider):
    """测试用 provider。"""

    base_url = "https://test.example.com/v1"
    default_models = ["test-model-1", "test-model-2"]
    provider_name = "test"


class TestStreamParsing:
    """测试 SSE 流式解析。"""

    def test_parse_content_delta(self) -> None:
        """解析普通 content delta。"""
        provider = TestProvider(api_key="sk-test")
        data = {"choices": [{"delta": {"content": "hello"}}]}
        result = provider._parse_stream_chunk(data)
        assert result is not None
        assert result["delta"] == "hello"

    def test_parse_tool_calls(self) -> None:
        """解析 tool_calls。"""
        provider = TestProvider(api_key="sk-test")
        data = {"choices": [{"delta": {"tool_calls": [{"id": "call-1", "function": {"name": "calc"}}]}}]}
        result = provider._parse_stream_chunk(data)
        assert result is not None
        assert "tool_calls" in result

    def test_parse_empty_delta(self) -> None:
        """空 delta 返回 None。"""
        provider = TestProvider(api_key="sk-test")
        data = {"choices": [{"delta": {}}]}
        result = provider._parse_stream_chunk(data)
        assert result is None

    def test_parse_no_choices(self) -> None:
        """无 choices 返回 None。"""
        provider = TestProvider(api_key="sk-test")
        data = {"choices": []}
        result = provider._parse_stream_chunk(data)
        assert result is None

    def test_parse_reasoning_content(self) -> None:
        """解析 reasoning_content（DeepSeek 特有）。"""
        provider = TestProvider(api_key="sk-test")
        data = {"choices": [{"delta": {"reasoning_content": "thinking..."}}]}
        result = provider._parse_stream_chunk(data)
        assert result is not None
        assert result["reasoning_content"] == "thinking..."


class TestTokenCounter:
    """测试 TokenCounter 实现。"""

    def test_count_tokens_returns_positive(self) -> None:
        """count_tokens 返回正整数。"""
        provider = TestProvider(api_key="sk-test")
        result = provider.count_tokens("你好世界", "test-model-1")
        assert isinstance(result, int)
        assert result > 0

    def test_count_tokens_longer_text_more_tokens(self) -> None:
        """更长文本产生更多 token。"""
        provider = TestProvider(api_key="sk-test")
        short = provider.count_tokens("hi", "test-model-1")
        long = provider.count_tokens("Hello, this is a much longer text.", "test-model-1")
        assert long > short

    def test_count_tokens_via_service_registry(self) -> None:
        """通过 ServiceRegistry 调用 TokenCounter。"""
        from harness.kernel.contracts.token_counter import TokenCounter
        from harness.kernel.services import ServiceRegistry

        provider = TestProvider(api_key="sk-test")
        registry = ServiceRegistry()
        registry.register(TokenCounter, provider, owner="test")

        counter = registry.get(TokenCounter)
        assert isinstance(counter, TokenCounter)
        result = counter.count_tokens("你好世界", "test-model-1")
        assert result > 0


class TestProviderErrorHandling:
    """测试错误处理。"""

    def test_provider_error_has_status_code(self) -> None:
        """ProviderError 携带 status_code。"""
        err = ProviderError("test error", status_code=401)
        assert err.status_code == 401
        assert "test error" in str(err)

    def test_parse_error_response_json(self) -> None:
        """解析 JSON 格式的错误响应。"""
        provider = TestProvider(api_key="sk-test")
        error_text = json.dumps({"error": {"message": "Invalid API key"}})
        msg = provider._parse_error_response(error_text, 401)
        assert "Invalid API key" in msg

    def test_parse_error_response_plain_text(self) -> None:
        """解析纯文本错误响应。"""
        provider = TestProvider(api_key="sk-test")
        msg = provider._parse_error_response("Bad Request", 400)
        assert "Bad Request" in msg


class TestListModels:
    """测试模型列表。"""

    @pytest.mark.asyncio
    async def test_list_models(self) -> None:
        """返回默认模型列表。"""
        provider = TestProvider(api_key="sk-test")
        models = await provider.list_models()
        assert "test-model-1" in models
        assert "test-model-2" in models

    def test_custom_models_override(self) -> None:
        """自定义 models 覆盖默认列表。"""
        provider = TestProvider(
            api_key="sk-test",
            models=["custom-model"],
        )
        assert provider.models == ["custom-model"]


class TestBuildRequestBody:
    """测试请求体构建。"""

    def test_build_request_body_basic(self) -> None:
        """基本请求体构建。"""
        provider = TestProvider(api_key="sk-test")
        body = provider._build_request_body(
            messages=[{"role": "user", "content": "hi"}],
            model="test-model-1",
            stream=True,
        )
        assert body["model"] == "test-model-1"
        assert body["stream"] is True
        assert body["messages"] == [{"role": "user", "content": "hi"}]

    def test_build_request_body_with_extra_params(self) -> None:
        """带额外参数的请求体。"""
        provider = TestProvider(
            api_key="sk-test",
            extra_params={"temperature": 0.7},
        )
        body = provider._build_request_body(
            messages=[{"role": "user", "content": "hi"}],
            model="test-model-1",
            temperature=0.5,  # kwargs 覆盖 extra_params
        )
        assert body["temperature"] == 0.5  # kwargs 优先

    def test_headers_contain_auth(self) -> None:
        """请求头包含 Authorization。"""
        provider = TestProvider(api_key="sk-test-key")
        headers = provider._headers()
        assert headers["Authorization"] == "Bearer sk-test-key"
        assert headers["Content-Type"] == "application/json"


class TestReasoningContentPassthrough:
    """测试 assistant 消息的 reasoning_content 回传行为（DeepSeek 多轮要求）。"""

    def test_strip_reasoning_content_when_unsupported(self) -> None:
        """不支持 reasoning_content 的 provider 应剥离该字段，避免 400。"""
        provider = TestProvider(api_key="sk-test")
        assert provider.supports_reasoning_content is False
        messages = [
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "你好！", "reasoning_content": "思考过程"},
        ]
        body = provider._build_request_body(messages=messages, model="test-model-1")
        assert "reasoning_content" not in body["messages"][1]

    def test_keep_reasoning_content_when_supported(self) -> None:
        """支持 reasoning_content 的 provider（如 DeepSeek）应保留该字段。"""

        class ReasoningProvider(TestProvider):
            supports_reasoning_content = True

        provider = ReasoningProvider(api_key="sk-test")
        messages = [
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "你好！", "reasoning_content": "思考过程"},
        ]
        body = provider._build_request_body(messages=messages, model="test-model-1")
        assert body["messages"][1].get("reasoning_content") == "思考过程"

    def test_non_assistant_reasoning_content_untouched(self) -> None:
        """仅剥离 assistant 角色上的 reasoning_content，其他角色不误伤。"""
        provider = TestProvider(api_key="sk-test")
        messages = [
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "你好！", "reasoning_content": "思考过程"},
        ]
        body = provider._build_request_body(messages=messages, model="test-model-1")
        assert body["messages"][0] == {"role": "user", "content": "你好"}

    def test_tool_call_assistant_gets_empty_reasoning_content_when_supported(self) -> None:
        """DeepSeek 思考模式：带 tool_calls 的 assistant 消息缺 rc 时补空串。

        实测（deepseek v4 系列，2026-10）：tool_calls 消息不带 reasoning_content
        会 400 "The reasoning_content in the thinking mode must be passed back
        to the API"，空字符串即可通过；普通 assistant 消息无需该字段。
        历史消息（旧版落库 / 模型未输出思考链）由此兜底，重新回答不再报 400。
        """

        class ReasoningProvider(TestProvider):
            supports_reasoning_content = True

        provider = ReasoningProvider(api_key="sk-test")
        messages = [
            {"role": "user", "content": "hi"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": "c1", "type": "function", "function": {"name": "t", "arguments": "{}"}},
                ],
            },
            {"role": "tool", "tool_call_id": "c1", "content": "r"},
            # 普通 assistant 消息缺 rc：无需填充
            {"role": "assistant", "content": "plain answer"},
        ]
        body = provider._build_request_body(messages=messages, model="m1")
        assert body["messages"][1]["reasoning_content"] == ""
        assert "reasoning_content" not in body["messages"][3]

    def test_tool_call_assistant_with_reasoning_content_untouched(self) -> None:
        """已有 rc 的 tool_calls 消息保持原值，不被覆盖。"""

        class ReasoningProvider(TestProvider):
            supports_reasoning_content = True

        provider = ReasoningProvider(api_key="sk-test")
        messages = [
            {
                "role": "assistant",
                "content": "",
                "reasoning_content": "已存思维链",
                "tool_calls": [
                    {"id": "c1", "type": "function", "function": {"name": "t", "arguments": "{}"}},
                ],
            },
        ]
        body = provider._build_request_body(messages=messages, model="m1")
        assert body["messages"][0]["reasoning_content"] == "已存思维链"

    def test_unsupported_provider_strips_even_on_tool_calls(self) -> None:
        """不支持的 provider：tool_calls 消息上的 rc 一律剥离（维持原有行为）。"""
        provider = TestProvider(api_key="sk-test")
        messages = [
            {
                "role": "assistant",
                "content": "",
                "reasoning_content": "x",
                "tool_calls": [
                    {"id": "c1", "type": "function", "function": {"name": "t", "arguments": "{}"}},
                ],
            },
        ]
        body = provider._build_request_body(messages=messages, model="m1")
        assert "reasoning_content" not in body["messages"][0]
