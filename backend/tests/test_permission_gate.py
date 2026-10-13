"""CORE-01 权限网关测试。

验证：
- 无权限插件 → 放行（返回 None）；
- 权限服务异常 + 高危工具 → fail-closed 拒绝（DeniedDecision.denied=True）；
- 权限服务异常 + 低危工具 → 放行（返回 None，防自 DoS）；
- 服务正常 → 透传真实决策。
"""

from __future__ import annotations

import types

import pytest

import harness.modules.permission_manager.service as svc_mod
from harness.engine.permission_gate import DeniedDecision, check_tool_permission


class _FakeDecision:
    def __init__(self, *, denied: bool, needs_confirm: bool, risk: str = "low") -> None:
        self.tool_name = "tool"
        self.risk = risk
        self.action = "deny" if denied else "allow"
        self.reason = "policy"
        self.source = "policy"
        self.denied = denied
        self.needs_confirm = needs_confirm


@pytest.fixture
def fake_registry(monkeypatch: pytest.MonkeyPatch):
    """用一个假 PermissionService 替换真实类，避免加载真实权限服务依赖。"""

    class FakePermissionService:  # noqa: D401 - 仅作类型标记
        pass

    monkeypatch.setattr(svc_mod, "PermissionService", FakePermissionService)
    return FakePermissionService


def test_no_permission_plugin_returns_none(fake_registry) -> None:
    services = types.SimpleNamespace(has=lambda cls: False, get=lambda cls: None)
    assert check_tool_permission(services, "shell_execute") is None


def test_high_risk_fail_closed_on_service_error(fake_registry) -> None:
    failing = types.SimpleNamespace(
        decide=lambda name: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    services = types.SimpleNamespace(
        has=lambda cls: True,
        get=lambda cls: failing,
    )
    result = check_tool_permission(services, "shell_execute")
    assert isinstance(result, DeniedDecision)
    assert result.denied is True
    assert result.tool_name == "shell_execute"


def test_low_risk_fail_open_on_service_error(fake_registry) -> None:
    failing = types.SimpleNamespace(
        decide=lambda name: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    services = types.SimpleNamespace(
        has=lambda cls: True,
        get=lambda cls: failing,
    )
    # "summarize" 不在高危集合 → 放行（None）
    assert check_tool_permission(services, "summarize") is None


def test_normal_decision_passthrough(fake_registry) -> None:
    ok = types.SimpleNamespace(decide=lambda name: _FakeDecision(denied=False, needs_confirm=True))
    services = types.SimpleNamespace(has=lambda cls: True, get=lambda cls: ok)
    result = check_tool_permission(services, "any_tool")
    assert result is not None
    assert result.denied is False
    assert result.needs_confirm is True
