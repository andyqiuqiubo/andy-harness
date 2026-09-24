"""TokenCounter 契约 —— 跨 P2/P3 共享的 token 计数接口。"""

from __future__ import annotations

from abc import ABC, abstractmethod


class TokenCounter(ABC):
    """Token 计数器抽象接口。

    P2 各 provider 实现此接口并注册到 ServiceRegistry；
    P3 context-manager 通过 services.get(TokenCounter) 调用。
    """

    @abstractmethod
    def count_tokens(self, text: str, model: str) -> int:
        """计算文本在指定模型下的 token 数。

        Args:
            text: 待计数的文本
            model: 模型名称（如 "deepseek-chat"）

        Returns:
            token 数量（估算值）
        """
        ...
