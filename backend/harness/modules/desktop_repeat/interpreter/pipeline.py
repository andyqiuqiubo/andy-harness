"""9 阶段编译管线（§5.4）：事件流 → 步骤序列 → 工作流草稿图。

S1–S3 在 merge.compile_semantics；本模块做 S4–S9 与图投影。
LLM 语义命名（semantic.py）在本管线之后可选接入，只改 label 不改结构。
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from ..config import RepeatConfig
from ..models import (
    CompileResult,
    CompileStats,
    CompileWarning,
    RecEvent,
    StepDef,
)
from . import locator_builder, waits
from . import risk as risk_mod
from .merge import SemEvent, compile_semantics

logger = logging.getLogger("harness.repeat.compile")

# ── S8 参数模式 ─────────────────────────────────────────────────
_PARAM_PATTERNS: list[tuple[str, str, re.Pattern[str]]] = [
    ("date", "string", re.compile(r"^\d{4}[-/年]\d{1,2}[-/月]\d{1,2}日?$")),
    ("datetime", "string", re.compile(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(:\d{2})?$")),
    ("money", "string", re.compile(r"^[¥$€]?\d{1,3}(,\d{3})*(\.\d{1,2})?$")),
    ("email", "string", re.compile(r"^[\w.+-]+@[\w-]+\.[\w.-]+$")),
    ("phone_cn", "string", re.compile(r"^1[3-9]\d{9}$")),
    ("number", "number", re.compile(r"^-?\d+(\.\d+)?$")),
    ("url", "string", re.compile(r"^https?://[^\s]+$")),
    ("win_path", "string", re.compile(r"^[A-Za-z]:[\\/][^\s]*$")),
]

_GENERIC_TYPE_NAMES = {"edit", "button", "text", "pane", "document", "window", "custom", ""}


def compile_session(events: list[RecEvent], *, session_id: str,
                    cfg: RepeatConfig | None = None,
                    bundle_dir: Path | None = None) -> CompileResult:
    """主入口：events → CompileResult（含 draft_graph）。"""
    t0 = time.perf_counter()
    cfg = cfg or RepeatConfig.load()
    risk_re = risk_mod.compile_words(cfg.risk_words)

    sems, meta = compile_semantics(events)
    warnings: list[CompileWarning] = []
    if meta.get("pauses"):
        warnings.append(CompileWarning("CP-PAUSE", "",
                                       f"已扣除 {meta['pauses']} 段暂停（共 "
                                       f"{round(meta.get('pause_ms', 0) / 1000, 1)} 秒）",
                                       "info"))
    if not sems:
        warnings.append(CompileWarning("CP-EMPTY", "", "事件流不含可执行操作", "error"))
        return CompileResult(session_id=session_id, warnings=warnings,
                             stats=CompileStats(events_scanned=len(events),
                                                duration_ms=int((time.perf_counter() - t0) * 1000)))

    steps: list[StepDef] = []
    noise_steps: list[StepDef] = []
    sid_seq = 0
    prev_t_eff: int | None = None
    param_candidates: list[dict[str, Any]] = []
    used_param_names: set[str] = set()

    def next_sid() -> str:
        nonlocal sid_seq
        sid_seq += 1
        return f"s{sid_seq:02d}"

    for se in sems:
        sid = next_sid()
        ev = se.ev
        step = StepDef(sid=sid, action=_map_action(se), label=_default_label(se))
        step.scope = "window" if se.win else "desktop"
        step.app_hint = _app_hint(se)
        step.evidence = {"event_seq": ev.seq, "event_range": [ev.seq, ev.seq],
                         "frames": [f for f in (ev.data.get("frames") or []) if str(f)]}
        anchor = ev.data.get("anchor_prov")
        if anchor:
            step.evidence["anchor"] = anchor
        if se.password:
            step.action = "manual_confirm"
            step.params = {"prompt": "此步骤涉及密码输入，请人工完成", "mode": "human"}
            step.risk = "read"
            step.policy = {"on_fail": "suspend_manual", "retries": 0, "backoff_ms": 0, "idempotent": True}
            step.confidence = 1.0
            step.needs_review = False
            steps.append(step)
            prev_t_eff = se.t_eff
            continue
        if se.masked:
            step.action = "manual_confirm"
            step.params = {"prompt": "此步骤在屏蔽窗口内进行，请人工确认是否必要", "mode": "human"}
            step.needs_review = True
            steps.append(step)
            prev_t_eff = se.t_eff
            continue

        # S4 定位链
        win_scale = float((ev.pos.scale if ev.pos else 1.0) or 1.0)
        chain, conf = locator_builder.build_chain(ev, anchor, win_scale)
        step.locators = chain
        step.confidence = conf
        # 动作负载入 params（执行层契约，§4.5）
        if step.action == "type":
            step.params = {"mode": "auto", "value": str(ev.data.get("text", "") or "")}
        elif step.action == "hotkey":
            step.params = {"combo": str(ev.data.get("combo", "") or "")}
        elif step.action == "scroll":
            step.params = {"dx": int(ev.data.get("dx", 0) or 0), "dy": int(ev.data.get("dy", 0) or 0)}
        elif step.action == "drag":
            step.params = {"path": ev.data.get("path") or [], "to": ev.data.get("to") or []}
        elif step.action == "window_focus":
            # 焦点切换：目标窗口取"期望到达的窗口"（curr），非当前窗口
            step.params = {}
            curr = (ev.data.get("curr") or {}) if isinstance(ev.data.get("curr"), dict) else {}
            if curr.get("exe") or curr.get("title"):
                hint: dict[str, Any] = {}
                if curr.get("exe"):
                    hint["exe"] = curr.get("exe")
                # 标题为空时**不能**生成 ".*.*"：那会匹配任意窗口，作用域形同虚设
                if curr.get("title"):
                    hint["window_title_regex"] = (
                        f".*{re.escape(str(curr.get('title', ''))[:40])}.*")
                step.app_hint = {k: v for k, v in hint.items() if v}
                step.scope = "window"
        if ev.uia is None and ev.type in ("click", "double_click", "right_click", "drag"):
            step.needs_review = True
            warnings.append(CompileWarning("CP-NOUIA", sid,
                                          f"{sid} 采集时未获得界面语义（超时/不支持），已降级为图像/坐标定位", "warn"))
        if se.ime_risk:
            step.needs_review = True
            warnings.append(CompileWarning("CP-IME", sid, f"{sid} 文本上屏未能确认，请人工核对内容", "warn"))

        # S5 等待
        step.timing = waits.infer_timing(prev_t_eff, se)
        prev_t_eff = se.t_eff

        # S6 断言/无效点击建议
        _apply_assertions(step, se, warnings)

        # S8 参数建议（type 文本）
        if step.action == "type":
            text = str(ev.data.get("text", "") or "")
            if text:
                pname = _maybe_param(step, text, param_candidates, used_param_names)
                if pname:
                    step.label = _param_label(step, pname)

        # S9 风险
        name = str((ev.uia.name if ev.uia else "") or "")
        title = str(se.win.get("title", "") or "")
        base_risk = _base_risk(se)
        r, nr, reason = risk_mod.annotate(risk_re, action=se.type, name=name, title=title, base_risk=base_risk)
        step.risk = r
        if nr:
            step.needs_review = True
            step.policy = {**step.policy, "on_fail": "suspend_manual"}
            if reason:
                warnings.append(CompileWarning("CP-RISK", sid, reason, "warn"))

        # S9.5 噪声步骤：任务栏 / "运行中的应用程序" 列表的点击属于操作过程，
        # 不是业务流程（窗口切换已由 window_focus 保证）。回放时点任务栏会
        # 切到错误应用，直接导致后续步骤失败。真机实录：一次"百度搜索刘德华"
        # 的 17 步里混入 5 个任务栏/文件对话框操作。
        if _is_shell_noise(step):
            noise_steps.append(step)
            warnings.append(CompileWarning(
                "CP-NOISE", sid,
                f"{sid} 是任务栏/运行中应用列表操作，已自动跳过（不属于业务流程）", "info"))
            prev_t_eff = se.t_eff
            continue

        steps.append(step)

    # 极端兜底：若整段录制只有任务栏操作，全部跳过会让流程变空 —— 此时保留，
    # 由用户自行判断（只是加警告，不静默丢流程）。
    if not steps and noise_steps:
        steps = noise_steps
        warnings.append(CompileWarning("CP-NOISE", "", "所有步骤均为任务栏操作，未自动跳过", "warn"))

    # S7 循环检测（步骤层）
    loops = _detect_loops(steps)
    for lp in loops:
        warnings.append(CompileWarning("CP-LOOP", lp["sid"],
                                       f"检测到疑似循环（{lp['count']} 次相似操作），可改为循环展开", "info"))

    # 发布前静态校验
    for step in steps:
        err = step.validate()
        if err:
            warnings.append(CompileWarning("CP-INVALID", step.sid, err, "error"))

    stats = CompileStats(
        steps=len(steps),
        low_confidence=sum(1 for s in steps if s.confidence < 0.5),
        needs_review=sum(1 for s in steps if s.needs_review),
        loop_candidates=len(loops),
        param_candidates=len(param_candidates),
        duration_ms=int((time.perf_counter() - t0) * 1000),
        events_scanned=len(events),
        pauses_excluded=int(meta.get("pauses") or 0),
    )
    graph = to_draft_graph(steps, param_candidates, session_id)
    return CompileResult(session_id=session_id, steps=steps, warnings=warnings,
                         stats=stats, draft_graph=graph, params=param_candidates)


# ── S4–S8 辅助 ─────────────────────────────────────────────────
def _is_shell_noise(step: StepDef) -> bool:
    """任务栏 / "运行中的应用程序" 列表上的点击 = 操作过程噪声，不是业务流程。

    判定必须严格：只有**任一 UIA 定位器**明确指向任务栏层级才成立，
    避免误伤业务窗口内标题含"任务栏"字样的控件。
    """
    if step.action not in ("click", "double_click", "right_click"):
        return False
    uia = [loc for loc in step.locators if getattr(loc, "kind", "") == "uia"]
    if not uia:
        return False
    for spec in uia:
        v = getattr(spec, "value", None)
        text = json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else str(v or "")
        if "Pane[任务栏]" in text or "ToolBar[运行中的应用程序]" in text:
            return True
    return False


def _map_action(se: SemEvent) -> str:
    if se.type == "window_focus":
        # 首步/无前序同窗步骤 → 显式窗口步骤（launch 授权后的 focus 链入口）
        return "window_focus"
    return {"click": "click", "double_click": "double_click", "right_click": "right_click",
            "middle_click": "click", "drag": "drag", "scroll": "scroll", "type": "type",
            "hotkey": "hotkey", "window_change": "window_focus",
            "marker": "marker"}.get(se.type, "click")


def _default_label(se: SemEvent) -> str:
    uia = se.ev.uia
    if uia is not None and uia.name:
        return f"{se.type} → {uia.control_type}『{uia.name}』"
    if uia is not None and uia.control_type:
        return f"{se.type} → {uia.control_type}"
    return str(se.type)


def _app_hint(se: SemEvent) -> dict[str, Any]:
    win = se.win or {}
    title = str(win.get("title", "") or "")
    exe = str(win.get("exe", "") or "")
    cls = str(win.get("cls", "") or "")
    hint: dict[str, Any] = {}
    if exe:
        hint["exe"] = exe
    if cls:
        hint["cls"] = cls
    if title:
        stem = title.split(" - ")[0].strip() or title
        safe = re.escape(stem[:40])
        hint["window_title_regex"] = f".*{safe}.*"
    return hint


def _base_risk(se: SemEvent) -> str:
    return {"window_focus": "read", "marker": "read", "scroll": "read"}.get(se.type, "write")


def _apply_assertions(step: StepDef, se: SemEvent, warnings: list[CompileWarning]) -> None:
    diff = se.ev.data.get("post_diff")
    if se.type in ("click", "right_click", "double_click") and isinstance(diff, (int, float)) and diff < 0.002:
        warnings.append(CompileWarning("CP-NOOP", step.sid,
                                       f"{step.sid} 点击后界面无可观察变化（可能为无效点击）", "info"))
    if step.action == "type":
        step.assertion = {"kind": "value_equals", "locator_index": 0,
                          "value": str(step.params.get("value", "")) or None,
                          "optional": True}
        if step.assertion["value"] is None:
            step.assertion = None


def _maybe_param(step: StepDef, text: str, candidates: list[dict[str, Any]],
                 used: set[str]) -> str | None:
    for pname, ptype, rx in _PARAM_PATTERNS:
        if rx.match(text.strip()):
            base = pname if pname not in used else f"{pname}_{len(candidates) + 1}"
            name = base
            i = 1
            while name in used:
                i += 1
                name = f"{base}{i}"
            used.add(name)
            step.params = {"mode": "clipboard_paste", "value": f"{{{{param.{name}}}}}"}
            step.assertion = {"kind": "value_equals", "locator_index": 0,
                              "value": f"{{{{param.{name}}}}}", "optional": True}
            cand = {"name": name, "type": ptype, "required": False, "default": text,
                    "source": "literal", "sensitive": False,
                    "description": f"录制值：{text[:30]}", "from_step": step.sid}
            candidates.append(cand)
            step.ext["param_candidates"] = step.ext.get("param_candidates", []) + [name]
            return name
    return None


def _param_label(step: StepDef, pname: str) -> str:
    return step.label + f"（参数 {pname}）" if step.label else f"填入参数 {pname}"


def _detect_loops(steps: list[StepDef]) -> list[dict[str, Any]]:
    """≥3 次「同 action + 结构路径前缀 + 索引递增」子序列。"""
    loops: list[dict[str, Any]] = []
    i = 0
    while i < len(steps) - 2:
        run = _match_loop_at(steps, i)
        if run >= 3:
            prefix_steps = steps[i:i + run]
            loops.append({"sid": steps[i].sid, "count": run})
            for s in prefix_steps:
                s.ext["loop_group"] = f"{steps[i].sid}..{steps[i + run - 1].sid}"
                s.ext["loop_candidate"] = True
            i += run
        else:
            i += 1
    return loops


def _match_loop_at(steps: list[StepDef], start: int) -> int:
    """返回从 start 起的连续相似步数（action 相同 + 主定位器"同族不同序号"）。"""
    if start + 2 >= len(steps):
        return 0
    a0 = steps[start].action
    if a0 not in ("click", "type", "right_click", "scroll"):
        return 0
    n = 1
    for j in range(start + 1, len(steps)):
        sj = steps[j]
        if sj.action != a0:
            break
        if not _similar_family(steps[j - 1], sj):
            break
        n += 1
    return n


def _similar_family(a: StepDef, b: StepDef) -> bool:
    """同族：主定位器 kind/by 相同且 value 仅序号/索引不同，或同为 name_type 且前缀同。"""
    la = a.locators[0] if a.locators else None
    lb = b.locators[0] if b.locators else None
    if la is None or lb is None or la.kind != lb.kind or la.by != lb.by:
        return False
    va, vb = str(la.value or ""), str(lb.value or "")
    if not va or va != vb:
        # 数字/序号不同 → 视为同族候选
        if la.by in ("automation_id", "name_type", "class_index", "structure_path"):
            return _numeral_variant(va, vb)
        return False
    return True


def _numeral_variant(a: str, b: str) -> bool:
    na = re.sub(r"\d+", "#", a)
    nb = re.sub(r"\d+", "#", b)
    return na == nb and a != b


# ── 图投影（§4.2 ADR-10） ───────────────────────────────────────
def to_draft_graph(steps: list[StepDef], params: list[dict[str, Any]],
                   session_id: str) -> dict[str, Any]:
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    # 布局：start 独占首格，步骤从第 2 格开始按 3 列换行，end 接在最后一步之后。
    # 旧实现 start 与首个步骤都在 (60,120)，画布上两者完全重叠（真机截图坐实）。
    cols = 3
    step_x0, step_y0, dx, dy = 60, 120, 260, 160

    def _slot(i: int) -> dict[str, int]:
        return {"x": step_x0 + (i % cols) * dx, "y": step_y0 + (i // cols) * dy}

    start_vars = [{"name": p["name"], "type": p.get("type", "string"),
                   "default": p.get("default", ""), "required": bool(p.get("required")),
                   "description": p.get("description", ""), "pattern": p.get("pattern", ""),
                   "sensitive": bool(p.get("sensitive")), "source": p.get("source", "literal"),
                   "expression": p.get("expression", "")}
                  for p in params]
    nodes.append({"id": "n0", "type": "start", "position": _slot(0),
                  "data": {"variables": start_vars, "params": start_vars}})
    prev_id = "n0"
    for k, s in enumerate(steps):
        nid = f"n{k + 1}"
        nodes.append({"id": nid, "type": "gui", "position": _slot(k + 1),
                      "data": {**s.to_dict(), "step_schema": s.step_schema}})
        edges.append({"id": f"e{prev_id}-{nid}", "source": prev_id, "target": nid})
        prev_id = nid
    end_id = f"n{len(steps) + 1}"
    nodes.append({"id": end_id, "type": "end", "position": _slot(len(steps) + 1),
                  "data": {"outputs": []}})
    edges.append({"id": f"e{prev_id}-{end_id}", "source": prev_id, "target": end_id})
    return {"nodes": nodes, "edges": edges,
            "meta": {"source": "desktop_repeat", "session_id": session_id,
                     "compiled_at": datetime.now().astimezone().isoformat(timespec="seconds")}}
