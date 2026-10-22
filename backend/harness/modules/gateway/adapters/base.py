"""适配器注册表 —— 提供方注册机制（代码级）。

与 config 的 Channel 配合：Channel.adapter 引用这里注册的名字；未注册的名字
在启动/热更新时拒绝（fail-fast）。
"""

from __future__ import annotations

from harness.modules.gateway.contracts import ProviderAdapter


class AdapterRegistry:
    """适配器名 → 实例。后注册覆盖（便于测试与热替换）。"""

    def __init__(self) -> None:
        self._adapters: dict[str, ProviderAdapter] = {}

    def register(self, adapter: ProviderAdapter) -> ProviderAdapter:
        self._adapters[adapter.name] = adapter
        return adapter

    def get(self, name: str) -> ProviderAdapter:
        try:
            return self._adapters[name]
        except KeyError as e:
            raise KeyError(f"未注册的适配器: {name}") from e

    def names(self) -> list[str]:
        return sorted(self._adapters)

