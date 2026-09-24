"""钩子点 data 类型定义。

基于 P1 的 HookContext / HookResult 基础结构，定义各钩子点的具体 data 类型。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class BuildContext:
    """pre_context_build / post_context_build 的 data 类型。"""

    session_id: str
    budget: int = 4096
    model: str = "gpt-4o"
    messages: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class ModelRequest:
    """pre_model_call / post_model_call 的 data 类型。"""

    messages: list[dict[str, Any]] = field(default_factory=list)
    model: str = "gpt-4o"
    params: dict[str, Any] = field(default_factory=dict)
    response: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class ToolCallContext:
    """pre_tool_call / post_tool_call 的 data 类型。"""

    tool_name: str
    args: dict[str, Any] = field(default_factory=dict)
    result: str = ""
    error: str | None = None


@dataclass
class MessageRecord:
    """pre_message_persist 的 data 类型。"""

    session_id: str
    role: str
    content: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    tool_call_id: str | None = None
    tokens: int = 0
    latency_ms: int | None = None
