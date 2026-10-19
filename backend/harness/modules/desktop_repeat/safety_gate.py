"""L5 治理层（§2.1 / §9）：动作前置门、风险分级、审批。

语义与 computer_use.SafetyPolicy 对齐（不绕过、拦截即中止），独立实现：
- 动作下发前唯一出口 check(...)；
- read 直接放行；write/dangerous 走模式化放行（write 会话首次确认；dangerous 每次确认）；
- 审批由注入的 approval_provider 完成（REST/对话/无人值守转待办）；
- 密码字段默认禁止自动回放（RP-4004 → 人工）。
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .models import STEP_ACTIONS

RISK_OF_DEFAULT: dict[str, str] = {
    "scroll": "read", "wait_window": "read", "wait_element": "read",
    "assert_element": "read", "assert_text": "read", "marker": "read",
    "window_focus": "read", "manual_confirm": "read",
}


@dataclass(slots=True)
class Decision:
    allow: bool
    reason: str = ""
    pending_approval: bool = False
    approval_token: str = ""
    code: str = ""            # 拒绝/待审时的错误码（RP-4002/4003/4004）

    def verdict(self) -> str:
        return "allow" if self.allow else ("confirm" if self.pending_approval else "deny")


ApprovalProvider = Callable[[dict[str, Any]], Decision]


@dataclass
class SafetyGate:
    """动作门。approval_provider=None 时危险/写动作返回"待确认"（上层转人工）。"""

    mode: str = "interactive"                 # whitelist | interactive | auto
    approval_provider: ApprovalProvider | None = None
    allow_password: bool = False
    # 写操作审批策略：
    #   degraded  —— 仅"降级定位（图像/相对/坐标）"的写操作需要确认
    #   first_write —— 旧行为：本轮首次写操作确认一次
    #   always    —— 每次写操作都确认
    #   never     —— 写操作全部放行，危险动作也免中途询问（默认；见 config 注释）
    approval_policy: str = "never"
    _session_granted: set[str] = field(default_factory=set)
    _audit_sink: list[dict[str, Any]] = field(default_factory=list)
    _audit_cb: Callable[[dict[str, Any]], None] | None = None
    _pending: dict[str, dict[str, Any]] = field(default_factory=dict)

    def risk_of(self, action: str, declared: str) -> str:
        return RISK_OF_DEFAULT.get(action, declared or "write")

    def check(self, *, run_id: str, sid: str, action: str, risk: str,
              app_hint: dict[str, Any], params: dict[str, Any],
              scope_ok: bool, hit_kind: str = "", hit_index: int = 0,
              degraded: bool = False, step_index: int = 0) -> Decision:
        if action not in STEP_ACTIONS:
            self._audit(run_id, sid, action, "dangerous", "deny", "未知动作")
            return Decision(False, f"未知动作: {action}", code="RP-4002")
        if not scope_ok:
            self._audit(run_id, sid, action, "dangerous", "deny", "越界窗口")
            return Decision(False, "目标窗口不在流程声明的应用作用域内", code="RP-4002")
        eff = self.risk_of(action, risk)
        if eff == "read":
            self._audit(run_id, sid, action, eff, "allow", "只读动作")
            return Decision(True)
        if params.get("password") and not self.allow_password:
            self._audit(run_id, sid, action, "dangerous", "confirm", "密码")
            return Decision(False, "密码字段需人工输入（默认禁止自动回放）", True,
                            self._mk_token(run_id, sid), code="RP-4004")
        if eff == "write":
            if self.mode == "auto" or run_id in self._session_granted:
                self._audit(run_id, sid, action, eff, "allow",
                            "自动放行" if self.mode == "auto" else "会话已授权")
                return Decision(True)
            policy = str(self.approval_policy or "degraded").lower()
            if policy == "never":
                self._session_granted.add(run_id)
                self._audit(run_id, sid, action, eff, "allow", "策略：写操作免审")
                return Decision(True)
            if (policy == "degraded" and hit_kind == "uia"
                    and int(hit_index or 0) == 0 and not degraded):
                # 首级 UIA 命中：界面语义明确、位置可信，直接放行。
                # 注意**不写入** _session_granted，否则后面的降级步骤也会被免审。
                self._audit(run_id, sid, action, eff, "allow", "UIA 首级命中")
                return Decision(True)
            reason = self._reason_for(action, eff, hit_kind, hit_index, degraded, step_index)
            d = self._request(run_id, sid, action, eff, reason=reason)
            if d.allow:
                self._session_granted.add(run_id)
            return d
        # dangerous：每次确认（会话授权不覆盖危险动作，§9.2）
        if self.mode == "auto":
            self._audit(run_id, sid, action, eff, "allow", "自动放行(无人值守)")
            return Decision(True)
        if str(self.approval_policy or "").lower() == "never":
            # 显式关闭中途确认：高风险动作也直接执行，但审计留痕
            self._audit(run_id, sid, action, eff, "allow", "策略：免审（高风险动作）")
            return Decision(True)
        return self._request(run_id, sid, action, "dangerous",
                             reason=f"第 {step_index + 1} 步是高风险操作（{action}），请逐次确认")

    @staticmethod
    def _reason_for(action: str, risk: str, hit_kind: str, hit_index: int,
                    degraded: bool, step_index: int) -> str:
        head = f"第 {step_index + 1} 步（{action}）"
        if hit_kind and hit_kind != "uia":
            label = {"image": "图像匹配", "relative": "相对位置", "coord": "坐标"}.get(
                hit_kind, hit_kind)
            return f"{head}未能通过界面语义定位，将按{label}执行；确认后本轮剩余步骤不再询问"
        if degraded:
            return f"{head}使用了降级定位；确认后本轮剩余步骤不再询问"
        return f"{head}需要人工确认；确认后本轮剩余步骤不再询问"

    def _request(self, run_id: str, sid: str, action: str, risk: str,
                 reason: str = "") -> Decision:
        msg = reason or "该步骤需人工确认"
        if self.approval_provider is None:
            token = self._mk_token(run_id, sid)
            self._pending[token] = {"run_id": run_id, "sid": sid, "action": action,
                                    "risk": risk, "created": time.time()}
            self._audit(run_id, sid, action, risk, "confirm", "待审批")
            return Decision(False, msg, True, token,
                            code="RP-4003" if risk == "dangerous" else "RP-4004")
        d = self.approval_provider({"run_id": run_id, "sid": sid, "action": action,
                                    "risk": risk, "reason": msg})
        # 审批提供方（REST/对话）只负责"记录 + 通知 + 返回待审"，不一定带 token；
        # 这里统一补发可授权 token，否则 resume("retry") 的 gate.grant() 永远失败，
        # 交互模式会无限重复"挂起→重试→再挂起"（真机验证坐实）。
        if d.pending_approval and not d.reason:
            d.reason = msg
        if d.pending_approval and not d.approval_token:
            d.approval_token = self._mk_token(run_id, sid)
            self._pending[d.approval_token] = {"run_id": run_id, "sid": sid,
                                               "action": action, "risk": risk,
                                               "created": time.time()}
        self._audit(run_id, sid, action, risk, d.verdict(), d.reason)
        return d

    def grant(self, token: str) -> bool:
        item = self._pending.pop(token, None)
        if not item:
            return False
        self._session_granted.add(str(item.get("run_id", "")))
        return True

    def reject(self, token: str) -> None:
        self._pending.pop(token, None)

    def list_pending(self) -> list[dict[str, Any]]:
        now = time.time()
        expired = [k for k, v in self._pending.items() if now - float(v.get("created", now)) > 3600]
        for k in expired:
            self._pending.pop(k, None)
        return [{"token": k, **v} for k, v in self._pending.items()]

    def _mk_token(self, run_id: str, sid: str) -> str:
        return f"ap-{uuid.uuid4().hex[:8]}"

    def _audit(self, run_id: str, sid: str, action: str, risk: str, decision: str, reason: str) -> None:
        rec = {"ts": time.time(), "run_id": run_id, "sid": sid, "action": action,
               "risk": risk, "decision": decision, "reason": reason}
        self._audit_sink.append(rec)
        if self._audit_cb:
            try:
                self._audit_cb(rec)
            except Exception:  # noqa: BLE001
                pass

    def recent_audit(self, limit: int = 50) -> list[dict[str, Any]]:
        return list(self._audit_sink[-limit:])
