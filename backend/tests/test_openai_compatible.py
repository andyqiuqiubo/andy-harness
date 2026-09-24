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
        data = {
            "choices": [
                {"delta": {"content": "hello"}}
            ]
        }
        result = provider._parse_stream_chunk(data)
        assert result is not None
        assert result["delta"] == "hello"

    def test_parse_tool_calls(self) -> None:
        """解析 tool_calls。"""
        provider = TestProvider(api_key="sk-test")
        data = {
            "choices": [
                {"delta": {"tool_calls": [{"id": "call-1", "function": {"name": "calc"}}]}}
            ]
        }
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
        data = {
            "choices": [
                {"delta": {"reasoning_content": "thinking..."}}
            ]
        }
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
