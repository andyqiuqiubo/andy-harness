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


class TestCalculatorDosGuards:
    """SEC-01 修复验证：计算器 DoS 防护（幂规模 / 节点预算 / 长度上限）。"""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "expr",
        [
            "2**999999999",  # 超大指数
            "9**9**9",  # 右结合链式超大幂
            "(2**4096)**4096",  # 嵌套幂累积出超大中间结果
            "2**100000",  # 指数超绝对值上限
        ],
    )
    async def test_pow_dos_rejected(self, expr: str) -> None:
        tool = CalculatorTool()
        result = await tool.execute({"expression": expr})
        assert result.startswith("错误")
        assert "上限" in result or "过大" in result

    @pytest.mark.asyncio
    async def test_reasonable_pow_still_works(self) -> None:
        """合理幂运算不受影响（2**64 等常见计算）。"""
        tool = CalculatorTool()
        result = await tool.execute({"expression": "2**64"})
        assert result == "18446744073709551616"

    @pytest.mark.asyncio
    async def test_too_many_nodes_rejected(self) -> None:
        """超宽链式运算（999 字符 / 999 节点，长度内但超 500 节点预算）被拒。"""
        tool = CalculatorTool()
        expr = "1+" * 499 + "1"  # 999 字符，999 个 AST 节点
        result = await tool.execute({"expression": expr})
        assert "错误" in result and "复杂" in result

    @pytest.mark.asyncio
    async def test_too_long_expression_rejected(self) -> None:
        tool = CalculatorTool()
        result = await tool.execute({"expression": "1+" * 2000 + "1"})
        assert "错误" in result and "过长" in result

    @pytest.mark.asyncio
    async def test_string_constant_rejected(self) -> None:
        """字符串常量不得进入运算（与 tool_calculator 行为对齐）。"""
        tool = CalculatorTool()
        result = await tool.execute({"expression": "'abc'"})
        assert "错误" in result


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
