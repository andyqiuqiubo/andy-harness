"""可选通知出口（§9.5.3 系统 Toast）。

原则（§9.5.4）：提示永不阻塞主流程——所有投递异步 + 短超时 + 失败静默。
桌面横幅/胶囊由 recorder 子进程（overlay）自治呈现，不经过这里；
本模块只做服务侧的 Windows 通知（Toast via PowerShell）。
"""

from __future__ import annotations

import logging
import subprocess
import sys

logger = logging.getLogger("harness.notify")


class NotifyService:
    """best-effort 系统通知。无图形会话/受限环境静默失败。"""

    def __init__(self, app_id: str = "andy-harness") -> None:
        self.app_id = app_id

    def toast(self, title: str, body: str = "", *, timeout_s: float = 2.0) -> bool:
        if sys.platform != "win32":
            return False
        try:
            # PowerShell Toast（本地执行，不联网；5.1 兼容写法）
            script = (
                "[Windows.UI.Notifications.ToastNotificationManager,"
                " Windows.UI.Notifications, ContentType=WindowsRuntime] | Out-Null;"
                "$t=[Windows.UI.Notifications.ToastTemplateType]::ToastText02;"
                "$x=[Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent($t);"
                "$n=$x.GetElementsByTagName('text');"
                f"$n.Item(0).AppendChild($x.CreateTextNode({psq(title)})) | Out-Null;"
                f"$n.Item(1).AppendChild($x.CreateTextNode({psq(body)})) | Out-Null;"
                "$tn=[Windows.UI.Notifications.ToastNotification]::new($x);"
                "[Windows.UI.Notifications.ToastNotificationManager]"
                f"::CreateToastNotifier({psq(self.app_id)}).Show($tn);"
            )
            r = subprocess.run(  # noqa: S603,S607
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                capture_output=True, timeout=timeout_s, text=True,
            )
            return r.returncode == 0
        except Exception as e:  # noqa: BLE001
            logger.debug("toast 失败（静默）: %s", e)
            return False


def psq(s: str) -> str:
    """PowerShell 单引号字面量转义。"""
    return "'" + str(s).replace("'", "''") + "'"
