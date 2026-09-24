"""工具注册表 —— 管理所有已注册的工具插件。"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("harness.tool_registry")


class ToolNotFoundError(Exception):
    """工具未找到。"""


class ToolRegistry:
    """工具注册表。

    管理 tool_name -> tool 映射，支持按名查找和列出。
    工具插件 activate 时调用 register()，deactivate 时调用 unregister()。
    """

    def __init__(self) -> None:
        # tool_name -> (tool_instance, owner)
        self._tools: dict[str, tuple[Any, str]] = {}

    def register(self, tool: Any, owner: str = "") -> None:
        """注册工具。

        Args:
            tool: 工具实例（需有 tool_name / description / parameters_schema / execute）
            owner: 注册者标识
        """
        name = getattr(tool, "tool_name", "")
        if not name:
            raise ValueError("工具缺少 tool_name 属性")
        self._tools[name] = (tool, owner)
        logger.info("工具已注册: %s (owner=%s)", name, owner)

    def unregister(self, tool_name: str) -> None:
        """注销工具。"""
        self._tools.pop(tool_name, None)
        logger.info("工具已注销: %s", tool_name)

    def unregister_all(self, owner: str) -> None:
        """注销某 owner 注册的所有工具。"""
        for name in list(self._tools.keys()):
            if self._tools[name][1] == owner:
                self._tools.pop(name)
                logger.info("工具已注销: %s (owner=%s)", name, owner)

    def get(self, tool_name: str) -> Any:
        """获取工具实例。

        Raises:
            ToolNotFoundError: 工具未注册
        """
        entry = self._tools.get(tool_name)
        if entry is None:
            raise ToolNotFoundError(f"工具未找到: {tool_name}")
        return entry[0]

    def list_tools(self) -> list[dict[str, Any]]:
        """列出所有已注册工具。"""
        result: list[dict[str, Any]] = []
        for name, (tool, owner) in self._tools.items():
            result.append(
                {
                    "name": name,
                    "description": getattr(tool, "description", ""),
                    "parameters_schema": getattr(tool, "parameters_schema", {}),
                    "owner": owner,
                }
            )
        return result

    def has(self, tool_name: str) -> bool:
        """检查工具是否已注册。"""
        return tool_name in self._tools

    def get_tool_definitions(self) -> list[dict[str, Any]]:
        """获取工具定义列表（用于传递给模型的 function calling）。"""
        result: list[dict[str, Any]] = []
        for name, (tool, _owner) in self._tools.items():
            params = getattr(tool, "parameters_schema", {})
            result.append(
                {
                    "type": "function",
                    "function": {
                        "name": name,
                        "description": getattr(tool, "description", ""),
                        "parameters": params,
                    },
                }
            )
        return result
