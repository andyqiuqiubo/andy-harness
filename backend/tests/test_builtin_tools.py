"""内置工具测试（计算器 + 当前时间）。"""

import pytest

from harness.engine.builtin_tools import CalculatorTool, CurrentTimeTool


class TestCalculatorTool:
    """计算器工具测试。"""

    @pytest.mark.asyncio
    async def test_addition(self) -> None:
        """加法。"""
        tool = CalculatorTool()
        result = await tool.execute({"expression": "1 + 2"})
        assert result == "3"

    @pytest.mark.asyncio
    async def test_multiplication(self) -> None:
        """乘法（DoD 场景: 123*456）。"""
        tool = CalculatorTool()
        result = await tool.execute({"expression": "123*456"})
        assert result == "56088"

    @pytest.mark.asyncio
    async def test_complex_expression(self) -> None:
        """复杂表达式。"""
        tool = CalculatorTool()
        result = await tool.execute({"expression": "(1+2)*3"})
        assert result == "9"

    @pytest.mark.asyncio
    async def test_power(self) -> None:
        """幂运算。"""
        tool = CalculatorTool()
        result = await tool.execute({"expression": "2**10"})
        assert result == "1024"

    @pytest.mark.asyncio
    async def test_division_by_zero(self) -> None:
        """除零返回错误。"""
        tool = CalculatorTool()
        result = await tool.execute({"expression": "1/0"})
        assert "错误" in result

    @pytest.mark.asyncio
    async def test_invalid_expression(self) -> None:
        """无效表达式返回错误。"""
        tool = CalculatorTool()
        result = await tool.execute({"expression": "abc"})
        assert "错误" in result

    @pytest.mark.asyncio
    async def test_empty_expression(self) -> None:
        """空表达式返回错误。"""
        tool = CalculatorTool()
        result = await tool.execute({})
        assert "错误" in result

    def test_tool_name(self) -> None:
        """工具名称。"""
        assert CalculatorTool().tool_name == "calculator"

    def test_has_parameters_schema(self) -> None:
        """有参数 schema。"""
        schema = CalculatorTool().parameters_schema
        assert "expression" in schema["properties"]


class TestCurrentTimeTool:
    """当前时间工具测试。"""

    @pytest.mark.asyncio
    async def test_returns_time(self) -> None:
        """返回当前时间。"""
        tool = CurrentTimeTool()
        result = await tool.execute({})
        # 应包含日期格式的字符串
        assert len(result) > 10
        assert "-" in result  # 日期分隔符
        assert ":" in result  # 时间分隔符

    @pytest.mark.asyncio
    async def test_with_timezone_arg(self) -> None:
        """带时区参数（不报错即可）。"""
        tool = CurrentTimeTool()
        result = await tool.execute({"timezone": "Asia/Shanghai"})
        assert len(result) > 0

    def test_tool_name(self) -> None:
        """工具名称。"""
        assert CurrentTimeTool().tool_name == "current_time"
