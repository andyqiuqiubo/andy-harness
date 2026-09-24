"""PluginLoader 单元测试。"""

import json
from pathlib import Path

import pytest

from harness.kernel.exceptions import (
    PluginDeactivateError,
    PluginValidationError,
)
from harness.kernel.loader import PluginLoader

PLUGIN_ID = "hello_plugin"


class TestManifestValidation:
    """测试清单校验。"""

    def setup_method(self) -> None:
        self.loader = PluginLoader()

    def test_valid_manifest(self) -> None:
        """合法清单通过校验。"""
        raw = {
            "id": "test-plugin",
            "name": "Test Plugin",
            "version": "0.1.0",
            "type": "service",
            "entry": "main:TestPlugin",
            "core_api": ">=0.1.0 <1.0.0",
        }
        manifest = self.loader.validate_manifest(raw)
        assert manifest.id == "test-plugin"
        assert manifest.core is False

    def test_missing_required_field(self) -> None:
        """缺少必填字段时校验失败。"""
        raw = {
            "id": "test-plugin",
            "name": "Test Plugin",
        }
        with pytest.raises(PluginValidationError) as exc_info:
            self.loader.validate_manifest(raw)
        assert "version" in str(exc_info.value)

    def test_invalid_type(self) -> None:
        """无效的 type 校验失败。"""
        raw = {
            "id": "test-plugin",
            "name": "Test Plugin",
            "version": "0.1.0",
            "type": "invalid",
            "entry": "main:TestPlugin",
        }
        with pytest.raises(PluginValidationError) as exc_info:
            self.loader.validate_manifest(raw)
        assert "invalid" in str(exc_info.value)

    def test_incompatible_core_api(self) -> None:
        """core_api 版本不兼容时校验失败。"""
        raw = {
            "id": "test-plugin",
            "name": "Test Plugin",
            "version": "0.1.0",
            "type": "service",
            "entry": "main:TestPlugin",
            "core_api": ">=2.0.0 <3.0.0",
        }
        with pytest.raises(PluginValidationError) as exc_info:
            self.loader.validate_manifest(raw)
        assert "不兼容" in str(exc_info.value)

    def test_core_flag(self) -> None:
        """core: true 标记被正确解析。"""
        raw = {
            "id": "core-plugin",
            "name": "Core Plugin",
            "version": "0.1.0",
            "type": "service",
            "entry": "main:CorePlugin",
            "core": True,
        }
        manifest = self.loader.validate_manifest(raw)
        assert manifest.core is True


class TestPluginLoaderIntegration:
    """PluginLoader 集成测试（使用 hello_plugin）。"""

    @pytest.fixture
    def plugins_dir(self) -> str:
        return str(Path(__file__).parent.parent / "plugins")

    @pytest.mark.asyncio
    async def test_load_and_activate_hello_plugin(self, plugins_dir: str) -> None:
        """加载并激活 hello_plugin。"""
        loader = PluginLoader()
        manifest_paths = loader.discover(plugins_dir)

        hello_path = next(p for p in manifest_paths if "hello" in str(p))
        raw = json.loads(hello_path.read_text(encoding="utf-8"))
        manifest = loader.validate_manifest(raw)

        loader.load(manifest, plugins_dir)
        await loader.activate(PLUGIN_ID)

        assert loader.is_activated(PLUGIN_ID)

        await loader.deactivate(PLUGIN_ID)

    @pytest.mark.asyncio
    async def test_service_available_after_activate(self, plugins_dir: str) -> None:
        """激活后服务可用。"""
        from plugins.hello_plugin.main import HelloService

        loader = PluginLoader()
        raw = json.loads(
            (Path(plugins_dir) / "hello_plugin" / "plugin.json").read_text(
                encoding="utf-8"
            )
        )
        manifest = loader.validate_manifest(raw)
        loader.load(manifest, plugins_dir)
        await loader.activate(PLUGIN_ID)

        service = loader.services.get(HelloService)
        result = service.greet("Test")
        assert "Hello, Test!" in result

        await loader.deactivate(PLUGIN_ID)

    @pytest.mark.asyncio
    async def test_service_unavailable_after_deactivate(self, plugins_dir: str) -> None:
        """停用后服务不可用（ServiceUnavailable）。"""
        from harness.kernel.exceptions import ServiceUnavailable
        from plugins.hello_plugin.main import HelloService

        loader = PluginLoader()
        raw = json.loads(
            (Path(plugins_dir) / "hello_plugin" / "plugin.json").read_text(
                encoding="utf-8"
            )
        )
        manifest = loader.validate_manifest(raw)
        loader.load(manifest, plugins_dir)
        await loader.activate(PLUGIN_ID)

        assert loader.services.has(HelloService)

        await loader.deactivate(PLUGIN_ID)

        with pytest.raises(ServiceUnavailable):
            loader.services.get(HelloService)

    @pytest.mark.asyncio
    async def test_event_unsubscribed_after_deactivate(self, plugins_dir: str) -> None:
        """停用后事件订阅自动注销。"""
        loader = PluginLoader()
        raw = json.loads(
            (Path(plugins_dir) / "hello_plugin" / "plugin.json").read_text(
                encoding="utf-8"
            )
        )
        manifest = loader.validate_manifest(raw)
        loader.load(manifest, plugins_dir)
        await loader.activate(PLUGIN_ID)

        received: list[dict] = []

        await loader.events.subscribe(
            "plugin.activated",
            lambda data: received.append(data),
            owner="test-subscriber",
        )

        await loader.deactivate(PLUGIN_ID)

        assert PLUGIN_ID not in loader.events._subscriptions_by_owner

        await loader.events.unsubscribe_all("test-subscriber")

    @pytest.mark.asyncio
    async def test_core_plugin_cannot_deactivate(self, plugins_dir: str) -> None:
        """核心插件不可停用。"""
        loader = PluginLoader()

        raw = {
            "id": PLUGIN_ID,
            "name": "Hello Plugin",
            "version": "0.1.0",
            "type": "service",
            "entry": "plugins.hello_plugin.main:HelloPlugin",
            "core_api": ">=0.1.0 <1.0.0",
            "core": True,
        }
        manifest = loader.validate_manifest(raw)
        loader.load(manifest, plugins_dir)
        await loader.activate(PLUGIN_ID)

        with pytest.raises(PluginDeactivateError) as exc_info:
            await loader.deactivate(PLUGIN_ID)

        assert "核心插件" in str(exc_info.value)
        assert loader.is_activated(PLUGIN_ID)


