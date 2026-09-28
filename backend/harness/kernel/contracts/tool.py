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

    @property
    def needs_session(self) -> bool:
        """是否需要当前会话 ID。

        为 True 时，AgentLoop 会在调用 execute 前把 `session_id`
        注入 args（不覆盖模型已提供的值）。供需要会话上下文的工具使用。
        """
        return False

    @property
    def risk_level(self) -> str:
        """风险等级（供权限与人工确认策略使用）。

        - `read`：只读，无副作用（搜索、查询、读取文件）
        - `write`：会改变外部状态（切换主题、写文件）
        - `dangerous`：可执行任意代码或不可逆操作（沙箱执行 shell / Python）

        默认为 `write`（对未声明的工具取较保守等级）。
        """
        return "write"
