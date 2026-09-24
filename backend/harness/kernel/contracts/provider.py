"""ModelProviderPlugin 契约 —— 大模型接入。"""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import AsyncIterator
from typing import Any

from harness.kernel.contracts.base import BasePlugin


class ModelProviderPlugin(BasePlugin):
    """大模型 Provider 插件契约。"""

    @abstractmethod
    async def chat(
        self,
        messages: list[dict[str, str]],
        model: str,
        stream: bool = True,
        **kwargs: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        """流式对话。

        Args:
            messages: 消息列表 [{"role": "user", "content": "..."}]
            model: 模型名称
            stream: 是否流式输出
            **kwargs: 额外参数（temperature 等）

        Yields:
            流式响应块 [{"delta": "...", "tool_calls": [...]}]
        """
        ...

    @abstractmethod
    async def list_models(self) -> list[str]:
        """返回可用模型列表。"""
        ...

    @abstractmethod
    def count_tokens(self, text: str, model: str) -> int:
        """计算 token 数（实现 TokenCounter 契约）。"""
        ...

    @abstractmethod
    async def health_check(self) -> bool:
        """健康检查（测试连接）。"""
        ...
