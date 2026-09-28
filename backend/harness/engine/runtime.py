"""Agent 运行时上下文 —— 让工具在「当前这一轮」内访问运行环境。

`task`（子代理）工具需要拿到当前会话所用的 provider / model 才能派生
子代理，但 AgentLoop 的 provider 是每次 `run()` 的入参，不在服务注册表里，
也不适合塞进工具 args（args 会被序列化进 WebSocket 事件）。

这里用 `contextvars.ContextVar` 承载运行环境：AgentLoop 在 `run()` 开始时
设置、结束时重置。ContextVar 是「按异步任务隔离」的，因此不同会话/连接
的并发运行互不干扰。
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any


@dataclass
class AgentRuntime:
    """一次 AgentLoop.run() 的运行环境。"""

    provider: Any
    model: str
    session_id: str
    services: Any
    hooks: Any
    tool_registry: Any
    budget: int = 4096


_current: ContextVar[AgentRuntime | None] = ContextVar(
    "harness_agent_runtime", default=None
)


def set_runtime(rt: AgentRuntime) -> Any:
    """设置当前运行环境，返回可用于 reset 的 token。"""
    return _current.set(rt)


def reset_runtime(token: Any) -> None:
    """恢复到设置前的运行环境。"""
    try:
        _current.reset(token)
    except (ValueError, LookupError):
        # token 已被消费（重复 reset）时忽略
        pass


def get_runtime() -> AgentRuntime | None:
    """获取当前运行环境（不在 AgentLoop 内时为 None）。"""
    return _current.get()
