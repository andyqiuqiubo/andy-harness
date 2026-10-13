"""权限判定网关（CORE-01）。

把原本内嵌在 ``agent_loop`` 里的权限判定抽到独立层，并修正 fail-open 缺陷：

- 权限服务未装配（未加载 permission 插件）时返回 ``None`` → 视为放行，
  保证「没有权限插件时行为不变」（与历史一致）；
- 权限服务已装配、但判定过程抛异常时：
    * 高危工具（shell / code / file 等系统级操作）→ 返回合成的拒绝决策
      （**fail-closed**），避免权限服务抖动时静默放行高危操作；
    * 低危工具 → 返回 ``None`` 放行，但记 error 日志，避免权限服务抖动
      导致整个 agent 自 DoS。

下游（``agent_loop``）对 ``DeniedDecision`` 与真实决策对象一视同仁：
``denied`` 为真即阻断执行并落审计。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("harness.engine.permission_gate")

# 高危工具：权限服务异常时 fail-closed（拒绝），防止静默放行系统级操作。
HIGH_RISK_TOOLS: frozenset[str] = frozenset(
    {
        "shell_execute",
        "command_exec",
        "code_execute",
        "os_exec",
        "execute_command",
        "run_script",
        "write_file",
        "file_write",
        "delete_file",
        "read_file",
        "mcp_call",
        "install_plugin",
        "uninstall_plugin",
    }
)


@dataclass
class DeniedDecision:
    """权限服务异常时为高危工具合成的「拒绝」决策。

    属性与真实决策对象保持一致，使 ``agent_loop`` 的下游消费代码无需改动。
    """

    tool_name: str
    risk: str
    action: str = "deny"
    reason: str = "权限服务异常，按 fail-closed 拒绝高危工具"
    source: str = "fail-closed"
    denied: bool = True
    needs_confirm: bool = False


def check_tool_permission(services: Any, tool_name: str) -> Any | None:
    """查询权限服务决策。

    返回：
    - 真实决策对象（含 ``denied`` / ``needs_confirm`` 等字段）；
    - ``DeniedDecision``：权限服务异常且为高危工具（fail-closed）；
    - ``None``：无权限插件（放行）或低危工具在异常下放行。
    """
    try:
        from harness.modules.permission_manager.service import PermissionService
    except Exception:  # noqa: BLE001
        return None
    if not services.has(PermissionService):
        return None
    try:
        service: Any = services.get(PermissionService)
        return service.decide(tool_name)
    except Exception as e:  # noqa: BLE001
        if tool_name in HIGH_RISK_TOOLS:
            logger.error(
                "权限服务异常且工具 %s 属高危，按 deny 处理: %s", tool_name, e
            )
            return DeniedDecision(tool_name=tool_name, risk="high")
        logger.error("权限服务异常（低危工具 %s 已放行）: %s", tool_name, e)
        return None
