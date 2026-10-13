"""内置示例工具 —— 计算器 + 当前时间。"""

from __future__ import annotations

import ast
import logging
import operator
from collections.abc import Callable
from datetime import datetime
from typing import Any

from harness.engine.safe_math import NodeBudget, check_expression_length, check_pow
from harness.kernel.context import PluginContext
from harness.kernel.contracts.base import BasePlugin, PluginManifest
from harness.kernel.contracts.tool import ToolPlugin

logger = logging.getLogger("harness.tools.builtin")

# 安全的运算符映射
_SAFE_OPERATORS: dict[type, Callable[..., Any]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}


class CalculatorTool(ToolPlugin):
    """计算器工具 —— 安全解析并计算数学表达式。"""

    @property
    def tool_name(self) -> str:
        return "calculator"

    @property
    def risk_level(self) -> str:
        return "read"

    @property
    def description(self) -> str:
        return "计算数学表达式，支持加减乘除、幂、取模等。例如: 123*456、(1+2)*3"

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "expression": {
                    "type": "string",
                    "description": "要计算的数学表达式",
                }
            },
            "required": ["expression"],
        }

    async def execute(self, args: dict[str, Any]) -> str:
        expression = args.get("expression", "")
        if not expression:
            return "错误: 未提供表达式"

        try:
            # SEC-01：长度 + 节点数预算 + 幂规模三重上限，防 DoS
            # （2**999999999 之类可在无代码执行面的前提下拖垮进程）
            check_expression_length(expression)
            tree = ast.parse(expression, mode="eval")
            result = self._eval_node(tree.body, NodeBudget())
            return str(result)
        except ZeroDivisionError:
            return "错误: 除零"
        except RecursionError:
            return "错误: 表达式嵌套过深"
        except Exception as e:  # noqa: BLE001
            return f"错误: 无法计算表达式 '{expression}': {e}"

    def _eval_node(self, node: ast.AST, budget: NodeBudget) -> Any:
        budget.spend()
        if isinstance(node, ast.Constant):
            # 只接受数值常量（拒绝字符串/字节等，避免意外类型进入运算）
            if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
                raise ValueError("仅支持数值常量")
            return node.value
        if isinstance(node, ast.BinOp):
            if isinstance(node.op, ast.Pow):
                # 先算操作数、真正做幂之前拦截超大结果（位级估算）
                left = self._eval_node(node.left, budget)
                right = self._eval_node(node.right, budget)
                check_pow(left, right)
                return operator.pow(left, right)
            op_func = _SAFE_OPERATORS.get(type(node.op))
            if op_func is None:
                raise ValueError(f"不支持的运算符: {type(node.op).__name__}")
            return op_func(self._eval_node(node.left, budget), self._eval_node(node.right, budget))
        if isinstance(node, ast.UnaryOp):
            op_func = _SAFE_OPERATORS.get(type(node.op))
            if op_func is None:
                raise ValueError(f"不支持的运算符: {type(node.op).__name__}")
            return op_func(self._eval_node(node.operand, budget))
        raise ValueError(f"不支持的表达式类型: {type(node).__name__}")


class CurrentTimeTool(ToolPlugin):
    """当前时间工具。"""

    @property
    def tool_name(self) -> str:
        return "current_time"

    @property
    def risk_level(self) -> str:
        return "read"

    @property
    def description(self) -> str:
        return "获取当前日期和时间。可选参数 timezone 指定时区（默认本地时区）。"

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "timezone": {
                    "type": "string",
                    "description": "时区名称（可选，默认本地时区），如 'UTC'、'Asia/Shanghai'",
                }
            },
        }

    async def execute(self, args: dict[str, Any]) -> str:
        tz_name = args.get("timezone", "")
        if tz_name:
            try:
                from zoneinfo import ZoneInfo

                tz = ZoneInfo(tz_name)
                now = datetime.now(tz)
            except Exception:
                # 不支持的时区，回退到本地时间
                now = datetime.now()
        else:
            now = datetime.now()
        return now.strftime("%Y-%m-%d %H:%M:%S")


class BuiltinToolsPlugin(BasePlugin):
    """内置工具插件 —— 注册计算器和当前时间工具。"""

    manifest: PluginManifest
    _ctx: PluginContext | None = None

    def __init__(self) -> None:
        self._ctx = None

    async def activate(self, ctx: PluginContext) -> None:
        """激活：注册工具到 ToolRegistry。"""
        self._ctx = ctx

        from harness.engine.tool_registry import ToolRegistry

        if not ctx.services.has(ToolRegistry):
            ctx.services.register(ToolRegistry, ToolRegistry(), owner=self.plugin_id)
        tool_registry = ctx.services.get(ToolRegistry)

        tool_registry.register(CalculatorTool(), owner=self.plugin_id)
        tool_registry.register(CurrentTimeTool(), owner=self.plugin_id)

        ctx.logger.info("内置工具插件已激活（calculator + current_time）")

    async def deactivate(self, ctx: PluginContext) -> None:
        """停用。"""
        from harness.engine.tool_registry import ToolRegistry

        try:
            tool_registry = ctx.services.get(ToolRegistry)
            tool_registry.unregister_all(self.plugin_id)
        except Exception:
            pass
        ctx.logger.info("内置工具插件已停用")
