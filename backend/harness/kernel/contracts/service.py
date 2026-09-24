"""ServicePlugin 契约 —— 后台常驻服务。"""

from __future__ import annotations

from harness.kernel.contracts.base import BasePlugin


class ServicePlugin(BasePlugin):
    """后台常驻服务插件契约。

    与 BasePlugin 的 activate/deactivate 一致，
    service 类型插件的 activate 通常会注册一个服务接口到 ServiceRegistry。
    """
