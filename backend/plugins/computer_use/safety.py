"""Computer Use 安全围栏。

目标：在不信任模型自主性的前提下，提供可用的自动化能力。三层防护：

1. 硬性拒绝（deny_commands / deny_paths）：无论何种模式，命中即拦截，
   用于防止 `rm -rf`、`format`、改写系统目录等不可逆灾难。
   - `deny_paths` 对 **command_exec 同样生效**（命令里出现的路径 token 会被
     抽取并做边界比较），堵住 `cat ~/.ssh/id_rsa` 这类「命令通道任意文件读取」；
   - 解释器的**内联代码执行**（`python -c ...`、`node -e ...`、命令串里出现
     `os.system` / `subprocess` / `eval(`）默认硬性拦截，除非显式开启
     `allow_interpreters`——白名单放行解释器 ≠ 放行任意代码执行。
2. 作用域约束（文件读写 + 白名单模式下的读类命令）：路径必须落在
   working_dir 或 allow_paths 内，且用**路径边界比较**（`is_relative_to` 语义）
   而非字符串前缀，杜绝 `/data/ws-evil/x` 绕过 `/data/ws`。
3. 模式化放行：
   - whitelist（默认，无人值守安全）：写/危险动作仅当命中 allow_commands /
     allow_paths 才放行，其余一律拦截（模型可换方案，不会失控）。
   - interactive（需人看守）：写/危险动作需要「整个任务(run)」先经人工授权；
     未授权前返回待审批提示，人工通过后以 resume_run_id 继续。

风险分级：read（截图/读文件/等待）/ write（写文件、鼠标键盘操作）/ dangerous（命令执行）。
"""

from __future__ import annotations

import os
import re
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
    "wait": "read",  # 纯等待：无副作用（曾未登记 → 白名单模式必被误拦）
    "finish": "read",
}

# 只读工具集合：白名单模式下一律放行（不消耗写/危险额度）
_READ_TOOLS: frozenset[str] = frozenset({"screenshot", "file_read", "wait", "finish"})

# 解释器 / 命令外壳：命中即视为「具备任意代码执行能力」
_INTERPRETER_TOKENS: frozenset[str] = frozenset(
    {
        "python",
        "python3",
        "py",
        "node",
        "npm",
        "npx",
        "ruby",
        "perl",
        "php",
        "bash",
        "sh",
        "zsh",
        "powershell",
        "pwsh",
        "cmd",
    }
)

# 内联代码执行的开关形态（-c / -e / --command / --eval / -Command）
_INLINE_FLAG_RE = re.compile(r"(?:^|\s)-{1,2}(?:c|e|command|eval|exec)\b", re.IGNORECASE)
# 命令串里出现的代码执行特征（解释器兜底执行的常见写法）
_INLINE_CODE_RE = re.compile(
    r"os\.system|subprocess|__import__|eval\s*\(|exec\s*\(|popen\(",
    re.IGNORECASE,
)

# 会把文件内容回灌给模型的命令（典型外泄通道）
_READ_COMMAND_TOKENS: frozenset[str] = frozenset(
    {"cat", "type", "more", "head", "tail", "less", "nl", "od", "strings", "dir", "ls"}
)

