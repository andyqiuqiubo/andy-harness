"""权限管理服务 —— 工具风险分级 + Human-in-the-loop 审批。

行业依据：
- Cline：每个动作默认暂停等待人工确认
- Claude Code：三级权限（只读自动放行 / 状态修改需确认 / 危险操作拒绝）
- Microsoft Agent Framework：human-in-the-loop approvals
- Codex：OS 沙箱 + 工作区限制

核心原则：**模型生成的工具调用是不可信输入**，deny-by-default 之外的一切
都应当可审计、可确认。

策略（mode）：
- `auto`：全部放行（不推荐，仅供信任场景）
- `confirm_dangerous`：**默认**。仅 dangerous 级工具需确认
- `confirm_write`：write 与 dangerous 级均需确认
- `confirm_all`：所有工具都需确认

任意工具可被 `overrides` 单独覆盖为 `auto` / `confirm` / `deny`。
"""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger("harness.permissions")

# 风险等级
RISK_READ = "read"
RISK_WRITE = "write"
RISK_DANGEROUS = "dangerous"

RISK_ORDER = {RISK_READ: 0, RISK_WRITE: 1, RISK_DANGEROUS: 2}

# 策略模式
MODE_AUTO = "auto"
MODE_CONFIRM_DANGEROUS = "confirm_dangerous"
MODE_CONFIRM_WRITE = "confirm_write"
MODE_CONFIRM_ALL = "confirm_all"

VALID_MODES = (MODE_AUTO, MODE_CONFIRM_DANGEROUS, MODE_CONFIRM_WRITE, MODE_CONFIRM_ALL)

# 单工具覆盖动作
ACTION_AUTO = "auto"
ACTION_CONFIRM = "confirm"
ACTION_DENY = "deny"

VALID_ACTIONS = (ACTION_AUTO, ACTION_CONFIRM, ACTION_DENY)

# 动作结果
ALLOW = "allow"
CONFIRM = "confirm"
DENY = "deny"


@dataclass
class Decision:
    """权限决策结果。"""

    tool_name: str
    risk: str
    action: str  # allow / confirm / deny
    reason: str = ""

    @property
    def needs_confirm(self) -> bool:
        """是否需要人工确认。"""
        return self.action == CONFIRM

    @property
    def denied(self) -> bool:
        """是否被拒绝。"""
        return self.action == DENY

    def to_dict(self) -> dict[str, Any]:
        """序列化。"""
        return {
            "tool_name": self.tool_name,
            "risk": self.risk,
            "action": self.action,
            "reason": self.reason,
        }


class PermissionService(ABC):
    """权限服务契约。"""

    @abstractmethod
    def decide(self, tool_name: str) -> Decision:
        """判定某工具本次调用应采取的动作。"""

    @abstractmethod
    def get_risk(self, tool_name: str) -> str:
        """获取工具风险等级（未注册工具默认 dangerous）。"""

    @abstractmethod
    def set_mode(self, mode: str) -> bool:
        """设置全局策略模式。"""

    @abstractmethod
    def get_mode(self) -> str:
        """获取全局策略模式。"""

    @abstractmethod
    def set_override(self, tool_name: str, action: str | None) -> None:
        """设置/清除单工具覆盖。"""

    @abstractmethod
    def get_overrides(self) -> dict[str, str]:
        """获取所有单工具覆盖。"""

    @abstractmethod
    def list_tool_risks(self) -> list[dict[str, Any]]:
        """列出所有已注册工具的风险等级。"""


