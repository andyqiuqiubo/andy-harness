"""钩子契约 —— HookContext / HookResult / HookPlugin + HookHandler 类型。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from harness.kernel.contracts.base import BasePlugin

if TYPE_CHECKING:
    pass

# 钩子处理器类型：接收 HookContext，返回 HookResult
HookHandler = Callable[["HookContext"], Awaitable["HookResult"]]


@dataclass
class HookContext:
    """钩子处理器接收的上下文。

    每个钩子点填充不同字段，data 可被钩子改写。
    """

    hook_name: str
    data: Any = None
    session_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class HookResult:
    """钩子处理器返回值。

    - data: 改写后的数据（None 表示不修改）
    - short_circuit: 是否短路（跳过后续钩子与原始操作）
    - error: 错误信息（短路时可附带）
    - metadata: 附加元数据（会在管线中传播合并）
    """

    data: Any | None = None
    short_circuit: bool = False
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class HookPlugin(BasePlugin):
    """钩子插件契约（与 BasePlugin 一致，activate 时注册钩子处理器）。"""
