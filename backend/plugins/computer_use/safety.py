"""Computer Use 安全围栏。

目标：在不信任模型自主性的前提下，提供可用的自动化能力。三层防护：

1. 硬性拒绝（deny_commands / deny_paths）：无论何种模式，命中即拦截，
   用于防止 `rm -rf`、`format`、改写系统目录等不可逆灾难。
2. 作用域约束（文件读写）：路径必须落在 working_dir 或 allow_paths 内，
   绝不越界到用户个人文件 / 系统目录。
3. 模式化放行：
   - whitelist（默认，无人值守安全）：写/危险动作仅当命中 allow_commands /
     allow_paths 才放行，其余一律拦截（模型可换方案，不会失控）。
   - interactive（需人看守）：写/危险动作需要「整个任务(run)」先经人工授权；
     未授权前返回待审批提示，人工通过后以 resume_run_id 继续。

风险分级：read（截图/读文件）/ write（写文件、鼠标键盘操作）/ dangerous（命令执行）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import ComputerUseConfig

# 各工具的风险等级
_RISK_OF_TOOL: dict[str, str] = {
    "screenshot": "read",
    "file_read": "read",
    "file_write": "write",
    "mouse_move": "write",
    "mouse_click": "write",
    "mouse_drag": "write",
    "keyboard_type": "write",
    "keyboard_press": "write",
    "command_exec": "dangerous",
    "finish": "read",
}


@dataclass
class SafetyDecision:
    """安全决策结果。"""

    allowed: bool
    risk: str
    reason: str
    requires_approval: bool = False
    signature: str = ""


class SafetyPolicy:
    """基于配置的访问控制策略。"""

    def __init__(self, config: ComputerUseConfig) -> None:
        self._cfg = config

    @staticmethod
    def _risk_of(tool_name: str) -> str:
        return _RISK_OF_TOOL.get(tool_name, "write")

    def _signature(self, tool_name: str, args: dict[str, Any]) -> str:
        """生成动作指纹（用于审批/白名单匹配与幂等）。"""
        if tool_name == "command_exec":
            return f"cmd:{args.get('command', '')}"
        if tool_name in ("file_read", "file_write"):
            return f"file:{tool_name}:{args.get('path', '')}"
        return f"desktop:{tool_name}"

    # ── 硬性拒绝 ──────────────────────────────────────
    def _matches_deny(self, tool_name: str, args: dict[str, Any]) -> str | None:
        """命中危险命令/受限目录则返回拦截原因，否则返回 None。"""
        if tool_name == "command_exec":
            cmd = " ".join(str(args.get("command", "")).lower().split())
            for bad in self._cfg.deny_commands:
                if bad.lower() in cmd:
                    return f"命中危险命令黑名单: 包含 '{bad}'"
        if tool_name in ("file_read", "file_write"):
            path = self._normalize_path(args.get("path", ""))
            for bad in self._cfg.deny_paths:
                if path.startswith(bad):
                    return f"命中受限目录黑名单: {args.get('path', '')}"
        return None

    # ── 作用域约束（仅文件操作） ──────────────────────
    def _check_scope(self, tool_name: str, args: dict[str, Any]) -> str | None:
        """文件读写必须落在 working_dir / allow_paths 之内。"""
        if tool_name not in ("file_read", "file_write"):
            return None
        raw = args.get("path", "")
        path = self._normalize_path(raw)
        working = self._normalize_path(self._cfg.working_dir)
        if path.startswith(working):
            return None
        for allowed in self._cfg.allow_paths:
            if path.startswith(self._normalize_path(allowed)):
                return None
        return f"文件路径越界：'{raw}' 不在工作目录({self._cfg.working_dir})或 allow_paths 允许范围内"

    # ── 白名单放行（仅命令执行） ──────────────────────
    def _matches_allow(self, tool_name: str, args: dict[str, Any]) -> bool:
        if tool_name == "command_exec":
            token = str(args.get("command", "")).strip().split(" ", 1)[0].lower()
            return token in self._cfg.allow_commands
        if tool_name == "file_write":
            # 写文件只要通过作用域约束（在 working_dir 内）即视为允许
            return self._check_scope(tool_name, args) is None
        return False

    @staticmethod
    def _normalize_path(p: str) -> str:
        try:
            return str(Path(os.path.expanduser(p)).resolve()).replace("\\", "/").lower()
        except (OSError, ValueError):
            return p.replace("\\", "/").lower()

    # ── 主入口 ────────────────────────────────────────
    def evaluate(self, tool_name: str, args: dict[str, Any], run_approved: bool) -> SafetyDecision:
        """评估一个工具调用是否允许执行。

        Args:
            tool_name: 工具名
            args: 参数（已解析）
            run_approved: 当前任务(run)是否已通过人工授权（仅 interactive 模式相关）
        """
        risk = self._risk_of(tool_name)
        sig = self._signature(tool_name, args)

        # 1) 硬性拒绝优先
        deny_reason = self._matches_deny(tool_name, args)
        if deny_reason:
            return SafetyDecision(False, risk, deny_reason, signature=sig)

        # 2) 作用域约束（文件）
        scope_err = self._check_scope(tool_name, args)
        if scope_err:
            return SafetyDecision(False, risk, scope_err, signature=sig)

        # 3) 只读动作（截图 / 读文件）一律放行
        if risk == "read" and tool_name in ("screenshot", "file_read"):
            return SafetyDecision(True, risk, "只读操作，允许", signature=sig)

        # 4) 写 / 危险动作：按模式放行
        if self._cfg.approval_mode == "whitelist":
            # 桌面控制：由显式开关控制
            if tool_name in (
                "mouse_move",
                "mouse_click",
                "mouse_drag",
                "keyboard_type",
                "keyboard_press",
            ):
                if self._cfg.allow_desktop:
                    return SafetyDecision(True, risk, "白名单模式：桌面控制已开启", signature=sig)
                return SafetyDecision(False, risk, "白名单模式：桌面控制未开启(allow_desktop=false)", signature=sig)
            # 命令 / 写文件：严格白名单
            if self._matches_allow(tool_name, args):
                return SafetyDecision(True, risk, "白名单模式：命中允许项", signature=sig)
            return SafetyDecision(
                False,
                risk,
                "白名单模式：该操作不在 allow_commands / allow_paths 允许范围内，已拦截",
                signature=sig,
            )

        # interactive 模式：需要整个任务已授权
        if run_approved:
            return SafetyDecision(True, risk, "交互模式：任务已获人工授权", signature=sig)
        return SafetyDecision(
            False,
            risk,
            "交互模式：该写/危险操作需要人工授权整个任务(run)后方可执行",
            requires_approval=True,
            signature=sig,
        )
