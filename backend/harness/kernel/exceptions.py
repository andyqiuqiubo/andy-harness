"""内核标准异常体系。"""

from __future__ import annotations


class KernelError(Exception):
    """内核基础异常。"""


class PluginError(KernelError):
    """插件相关基础异常。"""


class PluginValidationError(PluginError):
    """插件清单校验失败。"""

    def __init__(self, plugin_id: str, reason: str) -> None:
        self.plugin_id = plugin_id
        self.reason = reason
        super().__init__(f"插件 [{plugin_id}] 校验失败: {reason}")


class PluginLoadError(PluginError):
    """插件加载失败。"""

    def __init__(self, plugin_id: str, reason: str) -> None:
        self.plugin_id = plugin_id
        self.reason = reason
        super().__init__(f"插件 [{plugin_id}] 加载失败: {reason}")


class PluginNotLoadedError(PluginError):
    """插件未加载。"""

    def __init__(self, plugin_id: str) -> None:
        self.plugin_id = plugin_id
        super().__init__(f"插件 [{plugin_id}] 未加载")


class PluginAlreadyLoadedError(PluginError):
    """插件已加载。"""

    def __init__(self, plugin_id: str) -> None:
        self.plugin_id = plugin_id
        super().__init__(f"插件 [{plugin_id}] 已加载")


class PluginDeactivateError(PluginError):
    """插件停用失败（如核心插件不可停用）。"""

    def __init__(self, plugin_id: str, reason: str) -> None:
        self.plugin_id = plugin_id
        self.reason = reason
        super().__init__(f"插件 [{plugin_id}] 停用失败: {reason}")


class ServiceUnavailable(KernelError):  # noqa: N818
    """请求的服务未注册或已停用。"""

    def __init__(self, service_name: str) -> None:
        self.service_name = service_name
        super().__init__(f"服务不可用: {service_name}")
