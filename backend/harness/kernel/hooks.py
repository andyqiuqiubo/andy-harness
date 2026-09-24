"""HookManager —— 钩子管线管理器。

有序管线，支持改写数据与短路。
"""

from __future__ import annotations

import logging
from typing import Any

from harness.kernel.contracts.hook import HookContext, HookHandler, HookResult

logger = logging.getLogger("harness.hooks")


class HookManager:
    """钩子管线管理器。

    - 每个钩子点维护一个有序处理器列表
    - 处理器按注册顺序依次执行，前一个的输出 data 作为下一个的输入
    - short_circuit=True 时终止管线
    - 插件停用时自动注销其注册的所有钩子
    """

    def __init__(self) -> None:
        # hook_name -> [(handler, owner), ...]
        self._hooks: dict[str, list[tuple[HookHandler, str]]] = {}

    def register(self, hook_name: str, handler: HookHandler, owner: str = "") -> None:
        """注册钩子处理器（追加到管线末尾）。"""
        if hook_name not in self._hooks:
            self._hooks[hook_name] = []
        self._hooks[hook_name].append((handler, owner))
        logger.info("钩子注册: %s <- %s (owner=%s)", hook_name, handler, owner)

    def unregister(self, hook_name: str, handler: HookHandler) -> None:
        """注销指定钩子处理器。"""
        if hook_name in self._hooks:
            self._hooks[hook_name] = [
                (h, o) for h, o in self._hooks[hook_name] if h != handler
            ]
            if not self._hooks[hook_name]:
                del self._hooks[hook_name]

    def unregister_all(self, owner: str) -> None:
        """注销某 owner 注册的所有钩子。"""
        for hook_name in list(self._hooks.keys()):
            self._hooks[hook_name] = [
                (h, o) for h, o in self._hooks[hook_name] if o != owner
            ]
            if not self._hooks[hook_name]:
                del self._hooks[hook_name]

    async def execute(self, hook_name: str, context: HookContext) -> HookResult:
        """执行钩子管线。

        处理器按顺序执行，前一个的输出 data 作为下一个的输入。
        short_circuit=True 时终止管线并返回当前结果。

        Args:
            hook_name: 钩子点名称
            context: 钩子上下文（data 会被管线改写）

        Returns:
            最后一个处理器的 HookResult（或短路时的结果）
        """
        handlers = self._hooks.get(hook_name, [])
        if not handlers:
            return HookResult(data=context.data)

        current_data: Any = context.data
        current_metadata: dict[str, Any] = dict(context.metadata)

        for handler, _owner in handlers:
            # 为每个处理器创建带有当前 data 和 metadata 的上下文
            ctx = HookContext(
                hook_name=context.hook_name,
                data=current_data,
                session_id=context.session_id,
                metadata=current_metadata,
            )
            result = await handler(ctx)

            if result.data is not None:
                current_data = result.data

            # metadata 在管线中传播
            if result.metadata:
                current_metadata.update(result.metadata)

            if result.short_circuit:
                logger.info(
                    "钩子短路: %s (error=%s)", hook_name, result.error
                )
                return result

        return HookResult(data=current_data)