class TestServiceOverride:
    """测试服务注册覆盖策略。"""

    @pytest.mark.asyncio
    async def test_two_plugins_same_service(self) -> None:
        """两个插件注册同一服务接口，后注册者覆盖前者。"""
        from harness.kernel.context import PluginContext
        from harness.kernel.contracts.base import BasePlugin, PluginManifest

        class MyService:
            pass

        class PluginA(BasePlugin):
            manifest = PluginManifest(
                id="plugin-a", name="A", version="0.1.0", type="service",
                entry="main:PluginA",
            )

            async def activate(self, ctx: PluginContext) -> None:
                ctx.services.register(MyService, "impl-a", owner="plugin-a")

            async def deactivate(self, ctx: PluginContext) -> None:
                pass

        class PluginB(BasePlugin):
            manifest = PluginManifest(
                id="plugin-b", name="B", version="0.1.0", type="service",
                entry="main:PluginB",
            )

            async def activate(self, ctx: PluginContext) -> None:
                ctx.services.register(MyService, "impl-b", owner="plugin-b")

            async def deactivate(self, ctx: PluginContext) -> None:
                pass

        loader = PluginLoader()

        ctx_a = PluginContext(
            "plugin-a", events=loader.events, services=loader.services,
            hooks=loader.hooks,
        )
        loader._loaded["plugin-a"] = (PluginA(), PluginA.manifest, ctx_a)
        await loader.activate("plugin-a")
        assert loader.services.get(MyService) == "impl-a"

        ctx_b = PluginContext(
            "plugin-b", events=loader.events, services=loader.services,
            hooks=loader.hooks,
        )
        loader._loaded["plugin-b"] = (PluginB(), PluginB.manifest, ctx_b)
        await loader.activate("plugin-b")
        assert loader.services.get(MyService) == "impl-b"

        await loader.deactivate("plugin-b")
        assert loader.services.get(MyService) == "impl-a"

        await loader.deactivate("plugin-a")


class TestTokenCounterContract:
    """测试 TokenCounter 契约接口。"""

    def test_mock_token_counter(self) -> None:
        """TokenCounter 契约可被 mock 实现并通过 ServiceRegistry 调用。"""
        from harness.kernel.contracts.token_counter import TokenCounter
        from harness.kernel.services import ServiceRegistry

        class MockTokenCounter(TokenCounter):
            def count_tokens(self, text: str, model: str) -> int:
                return len(text) // 4

        registry = ServiceRegistry()
        counter = MockTokenCounter()
        registry.register(TokenCounter, counter, owner="test")

        retrieved = registry.get(TokenCounter)
        assert isinstance(retrieved, TokenCounter)
        result = retrieved.count_tokens("你好世界", "deepseek-chat")
        assert result > 0
        assert isinstance(result, int)
