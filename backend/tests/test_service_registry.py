"""ServiceRegistry 单元测试。"""

import pytest

from harness.kernel.exceptions import ServiceUnavailable
from harness.kernel.services import ServiceRegistry


class TestService:
    """测试用服务接口。"""


class ImplA:
    """实现 A。"""

    name = "A"


class ImplB:
    """实现 B。"""

    name = "B"


def test_register_and_get() -> None:
    """注册服务并通过接口获取实现。"""
    registry = ServiceRegistry()
    impl = ImplA()
    registry.register(TestService, impl, owner="plugin-a")

    result = registry.get(TestService)
    assert result is impl


def test_service_unavailable() -> None:
    """未注册的服务抛出 ServiceUnavailable。"""
    registry = ServiceRegistry()

    with pytest.raises(ServiceUnavailable) as exc_info:
        registry.get(TestService)

    assert "TestService" in str(exc_info.value)


def test_later_registration_overrides() -> None:
    """后注册覆盖先注册。"""
    registry = ServiceRegistry()
    impl_a = ImplA()
    impl_b = ImplB()

    registry.register(TestService, impl_a, owner="plugin-a")
    assert registry.get(TestService) is impl_a

    registry.register(TestService, impl_b, owner="plugin-b")
    assert registry.get(TestService) is impl_b  # 后注册的覆盖


def test_fallback_after_unregister() -> None:
    """注销后注册者后回退到先注册者。"""
    registry = ServiceRegistry()
    impl_a = ImplA()
    impl_b = ImplB()

    registry.register(TestService, impl_a, owner="plugin-a")
    registry.register(TestService, impl_b, owner="plugin-b")
    assert registry.get(TestService) is impl_b

    registry.unregister(TestService, owner="plugin-b")
    assert registry.get(TestService) is impl_a  # 回退到 A


def test_unregister_all_by_owner() -> None:
    """按 owner 批量注销。"""
    registry = ServiceRegistry()
    impl_a = ImplA()

    registry.register(TestService, impl_a, owner="plugin-a")
    assert registry.has(TestService)

    registry.unregister_all("plugin-a")
    assert not registry.has(TestService)


def test_has() -> None:
    """检查服务是否已注册。"""
    registry = ServiceRegistry()
    assert not registry.has(TestService)

    registry.register(TestService, ImplA(), owner="plugin-a")
    assert registry.has(TestService)
