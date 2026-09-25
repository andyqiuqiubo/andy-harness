"""ProviderRegistry 测试。"""

import pytest

from harness.modules.model_manager.openai_compatible import (
    OpenAICompatibleProvider,
)
from harness.modules.model_manager.provider_registry import (
    ProviderConfigError,
    ProviderNotFoundError,
    ProviderRegistry,
)


class TestProviderImpl(OpenAICompatibleProvider):
    """测试用 provider 实现。"""

    base_url = "https://test.example.com/v1"
    default_models = ["test-model"]
    provider_name = "test"


class CustomProvider(OpenAICompatibleProvider):
    """自定义 provider 实现。"""

    base_url = ""
    default_models = ["custom-model"]
    provider_name = "custom"


class TestProviderRegistry:
    """ProviderRegistry 测试。"""

    def test_register_and_get(self) -> None:
        """注册并获取 provider。"""
        registry = ProviderRegistry()
        registry.register_provider(
            "test",
            TestProviderImpl,
            {"api_key": "sk-test", "base_url": "https://test.example.com/v1"},
        )

        provider = registry.get_provider("test")
        assert isinstance(provider, TestProviderImpl)
        assert provider.api_key == "sk-test"

    def test_get_not_found(self) -> None:
        """获取未注册的 provider 抛出异常。"""
        registry = ProviderRegistry()

        with pytest.raises(ProviderNotFoundError):
            registry.get_provider("nonexistent")

    def test_unregister(self) -> None:
        """注销 provider。"""
        registry = ProviderRegistry()
        registry.register_provider(
            "test", TestProviderImpl, {"api_key": "sk-test"}
        )
        assert registry.has_provider("test")

        registry.unregister_provider("test")
        assert not registry.has_provider("test")

    def test_missing_api_key_raises_error(self) -> None:
        """缺少 API Key 抛出配置错误。"""
        registry = ProviderRegistry()
        registry.register_provider("test", TestProviderImpl, {})

        with pytest.raises(ProviderConfigError):
            registry.get_provider("test")

    def test_list_providers(self) -> None:
        """列出所有 provider。"""
        registry = ProviderRegistry()
        registry.register_provider(
            "test", TestProviderImpl,
            {"name": "Test", "api_key": "sk-test"},
        )
        registry.register_provider(
            "custom", CustomProvider,
            {"name": "Custom", "api_key": "sk-custom"},
        )

        providers = registry.list_providers()
        assert len(providers) == 2
        ids = [p["id"] for p in providers]
        assert "test" in ids
        assert "custom" in ids
        # 默认启用
        assert all(p["enabled"] is True for p in providers)
        # 返回完整字段
        assert all("has_api_key" in p and "base_url" in p for p in providers)

    def test_disabled_provider_raises_error(self) -> None:
        """已停用的 provider 不可获取实例（除非 include_disabled）。"""
        registry = ProviderRegistry()
        registry.register_provider(
            "test", TestProviderImpl,
            {"api_key": "sk-test", "enabled": False},
        )

        with pytest.raises(ProviderConfigError, match="已停用"):
            registry.get_provider("test")

        # include_disabled=True 时允许获取（用于连接测试）
        provider = registry.get_provider("test", include_disabled=True)
        assert provider.api_key == "sk-test"

    def test_update_config_clears_cache(self) -> None:
        """更新配置后清除实例缓存。"""
        registry = ProviderRegistry()
        registry.register_provider(
            "test", TestProviderImpl,
            {"api_key": "sk-old", "base_url": "https://old.example.com/v1"},
        )

        provider1 = registry.get_provider("test")
        assert provider1.api_key == "sk-old"

        registry.update_provider_config(
            "test",
            {"api_key": "sk-new", "base_url": "https://new.example.com/v1"},
        )

        provider2 = registry.get_provider("test")
        assert provider2.api_key == "sk-new"
        assert provider2 is not provider1  # 新实例


class TestCustomProvider:
    """测试自定义 provider（与内置 provider 走相同代码路径）。"""

    def test_custom_provider_same_code_path(self) -> None:
        """自定义 provider 与内置 provider 使用相同的 ProviderRegistry 代码路径。"""
        registry = ProviderRegistry()

        # 注册内置 provider
        registry.register_provider(
            "builtin", TestProviderImpl,
            {"name": "Builtin", "api_key": "sk-builtin"},
        )

        # 注册自定义 provider
        registry.register_provider(
            "custom", CustomProvider,
            {
                "name": "Custom",
                "api_key": "sk-custom",
                "base_url": "https://custom.example.com/v1",
                "models": ["custom-model"],
            },
        )

        # 两者走完全相同的代码路径
        builtin = registry.get_provider("builtin")
        custom = registry.get_provider("custom")

        assert isinstance(builtin, OpenAICompatibleProvider)
        assert isinstance(custom, OpenAICompatibleProvider)
        assert builtin.api_key == "sk-builtin"
        assert custom.api_key == "sk-custom"
        assert custom.base_url == "https://custom.example.com/v1"
