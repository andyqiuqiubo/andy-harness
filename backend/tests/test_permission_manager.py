"""权限管理与人工确认测试。"""

from __future__ import annotations

import os
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from harness.engine.agent_loop import AgentLoop
from harness.engine.tool_registry import ToolRegistry
from harness.infra.database import Database
from harness.kernel.contracts.tool import ToolPlugin
from harness.kernel.hooks import HookManager
from harness.kernel.services import ServiceRegistry
from harness.modules.context_manager.service import ContextService, ContextServiceImpl
from harness.modules.permission_manager.service import (
    MODE_AUTO,
    MODE_CONFIRM_ALL,
    MODE_CONFIRM_DANGEROUS,
    MODE_CONFIRM_WRITE,
    PermissionService,
    PermissionServiceImpl,
)
from harness.modules.session_manager.service import SessionService, SessionServiceImpl


class ReadTool(ToolPlugin):
    """只读工具。"""

    calls: int = 0

    @property
    def tool_name(self) -> str:
        return "read_tool"

    @property
    def risk_level(self) -> str:
        return "read"

    @property
    def description(self) -> str:
        return "只读工具"

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self, args: dict[str, Any]) -> str:
        type(self).calls += 1
        return "读到了"


class WriteTool(ToolPlugin):
    """写工具。"""

    calls: int = 0

    @property
    def tool_name(self) -> str:
        return "write_tool"

    @property
    def risk_level(self) -> str:
        return "write"

    @property
    def description(self) -> str:
        return "写工具"

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self, args: dict[str, Any]) -> str:
        type(self).calls += 1
        return "写好了"


class DangerTool(ToolPlugin):
    """危险工具（可执行代码）。"""

    calls: int = 0

    @property
    def tool_name(self) -> str:
        return "danger_tool"

    @property
    def risk_level(self) -> str:
        return "dangerous"

    @property
    def description(self) -> str:
        return "危险工具"

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self, args: dict[str, Any]) -> str:
        type(self).calls += 1
        return "执行了"


@pytest.fixture
def tool_registry() -> ToolRegistry:
    """注册三类风险工具。"""
    registry = ToolRegistry()
    registry.register(ReadTool(), owner="test")
    registry.register(WriteTool(), owner="test")
    registry.register(DangerTool(), owner="test")
    return registry


@pytest.fixture
def service(tool_registry: ToolRegistry, tmp_path: Path) -> PermissionServiceImpl:
    """权限服务实例（状态文件在临时目录）。"""
    return PermissionServiceImpl(
        tool_registry=tool_registry,
        state_file=tmp_path / "permission.json",
        mode=MODE_CONFIRM_DANGEROUS,
    )


class TestRiskLevels:
    """风险等级判定。"""

    def test_default_risk_is_write(self) -> None:
        """未声明 risk_level 的工具默认 write。"""

        class PlainTool(ToolPlugin):
            @property
            def tool_name(self) -> str:
                return "plain"

            @property
            def description(self) -> str:
                return "普通工具"

            @property
            def parameters_schema(self) -> dict[str, Any]:
                return {}

            async def execute(self, args: dict[str, Any]) -> str:
                return "ok"

        assert PlainTool().risk_level == "write"

    def test_get_risk(self, service: PermissionServiceImpl) -> None:
        """按工具声明返回等级。"""
        assert service.get_risk("read_tool") == "read"
        assert service.get_risk("write_tool") == "write"
        assert service.get_risk("danger_tool") == "dangerous"

    def test_unknown_tool_is_dangerous(self, service: PermissionServiceImpl) -> None:
        """未注册工具按最保守等级处理。"""
        assert service.get_risk("not_registered") == "dangerous"