class PermissionServiceImpl(PermissionService):
    """权限服务默认实现。"""

    def __init__(
        self,
        tool_registry: Any = None,
        state_file: Path | None = None,
        mode: str | None = None,
    ) -> None:
        self._tool_registry = tool_registry
        self._state_file = state_file or (
            Path(__file__).resolve().parents[3] / "data" / "permission.json"
        )
        self._mode = MODE_CONFIRM_DANGEROUS
        self._overrides: dict[str, str] = {}
        self._load_state()
        # mode=None 表示"不覆盖持久化值"；显式传入则优先（便于测试与临时覆盖）
        if mode and mode in VALID_MODES:
            self._mode = mode

    # ── 决策 ──────────────────────────────────────────

    def get_risk(self, tool_name: str) -> str:
        """获取工具风险等级；未注册工具按 dangerous 处理（最保守）。"""
        if self._tool_registry is None:
            return RISK_DANGEROUS
        try:
            tool = self._tool_registry.get(tool_name)
        except Exception:
            return RISK_DANGEROUS
        risk = getattr(tool, "risk_level", RISK_WRITE)
        return risk if risk in RISK_ORDER else RISK_WRITE

    def decide(self, tool_name: str) -> Decision:
        """判定动作：单工具覆盖 > 全局策略。"""
        risk = self.get_risk(tool_name)

        override = self._overrides.get(tool_name)
        if override == ACTION_DENY:
            return Decision(tool_name, risk, DENY, "已被用户加入拒绝列表")
        if override == ACTION_CONFIRM:
            return Decision(tool_name, risk, CONFIRM, "该工具被单独设置为需确认")
        if override == ACTION_AUTO:
            return Decision(tool_name, risk, ALLOW, "该工具被单独设置为自动放行")

        if self._mode == MODE_AUTO:
            return Decision(tool_name, risk, ALLOW, "当前策略：全部自动放行")
        if self._mode == MODE_CONFIRM_ALL:
            return Decision(tool_name, risk, CONFIRM, "当前策略：所有工具需确认")
        if self._mode == MODE_CONFIRM_WRITE:
            if risk in (RISK_WRITE, RISK_DANGEROUS):
                return Decision(tool_name, risk, CONFIRM, "当前策略：写操作与危险操作需确认")
            return Decision(tool_name, risk, ALLOW, "只读工具自动放行")

        # 默认 MODE_CONFIRM_DANGEROUS
        if risk == RISK_DANGEROUS:
            return Decision(
                tool_name, risk, CONFIRM, "当前策略：危险操作（可执行代码）需确认"
            )
        return Decision(tool_name, risk, ALLOW, "非危险工具自动放行")

    # ── 配置 ──────────────────────────────────────────

    def set_mode(self, mode: str) -> bool:
        """设置全局策略模式。"""
        if mode not in VALID_MODES:
            return False
        self._mode = mode
        self._save_state()
        return True

    def get_mode(self) -> str:
        """获取全局策略模式。"""
        return self._mode

    def set_override(self, tool_name: str, action: str | None) -> None:
        """设置或清除单工具覆盖（None 表示清除）。"""
        if action is None:
            self._overrides.pop(tool_name, None)
        elif action in VALID_ACTIONS:
            self._overrides[tool_name] = action
        else:
            raise ValueError(f"无效的覆盖动作: {action}")
        self._save_state()

    def get_overrides(self) -> dict[str, str]:
        """获取所有覆盖。"""
        return dict(self._overrides)

    def list_tool_risks(self) -> list[dict[str, Any]]:
        """列出所有已注册工具的风险等级与生效动作。"""
        if self._tool_registry is None:
            return []
        result: list[dict[str, Any]] = []
        for entry in self._tool_registry.list_tools():
            name = entry["name"]
            decision = self.decide(name)
            result.append(
                {
                    "name": name,
                    "risk": decision.risk,
                    "action": decision.action,
                    "reason": decision.reason,
                    "owner": entry.get("owner", ""),
                }
            )
        return sorted(result, key=lambda x: (-RISK_ORDER.get(x["risk"], 0), x["name"]))

    # ── 持久化 ────────────────────────────────────────

    def _load_state(self) -> None:
        """从状态文件加载配置。"""
        try:
            if self._state_file.is_file():
                data = json.loads(self._state_file.read_text(encoding="utf-8"))
                mode = data.get("mode", MODE_CONFIRM_DANGEROUS)
                if mode in VALID_MODES:
                    self._mode = mode
                overrides = data.get("overrides", {})
                self._overrides = {
                    k: v for k, v in overrides.items() if v in VALID_ACTIONS
                }
        except Exception as e:
            logger.warning("读取权限配置失败: %s", e)

    def _save_state(self) -> None:
        """写入状态文件。"""
        try:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            self._state_file.write_text(
                json.dumps(
                    {"mode": self._mode, "overrides": self._overrides},
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning("写入权限配置失败: %s", e)
