"""ServiceRegistry —— 服务注册表。

按抽象接口注册/解析服务实现，支持后注册覆盖策略。
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from harness.kernel.exceptions import ServiceUnavailable

logger = logging.getLogger("harness.services")


class ServiceRegistry:
    """服务注册表。

    - 按接口类型（Type 或 str）注册/解析服务实现
    - 后注册覆盖先注册：每个接口维护一个实现栈
    - deactivate 时 pop 栈顶，自动回退到上一个实现
    - 未注册时抛 ServiceUnavailable
    - 线程安全（使用 RLock）
    """

    def __init__(self) -> None:
        # 接口 -> 实现栈（栈顶为当前活跃实现）
        self._stacks: dict[Any, list[tuple[Any, str]]] = {}
        self._lock = threading.RLock()
        # 接口 -> 当前活跃实现
        # owner 记录: (implementation, owner_id)

    def register(self, interface: Any, implementation: Any, owner: str = "") -> None:
        """注册服务实现（压栈，成为当前活跃实现）。

        Args:
            interface: 服务接口（Type 对象或字符串名称）
            implementation: 服务实现实例
            owner: 注册者标识（通常为插件 id），用于批量注销
        """
        with self._lock:
            if interface not in self._stacks:
                self._stacks[interface] = []
            self._stacks[interface].append((implementation, owner))
            logger.info(
                "服务注册: %s <- %s (owner=%s), 栈深度=%d",
                self._interface_name(interface),
                implementation,
                owner,
                len(self._stacks[interface]),
            )

    def unregister(self, interface: Any, owner: str = "") -> None:
        """注销某 owner 在该接口上的实现（从栈中移除）。

        如果移除的是栈顶，则自动回退到前一个实现。
        """
        with self._lock:
            if interface not in self._stacks:
                return

            stack = self._stacks[interface]
            # 移除该 owner 的所有实现
            self._stacks[interface] = [
                (impl, own) for impl, own in stack if own != owner
            ]

            if not self._stacks[interface]:
                del self._stacks[interface]
                logger.info("服务注销: %s (无剩余实现)", self._interface_name(interface))
            else:
                logger.info(
                    "服务注销: %s (owner=%s), 回退到 %s",
                    self._interface_name(interface),
                    owner,
                    self._stacks[interface][-1][0],
                )

    def unregister_all(self, owner: str) -> None:
        """注销某 owner 注册的所有服务。"""
        with self._lock:
            for interface in list(self._stacks.keys()):
                self.unregister(interface, owner)

    def get(self, interface: Any) -> Any:
        """获取当前活跃的服务实现。

        Raises:
            ServiceUnavailable: 服务未注册
        """
        with self._lock:
            stack = self._stacks.get(interface)
            if not stack:
                raise ServiceUnavailable(self._interface_name(interface))
            return stack[-1][0]

    def has(self, interface: Any) -> bool:
        """检查服务是否已注册。"""
        with self._lock:
            return bool(self._stacks.get(interface))

    @staticmethod
    def _interface_name(interface: Any) -> str:
        """获取接口的显示名称。"""
        if isinstance(interface, type):
            return interface.__name__
        return str(interface)