class TestDecisions:
    """策略决策。"""

    def test_confirm_dangerous_mode(self, service: PermissionServiceImpl) -> None:
        """默认策略：仅 dangerous 需确认。"""
        assert service.decide("read_tool").action == "allow"
        assert service.decide("write_tool").action == "allow"
        assert service.decide("danger_tool").action == "confirm"

    def test_auto_mode(self, service: PermissionServiceImpl) -> None:
        """auto 模式全部放行。"""
        service.set_mode(MODE_AUTO)
        for name in ("read_tool", "write_tool", "danger_tool"):
            assert service.decide(name).action == "allow"

    def test_confirm_write_mode(self, service: PermissionServiceImpl) -> None:
        """confirm_write：write 与 dangerous 需确认。"""
        service.set_mode(MODE_CONFIRM_WRITE)
        assert service.decide("read_tool").action == "allow"
        assert service.decide("write_tool").action == "confirm"
        assert service.decide("danger_tool").action == "confirm"

    def test_confirm_all_mode(self, service: PermissionServiceImpl) -> None:
        """confirm_all：全部需确认。"""
        service.set_mode(MODE_CONFIRM_ALL)
        for name in ("read_tool", "write_tool", "danger_tool"):
            assert service.decide(name).action == "confirm"

    def test_override_deny(self, service: PermissionServiceImpl) -> None:
        """单工具覆盖为 deny 优先于全局策略。"""
        service.set_override("read_tool", "deny")
        assert service.decide("read_tool").denied is True

    def test_override_auto_overrides_confirm_all(self, service: PermissionServiceImpl) -> None:
        """confirm_all 下单独放行某工具。"""
        service.set_mode(MODE_CONFIRM_ALL)
        service.set_override("read_tool", "auto")
        assert service.decide("read_tool").action == "allow"
        assert service.decide("write_tool").action == "confirm"

    def test_clear_override(self, service: PermissionServiceImpl) -> None:
        """清除覆盖后回到全局策略。"""
        service.set_override("read_tool", "deny")
        service.set_override("read_tool", None)
        assert service.decide("read_tool").action == "allow"
        assert "read_tool" not in service.get_overrides()

    def test_invalid_mode_rejected(self, service: PermissionServiceImpl) -> None:
        """无效模式被拒绝。"""
        assert service.set_mode("nonsense") is False
        assert service.get_mode() == MODE_CONFIRM_DANGEROUS

    def test_persistence(self, tool_registry: ToolRegistry, tmp_path: Path) -> None:
        """配置持久化，重建服务后保留。"""
        state = tmp_path / "p.json"
        svc = PermissionServiceImpl(tool_registry=tool_registry, state_file=state)
        svc.set_mode(MODE_CONFIRM_WRITE)
        svc.set_override("write_tool", "deny")

        svc2 = PermissionServiceImpl(tool_registry=tool_registry, state_file=state)
        assert svc2.get_mode() == MODE_CONFIRM_WRITE
        assert svc2.decide("write_tool").denied is True

    def test_list_tool_risks(self, service: PermissionServiceImpl) -> None:
        """工具清单按风险倒序，含生效动作。"""
        tools = service.list_tool_risks()
        names = [t["name"] for t in tools]
        assert names[0] == "danger_tool"
        assert {t["name"] for t in tools} == {"read_tool", "write_tool", "danger_tool"}


class ConfirmProvider:
    """Mock provider —— 首轮调用指定工具，次轮给出终答。"""

    def __init__(self, tool_name: str, args: str = "{}") -> None:
        self.tool_name = tool_name
        self.args = args
        self.call_count = 0

    async def chat(
        self,
        messages: list[dict[str, str]],
        model: str,
        stream: bool = True,
        **kwargs: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        self.call_count += 1
        if self.call_count == 1:
            yield {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call_1",
                        "function": {"name": self.tool_name, "arguments": self.args},
                    }
                ]
            }
        else:
            yield {"delta": "完成"}


