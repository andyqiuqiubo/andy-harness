"""计算器插件（plugins/tool_calculator）测试 —— 含 SEC-01 DoS 防护验证。"""

from __future__ import annotations

import os
import sys

import pytest

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

from plugins.tool_calculator.main import CalculatorTool, _safe_eval  # noqa: E402


class TestCalculatorPlugin:
    @pytest.mark.asyncio
    async def test_basic_arithmetic(self) -> None:
        tool = CalculatorTool()
        result = await tool.execute({"expression": "(1+2)*3"})
        assert result == "(1+2)*3 = 9"

    @pytest.mark.asyncio
    async def test_function_call(self) -> None:
        tool = CalculatorTool()
        result = await tool.execute({"expression": "sqrt(16)+log10(100)"})
        assert result == "sqrt(16)+log10(100) = 6"

    @pytest.mark.asyncio
    async def test_factorial_reasonable(self) -> None:
        tool = CalculatorTool()
        result = await tool.execute({"expression": "factorial(10)"})
        assert result == "factorial(10) = 3628800"

    @pytest.mark.asyncio
    async def test_rejects_disallowed_name(self) -> None:
        tool = CalculatorTool()
        result = await tool.execute({"expression": "__import__('os')"})
        assert "计算失败" in result or "错误" in result

    # ── SEC-01 DoS 防护 ──────────────────────────────
    @pytest.mark.asyncio
    async def test_factorial_huge_rejected(self) -> None:
        tool = CalculatorTool()
        result = await tool.execute({"expression": "factorial(999999)"})
        assert "上限" in result

    @pytest.mark.asyncio
    async def test_factorial_of_huge_pow_rejected(self) -> None:
        """factorial(2**2000)：幂本身合法，但阶乘参数检查必须拦截。"""
        tool = CalculatorTool()
        result = await tool.execute({"expression": "factorial(2**2000)"})
        assert "上限" in result

    @pytest.mark.asyncio
    @pytest.mark.parametrize("expr", ["2**999999999", "9**9**9", "(2**4096)**4096"])
    async def test_pow_dos_rejected(self, expr: str) -> None:
        with pytest.raises(ValueError, match="上限|过大"):
            _safe_eval(expr)

    def test_long_expression_rejected(self) -> None:
        with pytest.raises(ValueError, match="过长"):
            _safe_eval("1+" * 2000 + "1")

    def test_deep_unary_chain_rejected_by_budget(self) -> None:
        # 600 层一元负号 → 601 个 AST 节点，超过 500 预算（长度 601 未超限）
        expr = "-" * 600 + "1"
        with pytest.raises(ValueError, match="复杂"):
            _safe_eval(expr)

    def test_moderate_nesting_ok(self) -> None:
        # 100 层括号不产生额外 AST 节点（纯括号非 DoS 面），应正常计算
        expr = "(" * 100 + "1+1" + ")" * 100
        assert _safe_eval(expr) == 2
