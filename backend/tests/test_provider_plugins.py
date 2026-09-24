"""Provider 插件集成测试（不依赖真实 API Key）。"""

import pytest

from harness.kernel.context import PluginContext
from harness.kernel.contracts.token_counter import TokenCounter
from harness.kernel.eventbus import EventBus
from harness.kernel.hooks import HookManager
from harness.kernel.services import ServiceRegistry
from harness.modules.model_manager.provider_registry import ProviderRegistry


class TestDeepSeekPlugin:
    """DeepSeek provider 插件测试。"""

    @pytest.fixture
    def ctx(self) -> PluginContext:
        """创建带 ProviderRegistry 的 PluginContext。"""
        services = ServiceRegistry()
        services.register(ProviderRegistry, ProviderRegistry(), owner="test")
        return PluginContext(
            "provider_deepseek",
            events=EventBus(),
            services=services,
            hooks=HookManager(),
        )

    @pytest.mark.asyncio
    async def test_activate_registers_provider(self, ctx: PluginContext) -> None:
        """激活后 provider 注册到 ProviderRegistry。"""
        from plugins.provider_deepseek.main import DeepSeekProviderPlugin

        plugin = DeepSeekProviderPlugin()
        plugin.manifest = type(
            "M", (), {"id": "provider_deepseek", "core": False}
        )()  # type: ignore[attr-defined]

        # 设置配置
        ctx.config.set("api_key", "sk-test-deepseek")
        ctx.config.set("base_url", "https://api.deepseek.com")

        await plugin.activate(ctx)

        registry = ctx.services.get(ProviderRegistry)
        assert registry.has_provider("deepseek")

        # 清理
        await plugin.deactivate(ctx)

    @pytest.mark.asyncio
    async def test_token_counter_registered(self, ctx: PluginContext) -> None:
        """激活后 TokenCounter 注册到 ServiceRegistry。"""
        from plugins.provider_deepseek.main import DeepSeekProviderPlugin

        plugin = DeepSeekProviderPlugin()
        plugin.manifest = type(
            "M", (), {"id": "provider_deepseek", "core": False}
        )()  # type: ignore[attr-defined]

        ctx.config.set("api_key", "sk-test-deepseek")
        await plugin.activate(ctx)

        assert ctx.services.has(TokenCounter)
        counter = ctx.services.get(TokenCounter)
        result = counter.count_tokens("你好世界", "deepseek-chat")
        assert result > 0

        # 清理
        await plugin.deactivate(ctx)

    @pytest.mark.asyncio
    async def test_deactivate_unregisters_provider(self, ctx: PluginContext) -> None:
        """停用后 provider 从 ProviderRegistry 注销。"""
        from plugins.provider_deepseek.main import DeepSeekProviderPlugin

        plugin = DeepSeekProviderPlugin()
        plugin.manifest = type(
            "M", (), {"id": "provider_deepseek", "core": False}
        )()  # type: ignore[attr-defined]

        ctx.config.set("api_key", "sk-test-deepseek")
        await plugin.activate(ctx)

        registry = ctx.services.get(ProviderRegistry)
        assert registry.has_provider("deepseek")

        await plugin.deactivate(ctx)
        assert not registry.has_provider("deepseek")


class TestQwenPlugin:
    """Qwen provider 插件测试。"""

    @pytest.mark.asyncio
    async def test_activate_registers_provider(self) -> None:
        """激活后 provider 注册。"""
        from plugins.provider_qwen.main import QwenProviderPlugin

        services = ServiceRegistry()
        services.register(ProviderRegistry, ProviderRegistry(), owner="test")
        ctx = PluginContext(
            "provider_qwen",
            services=services,
            events=EventBus(),
            hooks=HookManager(),
        )

        plugin = QwenProviderPlugin()
        plugin.manifest = type(
            "M", (), {"id": "provider_qwen", "core": False}
        )()  # type: ignore[attr-defined]

        ctx.config.set("api_key", "sk-test-qwen")
        await plugin.activate(ctx)

        registry = ctx.services.get(ProviderRegistry)
        assert registry.has_provider("qwen")

        await plugin.deactivate(ctx)


class TestDoubaoPlugin:
    """Doubao provider 插件测试。"""

    @pytest.mark.asyncio
    async def test_activate_registers_provider(self) -> None:
        """激活后 provider 注册。"""
        from plugins.provider_doubao.main import DoubaoProviderPlugin

        services = ServiceRegistry()
        services.register(ProviderRegistry, ProviderRegistry(), owner="test")
        ctx = PluginContext(
            "provider_doubao",
            services=services,
            events=EventBus(),
            hooks=HookManager(),
        )

        plugin = DoubaoProviderPlugin()
        plugin.manifest = type(
            "M", (), {"id": "provider_doubao", "core": False}
        )()  # type: ignore[attr-defined]

        ctx.config.set("api_key", "sk-test-doubao")
        await plugin.activate(ctx)

        registry = ctx.services.get(ProviderRegistry)
        assert registry.has_provider("doubao")

        await plugin.deactivate(ctx)


class TestTokenCounterViaServiceRegistry:
    """通过 ServiceRegistry 调用 TokenCounter（DoD #4）。"""

    @pytest.mark.asyncio
    async def test_get_token_counter_returns_reasonable_count(self) -> None:
        """services.get(TokenCounter).count("你好", "deepseek-chat") 返回合理 token 数。"""
        from plugins.provider_deepseek.main import DeepSeekProviderPlugin

        services = ServiceRegistry()
        services.register(ProviderRegistry, ProviderRegistry(), owner="test")
        ctx = PluginContext(
            "provider_deepseek",
            services=services,
            events=EventBus(),
            hooks=HookManager(),
        )

        plugin = DeepSeekProviderPlugin()
        plugin.manifest = type(
            "M", (), {"id": "provider_deepseek", "core": False}
        )()  # type: ignore[attr-defined]

        ctx.config.set("api_key", "sk-test")
        await plugin.activate(ctx)

        counter = ctx.services.get(TokenCounter)
        result = counter.count_tokens("你好", "deepseek-chat")
        assert isinstance(result, int)
        assert result > 0

        await plugin.deactivate(ctx)