class TestAgentLoopPermission:
    """AgentLoop 权限拦截测试。"""

    @staticmethod
    def _build(
        service: PermissionServiceImpl, tool_registry: ToolRegistry
    ) -> tuple[ServiceRegistry, SessionServiceImpl]:
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db = Database(path)
        registry = ServiceRegistry()
        registry.register(SessionService, SessionServiceImpl(db), owner="test")
        registry.register(ContextService, ContextServiceImpl(services=registry), owner="test")
        registry.register(PermissionService, service, owner="test")
        registry.register(ToolRegistry, tool_registry, owner="kernel")
        return registry, registry.get(SessionService)

    async def test_dangerous_tool_approved(self, service: PermissionServiceImpl, tool_registry: ToolRegistry) -> None:
        """危险工具在用户批准后被真实执行。"""
        DangerTool.calls = 0
        registry, session_service = self._build(service, tool_registry)
        session = session_service.create_session(title="confirm-ok")
        seen: list[str] = []

        async def approve(tool: str, args: dict[str, Any], risk: str, reason: str) -> bool:
            seen.append(tool)
            return True

        loop = AgentLoop(services=registry, hooks=HookManager(), tool_registry=tool_registry)
        result = await loop.run(
            session_id=session.id,
            user_message="执行代码",
            provider=ConfirmProvider("danger_tool"),
            confirm_callback=approve,
        )

        assert result.error is None
        assert DangerTool.calls == 1
        assert seen == ["danger_tool"]

    async def test_dangerous_tool_rejected(self, service: PermissionServiceImpl, tool_registry: ToolRegistry) -> None:
        """用户拒绝时工具不执行，错误信息回传给模型。"""
        DangerTool.calls = 0
        registry, session_service = self._build(service, tool_registry)
        session = session_service.create_session(title="confirm-no")

        loop = AgentLoop(services=registry, hooks=HookManager(), tool_registry=tool_registry)
        result = await loop.run(
            session_id=session.id,
            user_message="执行代码",
            provider=ConfirmProvider("danger_tool"),
            confirm_callback=lambda *_a, **_k: _reject(),
        )

        assert result.error is None
        assert DangerTool.calls == 0
        tool_msg = next(m for m in session_service.list_messages(session.id) if m.role == "tool")
        assert "拒绝" in (tool_msg.content or "")

    async def test_denied_tool_never_runs(self, service: PermissionServiceImpl, tool_registry: ToolRegistry) -> None:
        """被 deny 的工具不发起确认，直接拒绝。"""
        service.set_override("read_tool", "deny")
        ReadTool.calls = 0
        registry, session_service = self._build(service, tool_registry)
        session = session_service.create_session(title="deny")
        confirm_called = False

        async def should_not_call(*_a: Any, **_k: Any) -> bool:
            nonlocal confirm_called
            confirm_called = True
            return True

        loop = AgentLoop(services=registry, hooks=HookManager(), tool_registry=tool_registry)
        await loop.run(
            session_id=session.id,
            user_message="读一下",
            provider=ConfirmProvider("read_tool"),
            confirm_callback=should_not_call,
        )

        assert ReadTool.calls == 0
        assert confirm_called is False

    async def test_read_tool_runs_without_confirm(
        self, service: PermissionServiceImpl, tool_registry: ToolRegistry
    ) -> None:
        """只读工具在默认策略下无需确认直接执行。"""
        ReadTool.calls = 0
        registry, session_service = self._build(service, tool_registry)
        session = session_service.create_session(title="read-ok")

        async def fail_if_called(*_a: Any, **_k: Any) -> bool:
            raise AssertionError("只读工具不应触发确认")

        loop = AgentLoop(services=registry, hooks=HookManager(), tool_registry=tool_registry)
        await loop.run(
            session_id=session.id,
            user_message="读一下",
            provider=ConfirmProvider("read_tool"),
            confirm_callback=fail_if_called,
        )
        assert ReadTool.calls == 1

    async def test_confirm_required_but_no_callback(
        self, service: PermissionServiceImpl, tool_registry: ToolRegistry
    ) -> None:
        """需确认但接入层无回调时，安全默认 = 拒绝。"""
        DangerTool.calls = 0
        registry, session_service = self._build(service, tool_registry)
        session = session_service.create_session(title="no-callback")

        loop = AgentLoop(services=registry, hooks=HookManager(), tool_registry=tool_registry)
        await loop.run(
            session_id=session.id,
            user_message="执行代码",
            provider=ConfirmProvider("danger_tool"),
        )
        assert DangerTool.calls == 0

    async def test_no_permission_service_allows_all(self, tool_registry: ToolRegistry) -> None:
        """未注册权限服务时行为不变（向后兼容）。"""
        DangerTool.calls = 0
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db = Database(path)
        registry = ServiceRegistry()
        registry.register(SessionService, SessionServiceImpl(db), owner="test")
        registry.register(ContextService, ContextServiceImpl(services=registry), owner="test")
        registry.register(ToolRegistry, tool_registry, owner="kernel")
        session_service = registry.get(SessionService)
        session = session_service.create_session(title="compat")

        loop = AgentLoop(services=registry, hooks=HookManager(), tool_registry=tool_registry)
        await loop.run(
            session_id=session.id,
            user_message="执行代码",
            provider=ConfirmProvider("danger_tool"),
        )
        assert DangerTool.calls == 1
        db.close()


async def _reject() -> bool:
    """始终拒绝的确认回调。"""
    return False
