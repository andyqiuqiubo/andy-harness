"""工具注册表测试。"""

import pytest

from harness.engine.tool_registry import ToolNotFoundError, ToolRegistry


class FakeTool:
    """测试用工具。"""

    @property
    def tool_name(self) -> str:
        """工具名称。"""
        return "fake_tool"

    @property
    def description(self) -> str:
        """工具描述。"""
        return "A fake tool for testing"

    @property
    def parameters_schema(self) -> dict:
        """参数 schema。"""
        return {"type": "object", "properties": {}}

    async def execute(self, args: dict) -> str:
        """执行。"""
        return "fake result"


class AnotherTool:
    """另一个测试工具。"""

    @property
    def tool_name(self) -> str:
        """工具名称。"""
        return "another_tool"

    @property
    def description(self) -> str:
        """工具描述。"""
        return "Another tool"

    @property
    def parameters_schema(self) -> dict:
        """参数 schema。"""
        return {"type": "object", "properties": {}}

    async def execute(self, args: dict) -> str:
        """执行。"""
        return "another result"


def test_register_and_get() -> None:
    """注册并获取工具。"""
    registry = ToolRegistry()
    tool = FakeTool()
    registry.register(tool, owner="test")

    assert registry.has("fake_tool")
    retrieved = registry.get("fake_tool")
    assert retrieved is tool


def test_get_not_found() -> None:
    """获取未注册工具抛出异常。"""
    registry = ToolRegistry()
    with pytest.raises(ToolNotFoundError):
        registry.get("nonexistent")


def test_unregister() -> None:
    """注销工具。"""
    registry = ToolRegistry()
    registry.register(FakeTool(), owner="test")
    assert registry.has("fake_tool")

    registry.unregister("fake_tool")
    assert not registry.has("fake_tool")


def test_unregister_all_by_owner() -> None:
    """按 owner 批量注销。"""
    registry = ToolRegistry()
    registry.register(FakeTool(), owner="plugin-a")
    registry.register(AnotherTool(), owner="plugin-a")

    registry.unregister_all("plugin-a")
    assert not registry.has("fake_tool")
    assert not registry.has("another_tool")


def test_list_tools() -> None:
    """列出所有工具。"""
    registry = ToolRegistry()
    registry.register(FakeTool(), owner="test")
    registry.register(AnotherTool(), owner="test")

    tools = registry.list_tools()
    assert len(tools) == 2
    names = [t["name"] for t in tools]
    assert "fake_tool" in names
    assert "another_tool" in names


def test_get_tool_definitions() -> None:
    """获取工具定义列表（function calling 格式）。"""
    registry = ToolRegistry()
    registry.register(FakeTool(), owner="test")

    defs = registry.get_tool_definitions()
    assert len(defs) == 1
    assert defs[0]["type"] == "function"
    assert defs[0]["function"]["name"] == "fake_tool"
    assert defs[0]["function"]["description"] == "A fake tool for testing"
