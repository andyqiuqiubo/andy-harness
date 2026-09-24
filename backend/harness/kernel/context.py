"""PluginContext —— 注入每个插件的受限门面。

插件拿到的不是整个内核，而是一个受限上下文。
"""

from __future__ import annotations

import logging
from typing import Any

from harness.kernel.eventbus import EventBus
from harness.kernel.hooks import HookManager
from harness.kernel.services import ServiceRegistry


class PluginConfig:
    """插件配置（schema 校验后）。"""

    def __init__(self, data: dict[str, Any] | None = None) -> None:
        self._data: dict[str, Any] = data or {}

    def get(self, key: str, default: Any = None) -> Any:
        """获取配置项。"""
        return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        """设置配置项。"""
        self._data[key] = value

    def as_dict(self) -> dict[str, Any]:
        """返回完整配置字典。"""
        return dict(self._data)


class SecretStore:
    """密钥存储（仅自己命名空间）。"""

    def __init__(self, namespace: str) -> None:
        self._namespace = namespace
        self._secrets: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        """获取密钥。"""
        return self._secrets.get(key)

    def set(self, key: str, value: str) -> None:
        """设置密钥。"""
        self._secrets[key] = value

    def delete(self, key: str) -> None:
        """删除密钥。"""
        self._secrets.pop(key, None)


class PluginStorage:
    """插件存储（仅自己命名空间的 KV）。"""

    def __init__(self, namespace: str) -> None:
        self._namespace = namespace
        self._kv: dict[str, Any] = {}

    def get(self, key: str, default: Any = None) -> Any:
        """获取值。"""
        return self._kv.get(key, default)

    def set(self, key: str, value: Any) -> None:
        """设置值。"""
        self._kv[key] = value

    def delete(self, key: str) -> None:
        """删除值。"""
        self._kv.pop(key, None)

    def keys(self) -> list[str]:
        """返回所有键。"""
        return list(self._kv.keys())


class PluginContext:
    """注入每个插件的受限门面。

    插件通过此上下文访问内核能力，而非直接访问内核。
    """

    def __init__(
        self,
        plugin_id: str,
        config: PluginConfig | None = None,
        events: EventBus | None = None,
        services: ServiceRegistry | None = None,
        hooks: HookManager | None = None,
    ) -> None:
        self.plugin_id: str = plugin_id
        self.logger: logging.Logger = logging.getLogger(f"harness.plugin.{plugin_id}")
        self.config: PluginConfig = config or PluginConfig()
        self.secrets: SecretStore = SecretStore(plugin_id)
        self.storage: PluginStorage = PluginStorage(plugin_id)
        self.events: EventBus = events or EventBus()
        self.services: ServiceRegistry = services or ServiceRegistry()
        self.hooks: HookManager = hooks or HookManager()
