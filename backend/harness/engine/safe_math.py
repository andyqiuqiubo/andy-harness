"""数学表达式求值的公共规模上限（SEC-01 修复：计算器 DoS 防护）。

背景：计算器工具用 ast 白名单求值，虽无代码执行面，但 **ast.Pow 无规模
上限**——``2**999999999`` / ``9**9**9`` 会让 Python 花数分钟算一个千万位
大整数并吃掉数 GB 内存；``factorial(999999)`` 同理；超长表达式还会拖慢
``ast.parse`` 本身。这些都是可由 LLM 输出（或用户输入）轻易触发的 DoS。

本模块把三处计算器（``engine/builtin_tools``、``plugins/tool_calculator``、
``marketplace/tool_calculator``）的防护收敛到同一组上限：

1. 表达式长度上限（parse 前拦截）；
2. AST 节点数预算（防超长嵌套 / 超宽链式运算）；
3. 幂运算结果位级估算（真正计算前拦截会产生超大中间值的幂）；
4. 阶乘参数上限。
"""

from __future__ import annotations

from typing import Any

# 表达式字符数上限（对人类足够宽松：1000 字符能写非常复杂的公式）
MAX_EXPR_LEN = 1000
# AST 节点数上限（防 50 层嵌套括号 / 超长链式运算）
MAX_NODES = 500
# 幂运算结果比特数上限（100_000 位 ≈ 3 万位十进制，str() 与返回都很快）
MAX_POW_RESULT_BITS = 100_000
# 幂运算指数绝对值上限
MAX_ABS_EXPONENT = 4096
# 阶乘参数上限（5000! 约 16 万位，可秒算；更大则拒绝）
MAX_FACTORIAL_ARG = 5000


def check_expression_length(expr: str) -> None:
    """parse 前拦截超长表达式（同时也是 parse 自身的 DoS 防护）。"""
    if len(expr) > MAX_EXPR_LEN:
        raise ValueError(f"表达式过长（上限 {MAX_EXPR_LEN} 字符）")


class NodeBudget:
    """AST 节点数预算：求值递归中逐节点扣减，超支即抛错。"""

    __slots__ = ("remaining",)

    def __init__(self, limit: int = MAX_NODES) -> None:
        self.remaining = limit

    def spend(self) -> None:
        self.remaining -= 1
        if self.remaining < 0:
            raise ValueError(f"表达式过于复杂（上限 {MAX_NODES} 个节点）")


def _is_plain_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def check_pow(base: Any, exponent: Any) -> None:
    """幂运算规模检查（在调用 ``**`` 之前执行）。

    对 int 底数做结果位级估算：``bits(base) * exponent``；对 float 指数
    限制绝对值。真正的计算发生在检查通过之后。
    """
    if _is_plain_int(exponent):
        if abs(exponent) > MAX_ABS_EXPONENT:
            raise ValueError(f"幂运算指数超过上限（|指数| ≤ {MAX_ABS_EXPONENT}）")
        if exponent >= 0 and _is_plain_int(base) and base not in (0, 1, -1):
            estimated_bits = exponent * abs(base).bit_length()
            if estimated_bits > MAX_POW_RESULT_BITS:
                raise ValueError(f"幂运算结果过大（上限约 {MAX_POW_RESULT_BITS} 位）")
    elif isinstance(exponent, float):
        if abs(exponent) > MAX_ABS_EXPONENT:
            raise ValueError(f"幂运算指数超过上限（|指数| ≤ {MAX_ABS_EXPONENT}）")


def check_factorial_arg(n: Any) -> None:
    """阶乘参数上限（``math.factorial`` 大参数会长时间占用 CPU 与内存）。"""
    if _is_plain_int(n) and n > MAX_FACTORIAL_ARG:
        raise ValueError(f"阶乘参数超过上限（≤ {MAX_FACTORIAL_ARG}）")
