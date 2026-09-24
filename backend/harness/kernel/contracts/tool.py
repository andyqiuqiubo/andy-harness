"""ToolPlugin 契约 —— Agent 可调用工具。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class ToolPlugin(ABC):
    """Agent 可调用工具插件契约。

    工具只需实现 tool_name / description / parameters_schema / execute。
    生命周期管理（activate/deactivate）由包装此工具的插件负责。
    """

    @property
    @abstractmethod
    def tool_name(self) -> str:
        """工具名称。"""
        ...

    @property
    @abstractmethod
    def description(self) -> str:
        """工具描述。"""
        ...

    @property
    @abstractmethod
    def parameters_schema(self) -> dict[str, Any]:
        """参数 JSON Schema。"""
        ...

    @abstractmethod
    async def execute(self, args: dict[str, Any]) -> str:
        """执行工具，返回结果文本。"""
        ...
