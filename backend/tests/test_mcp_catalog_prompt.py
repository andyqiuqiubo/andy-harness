"""MCP 能力清单（系统提示）渲染的单元测试。

背景：MCP 远端工具只以 function 定义注入时，模型往往更倾向用熟悉的
内置工具（如 web_fetch）硬凑。系统提示里显式点名外部能力后命中率显著提升。
"""

from __future__ import annotations

from harness.engine.agent_loop import AgentLoop
from harness.engine.tool_registry import ToolRegistry
from harness.kernel.hooks import HookManager
from harness.kernel.services import ServiceRegistry


class _FakeTool:
    """最小工具桩：仅需 tool_name / description / parameters_schema。"""

    def __init__(self, name: str) -> None:
        self.tool_name = name
        self.description = "desc"
        self.parameters_schema = {"type": "object", "properties": {}}


def _loop_with(*names: str) -> AgentLoop:
    registry = ToolRegistry()
    for name in names:
        registry.register(_FakeTool(name), owner="test")
    return AgentLoop(services=ServiceRegistry(), hooks=HookManager(), tool_registry=registry)


def test_returns_empty_without_mcp_tools() -> None:
    """没有 MCP 工具时必须返回空串（行为与改动前一致）。"""
    assert _loop_with("web_fetch", "code_runner")._render_mcp_catalog() == ""


def test_lists_servers_and_tools() -> None:
    """有 MCP 工具时按 server 分组点名，且不含普通内置工具。"""
    out = _loop_with(
        "web_fetch",
        "mcp__deepwiki__read_wiki_structure",
        "mcp__deepwiki__ask_wiki_question",
        "mcp__libgen__search",
    )._render_mcp_catalog()

    assert "deepwiki" in out
    assert "read_wiki_structure" in out
    assert "ask_wiki_question" in out
    assert "libgen" in out
    assert "优先直接调用" in out
    assert "web_fetch" not in out


def test_tolerates_registry_failure() -> None:
    """注册表异常时降级返回空串，不应影响主对话流程。"""

    class _BoomRegistry:
        def list_tools(self) -> list[dict]:  # type: ignore[no-untyped-def]
            raise RuntimeError("boom")

    loop = AgentLoop(services=ServiceRegistry(), hooks=HookManager(), tool_registry=_BoomRegistry())
    assert loop._render_mcp_catalog() == ""
