"""失败阶梯与诊断包（§7.4 / §7.6）。

阶梯决策（classify_ladder）：给定错误码/当前降级状态/环境级别/重试计数，
返回下一步动作：retry / degrade / coord_blind / suspend / abort / skip / continue_mark。

诊断包（§7.6）：report.json + expected.png（锚点）+ actual.png（失败全屏）+
locator_trace.json（每策略耗时/原因）+ window_tree.txt。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from ..errors import RepeatError
from ..models import StepDef

RETRYABLE = {"RP-1003", "RP-1007", "RP-2004", "RP-2005", "RP-3001", "RP-5001", "RP-5003"}
LOC_FAIL = {"RP-2001"}


@dataclass(slots=True)
class LadderStep:
    action: str              # retry | suspend | abort | skip | continue
    code: str = ""
    reason: str = ""
    backoff_ms: int = 0
    relaxed: bool = False


def classify_ladder(err: RepeatError, *, step: StepDef, run_level: str,
                    attempts: int, retries_left: int, run_retries_left: int,
                    on_fail: str, has_coord: bool) -> LadderStep:
    """定位链已由 resolve_chain 一次性走完（含逐级降级与闸门），本函数只决定
    **步骤级**处置：放宽重试 / 挂起 / 中止 / 跳过。

    注（与 §7.4 的分工）：「链内降级」发生在 resolve_chain 内部，不重复出现在这里；
    「坐标兜底一次」同理——链里存在 coord 且闸门放行时已被尝试过，
    链里根本没有 coord 时不得凭空造兜底（has_coord=False 直接走 on_fail）。
    """
    code = err.code
    # 安全拦截/审批拒绝/密码：绝不绕过
    if code in ("RP-4002", "RP-4003", "RP-4001"):
        return LadderStep("abort", code, "安全/权限硬拦截")
    if code in ("RP-4004", "RP-7002"):
        return LadderStep("suspend", code, "需人工介入")
    # 环境/系统类
    if code in ("RP-1001", "RP-1006"):
        return LadderStep("abort", code, "环境/应用不可用")
    if code in ("RP-8001", "RP-8002", "RP-8003", "RP-6001", "RP-6002", "RP-6004"):
        return LadderStep("abort", code, "校验/参数错误")
    if code == "RP-2006":
        return LadderStep("suspend", code, "环境变化阻止坐标兜底")
    if code == "RP-3004":
        return LadderStep("continue", code, "界面持续变化，标记继续")
    # 定位耗尽：先按"放宽重试"（name 编辑距离≤2 / 结构兄弟容差±1，§场景 5）
    if code in ("RP-2001", "RP-2005"):
        if attempts < retries_left and run_retries_left > 0:
            return LadderStep("retry", code, f"放宽重试 {attempts + 1}/{retries_left}",
                              backoff_ms=int((step.policy.get("backoff_ms") or 800) * (2 ** attempts)),
                              relaxed=True)
        return LadderStep(_on_fail_to(on_fail), code, "定位耗尽")
    # 可重试瞬态（未就绪/遮挡/焦点/超时/输入不一致）
    if code in RETRYABLE:
        if attempts < retries_left and run_retries_left > 0:
            return LadderStep("retry", code, f"瞬态重试 {attempts + 1}/{retries_left}",
                              backoff_ms=int((step.policy.get("backoff_ms") or 800) * (2 ** attempts)))
        return LadderStep(_on_fail_to(on_fail), code, "瞬态耗尽")
    if code in ("RP-3001", "RP-3003"):
        if attempts < retries_left and run_retries_left > 0:
            return LadderStep("retry", code, "等待重试",
                              backoff_ms=step.policy.get("backoff_ms", 800))
        return LadderStep(_on_fail_to(on_fail), code, "等待超时")
    if code == "RP-3002":
        return LadderStep(_on_fail_to(on_fail), code, "断言失败")
    # 兜底：按 on_fail
    return LadderStep(_on_fail_to(on_fail), code, "未分类错误")


def _untried_kinds(step: StepDef, tried: list[str]) -> list[str]:
    return [spec.kind for spec in step.locators if spec.kind not in tried]


def _on_fail_to(on_fail: str) -> str:
    return {"suspend_manual": "suspend", "abort": "abort", "skip": "skip",
            "ladder": "suspend"}.get(str(on_fail), "suspend")


# ── 诊断包 ───────────────────────────────────────────────────────
def build_diagnostic_package(root: Path, run_id: str, step: StepDef, err: RepeatError,
                             *, grabber: Any, windows: Any, hits: list[dict[str, Any]],
                             anchor_bytes: bytes | None, expected_frame: str) -> str:
    """落盘诊断目录，返回其路径。所有 IO 尽力而为，失败不抛出（诊断不可用不致命）。"""
    d = root / "diagnostics" / run_id / step.sid
    try:
        d.mkdir(parents=True, exist_ok=True)
        report = {
            "run_id": run_id, "sid": step.sid, "action": step.action,
            "code": err.code, "message": err.message, "detail": err.detail,
            "attempts": hits, "env": os.environ.get("COMPUTERNAME", ""),
            "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
        _write(d / "report.json", json.dumps(report, ensure_ascii=False, indent=2))
        # actual：全屏
        try:
            png = grabber.grab(None)
            _write_bytes(d / "actual.png", png)
        except Exception:  # noqa: BLE001
            pass
        # expected：锚点
        if anchor_bytes:
            _write_bytes(d / "expected.png", anchor_bytes)
        _write(d / "expected_frame.txt", str(expected_frame or ""))
        # locator_trace
        _write(d / "locator_trace.json", json.dumps(hits, ensure_ascii=False, indent=2))
        # window_tree
        try:
            win = windows.foreground()
            tree = ""
            if win is not None:
                tree = f"{win.title}\n  exe={win.exe} cls={win.cls} pid={win.pid} rect={win.rect}"
            _write(d / "window_tree.txt", tree)
        except Exception:  # noqa: BLE001
            _write(d / "window_tree.txt", "")
        return str(d)
    except Exception:  # noqa: BLE001
        return str(d)


def _write(path: Path, text: str) -> None:
    try:
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    except Exception:  # noqa: BLE001
        pass


def _write_bytes(path: Path, data: bytes) -> None:
    try:
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)
    except Exception:  # noqa: BLE001
        pass
