"""计算器工具插件 —— 安全数学表达式计算（基于 ast 白名单）。"""

from __future__ import annotations

import ast
import logging
import math
from typing import Any

from harness.kernel.context import PluginContext
from harness.kernel.contracts.base import BasePlugin, PluginManifest
from harness.kernel.contracts.tool import ToolPlugin

logger = logging.getLogger("harness.tools.calculator")

# 允许在数学表达式中出现的名称（函数 / 常量）
_ALLOWED_NAMES: dict[str, Any] = {
    "sqrt": math.sqrt,
    "abs": abs,
    "round": round,
    "floor": math.floor,
    "ceil": math.ceil,
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    "log": math.log,
    "log10": math.log10,
    "log2": math.log2,
    "exp": math.exp,
    "pow": math.pow,
    "factorial": math.factorial,
    "pi": math.pi,
    "e": math.e,
    "tau": math.tau,
}


def _safe_eval(expr: str) -> Any:
    """用 ast 白名单求值数学表达式，禁止任意代码执行。"""
    tree = ast.parse(expr, mode="eval")

    def _eval(node: ast.AST) -> Any:
        if isinstance(node, ast.Expression):
            return _eval(node.body)
        if isinstance(node, ast.Constant):
            if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
                raise ValueError("仅支持数值常量")
            return node.value
        if isinstance(node, ast.BinOp):
            left = _eval(node.left)
            right = _eval(node.right)
            op = node.op
            if isinstance(op, ast.Add):
                return left + right
            if isinstance(op, ast.Sub):
                return left - right
            if isinstance(op, ast.Mult):
                return left * right
            if isinstance(op, ast.Div):
                return left / right
            if isinstance(op, ast.FloorDiv):
                return left // right
            if isinstance(op, ast.Mod):
                return left % right
            if isinstance(op, ast.Pow):
                return left**right
            raise ValueError("不支持的运算符")
        if isinstance(node, ast.UnaryOp):
            value = _eval(node.operand)
            if isinstance(node.op, ast.UAdd):
                return +value
            if isinstance(node.op, ast.USub):
                return -value
            raise ValueError("不支持的一元运算")
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in _ALLOWED_NAMES:
                raise ValueError("不允许的函数调用")
            args = [_eval(a) for a in node.args]
            return _ALLOWED_NAMES[node.func.id](*args)
        if isinstance(node, ast.Name):
            if node.id in _ALLOWED_NAMES:
                return _ALLOWED_NAMES[node.id]
            raise ValueError(f"未知的标识符: {node.id}")
        raise ValueError("不支持的表达式")

    return _eval(tree)


class CalculatorTool(ToolPlugin):
    """安全计算器工具。"""

    @property
    def tool_name(self) -> str:
        return "calculator"

    @property
    def risk_level(self) -> str:
        return "read"

    @property
    def description(self) -> str:
        return (
            "安全计算数学表达式。支持 + - * / **(幂) //(整除) %(取模)、括号，以及函数 "
            "sqrt / abs / round / floor / ceil / sin / cos / tan / log / log10 / log2 / "
            "exp / pow / factorial，常量 pi / e / tau。当用户需要算数、公式求值、"
            "数值计算步骤时使用。注意：该工具只接受表达式字符串，不是搜索引擎。"
        )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "expression": {
                    "type": "string",
                    "description": "要计算的数学表达式，例如 (1+2)*3 或 sqrt(16)+log10(100)",
                },
            },
            "required": ["expression"],
        }

    async def execute(self, args: dict[str, Any]) -> str:
        expr = (args.get("expression") or "").strip()
        if not expr:
            return "错误：expression 不能为空"
        try:
            result = _safe_eval(expr)
            if isinstance(result, float) and result.is_integer():
                result = int(result)
            return f"{expr} = {result}"
        except ZeroDivisionError:
            return "错误：除以零"
        except Exception as e:  # noqa: BLE001
            return f"计算失败: {e}"


class CalculatorPlugin(BasePlugin):
    """计算器插件。"""

    manifest: PluginManifest
    _ctx: PluginContext | None = None

    def __init__(self) -> None:
        self._ctx = None

    async def activate(self, ctx: PluginContext) -> None:
        self._ctx = ctx
        from harness.engine.tool_registry import ToolRegistry

        if not ctx.services.has(ToolRegistry):
            ctx.services.register(ToolRegistry, ToolRegistry(), owner=self.plugin_id)
        ctx.services.get(ToolRegistry).register(CalculatorTool(), owner=self.plugin_id)
        ctx.logger.info("Calculator 工具已注册")

    async def deactivate(self, ctx: PluginContext) -> None:
        from harness.engine.tool_registry import ToolRegistry

        try:
            ctx.services.get(ToolRegistry).unregister_all(self.plugin_id)
        except Exception:  # noqa: BLE001
            pass