# 命令串里的路径 token：~ 开头 / 盘符 / 根路径 / 环境变量（$VAR、%VAR%）
_PATH_TOKEN_RE = re.compile(
    r"""(?<![\w$%])                      # 不被单词字符接续（避免吃掉 -x/foo）
        (?:
              ~[\\/][^\s"';|&<>(){}\[\]]*        # ~/x、~\x
            | ~[\\/]?                            # 单独的 ~
            | [A-Za-z]:[\\/][^\s"';|&<>(){}\[\]]*  # C:\x、D:/x
            | /(?:[^\\\s"';|&<>(){}\[\]]*[/\\])+[^\\\s"';|&<>(){}\[\]]*  # /a/b/c
            | %[A-Za-z_][A-Za-z0-9_]*%[\\/][^\s"';|&<>(){}\[\]]*  # %APPDATA%\x
            | \$\{?[A-Za-z_]\w*\}?[\\/][^\s"';|&<>(){}\[\]]*       # $HOME/x
        )
    """,
    re.VERBOSE,
)


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

    # ── 路径工具 ──────────────────────────────────────
    @staticmethod
    def _normalize_path(p: str) -> str:
        """归一化路径：展开环境变量与 ~、转绝对、统一分隔符与大小写。"""
        try:
            expanded = os.path.expandvars(os.path.expanduser(str(p)))
            return str(Path(expanded).resolve()).replace("\\", "/").lower()
        except (OSError, ValueError):
            return os.path.expandvars(str(p)).replace("\\", "/").lower()

    @staticmethod
    def _is_within(child: str, parent: str) -> bool:
        """路径边界比较：`child` 是否位于 `parent` **目录内部或就是它本身**。

        用「相等 or 以 parent + '/' 开头」代替 startswith，避免
        `/data/ws-evil/x` 被误判为位于 `/data/ws` 之内（SEC-07-3）。
        """
        if not parent:
            return False
        c = child.rstrip("/")
        p = parent.rstrip("/")
        return c == p or c.startswith(p + "/")

    def _in_scope(self, normalized: str) -> bool:
        """归一化路径是否落在 working_dir / allow_paths 之内。"""
        if self._is_within(normalized, self._normalize_path(self._cfg.working_dir)):
            return True
        for allowed in self._cfg.allow_paths:
            if self._is_within(normalized, self._normalize_path(allowed)):
                return True
        return False

    @classmethod
    def _command_path_candidates(cls, command: str) -> list[str]:
        """抽取命令串中疑似文件系统路径的 token（供 deny_paths / 作用域检查）。"""
        out: list[str] = []
        for raw in _PATH_TOKEN_RE.findall(command or ""):
            token = raw.strip().strip("\"'")
            if len(token) < 2 or token.startswith("-"):
                continue
            if "://" in token:  # URL 不是本地路径
                continue
            out.append(token)
        return out

    # ── 硬性拒绝 ──────────────────────────────────────
    def _matches_deny(self, tool_name: str, args: dict[str, Any]) -> str | None:
        """命中危险命令/受限目录则返回拦截原因，否则返回 None。"""
        if tool_name == "command_exec":
            cmd = " ".join(str(args.get("command", "")).lower().split())
            for bad in self._cfg.deny_commands:
                if bad.lower() in cmd:
                    return f"命中危险命令黑名单: 包含 '{bad}'"

            # 解释器内联代码执行（python -c / node -e / ...）
            token = str(args.get("command", "")).strip().split(" ", 1)[0].lower()
            if token in _INTERPRETER_TOKENS and not self._cfg.allow_interpreters:
                if _INLINE_FLAG_RE.search(cmd) or _INLINE_CODE_RE.search(cmd):
                    return (
                        f"命中硬性拦截: 解释器 '{token}' 的内联代码执行已被禁用"
                        "（需执行脚本请直接传脚本路径；确需内联执行请显式开启 allow_interpreters）"
                    )

            # 命令里引用的路径落在受限目录 → 拦截（含 ~/.ssh、C:\Windows 等）
            for raw in self._command_path_candidates(str(args.get("command", ""))):
                norm = self._normalize_path(raw)
                for bad in self._cfg.deny_paths:
                    if self._is_within(norm, bad):
                        return f"命中受限目录黑名单: 命令引用了受限路径 '{raw}'"

        if tool_name in ("file_read", "file_write"):
            path = self._normalize_path(args.get("path", ""))
            for bad in self._cfg.deny_paths:
                if self._is_within(path, bad):
                    return f"命中受限目录黑名单: {args.get('path', '')}"
        return None

    # ── 作用域约束（文件操作 + 白名单模式的读类命令） ──
    def _check_scope(self, tool_name: str, args: dict[str, Any]) -> str | None:
        """文件读写必须落在 working_dir / allow_paths 之内（路径边界比较）。"""
        if tool_name in ("file_read", "file_write"):
            raw = args.get("path", "")
            if not self._in_scope(self._normalize_path(raw)):
                return (
                    f"文件路径越界：'{raw}' 不在工作目录"
                    f"({self._cfg.working_dir})或 allow_paths 允许范围内"
                )
            return None

        if tool_name == "command_exec" and self._cfg.approval_mode == "whitelist":
            # 白名单模式下，读类命令引用的路径同样受作用域约束：
            # 否则 `type D:\secret\key.pem` 可绕过文件读写的围栏。
            token = str(args.get("command", "")).strip().split(" ", 1)[0].lower()
            if token in _READ_COMMAND_TOKENS:
                for raw in self._command_path_candidates(str(args.get("command", ""))):
                    if not self._in_scope(self._normalize_path(raw)):
                        return (
                            f"命令引用了作用域外的路径：'{raw}' 不在工作目录"
                            f"({self._cfg.working_dir})或 allow_paths 允许范围内"
                        )
        return None

    def check_file_scope(self, path: str) -> str | None:
        """供 Agent 文件读写实现做二次校验（纵深防御）。

        返回 None 表示放行，否则返回拦截原因。即使调用方绕过工具层
        直接调 `_read_file` / `_write_file`，作用域约束仍然生效。
        """
        deny = self._matches_deny("file_read", {"path": path})
        if deny:
            return deny
        return self._check_scope("file_read", {"path": path})

    # ── 白名单放行（仅命令执行） ──────────────────────
    def _matches_allow(self, tool_name: str, args: dict[str, Any]) -> bool:
        if tool_name == "command_exec":
            token = str(args.get("command", "")).strip().split(" ", 1)[0].lower()
            return token in self._cfg.allow_commands
        if tool_name == "file_write":
            # 写文件只要通过作用域约束（在 working_dir 内）即视为允许
            return self._check_scope(tool_name, args) is None
        return False

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

        # 2) 作用域约束（文件 / 白名单模式的读类命令）
        scope_err = self._check_scope(tool_name, args)
        if scope_err:
            return SafetyDecision(False, risk, scope_err, signature=sig)

        # 3) 只读动作（截图 / 读文件 / 等待）一律放行
        if risk == "read" and tool_name in _READ_TOOLS:
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
