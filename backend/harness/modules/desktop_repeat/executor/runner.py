"""RepeatRunner：回放状态机（§5.2 / §6.3 / §7.4 / §7.5）。

asyncio 任务，跑在 backend 内；平台重调用经 asyncio.to_thread 出事件循环。
流程：租约(服务层) → 预检 → 逐步微状态机（resolve/pre_wait/act/settle/assert）
→ 失败阶梯 → 挂起/续跑/检查点 → 收口事件（SSE 总线 Reporter）。

_run_step 返回控制令牌（避免"失败收口后仍去 await 挂起"的死等 bug）：
    NEXT       步进下一步（含 skip 成功）
    SUSPEND    已挂起，主循环 await 人工裁决
    FATAL      已收口（failed/aborted），主循环退出
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import threading
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from ..bus import Reporter
from ..errors import RepeatError
from ..models import (
    Attempt,
    DryRunReport,
    DryRunStep,
    Env,
    Hit,
    RunRecord,
    StepDef,
    StepRun,
    WindowRef,
)
from ..safety_gate import Decision, SafetyGate
from . import actions as actions_mod
from . import envcheck
from .assertions import AssertCtx, check_assert
from .pacing import (
    BETWEEN_STEP_MS,
    SETTLE_DEFAULT_MS,
    HumanTakeover,
    RunBudget,
    StepBudget,
)
from .recovery import build_diagnostic_package, classify_ladder

logger = logging.getLogger("harness.repeat.runner")

NEXT, SUSPEND, FATAL = "next", "suspend", "fatal"


def steps_from_graph(graph: dict[str, Any]) -> list[StepDef]:
    """从工作流图按拓扑顺序提取 gui 节点为步骤序列（start→…→end）。"""
    nodes = {n["id"]: n for n in graph.get("nodes", []) or []}
    edges = graph.get("edges", []) or []
    incoming: dict[str, list[dict[str, Any]]] = {nid: [] for nid in nodes}
    outgoing: dict[str, list[dict[str, Any]]] = {nid: [] for nid in nodes}
    for e in edges:
        if e.get("source") in nodes and e.get("target") in nodes:
            outgoing[e["source"]].append(e)
            incoming[e["target"]].append(e)
    starts = [n for n in nodes.values() if n.get("type") == "start"]
    if not starts:
        raise RepeatError("RP-6004", "工作流缺少 start 节点")
    order: list[str] = []
    indeg = {nid: len(incoming[nid]) for nid in nodes}
    queue = [starts[0]["id"]]
    visited: set[str] = set()
    while queue:
        nid = queue.pop(0)
        if nid in visited:
            continue
        visited.add(nid)
        order.append(nid)
        for e in outgoing[nid]:
            indeg[e["target"]] -= 1
            if indeg[e["target"]] <= 0:
                queue.append(e["target"])
    for nid, n in nodes.items():  # 拓扑未覆盖的 gui 容错补入
        if nid not in visited and n.get("type") == "gui":
            order.append(nid)
    steps: list[StepDef] = []
    for nid in order:
        n = nodes[nid]
        if n.get("type") != "gui":
            continue
        try:
            steps.append(StepDef.from_dict(dict(n.get("data") or {})))
        except Exception as e:  # noqa: BLE001
            raise RepeatError("RP-6004", f"gui 节点 {nid} 解析失败: {e}") from e
    if not steps:
        raise RepeatError("RP-6004", "工作流不含任何 gui 步骤")
    return steps


def graph_sha256(graph: dict[str, Any]) -> str:
    payload = json.dumps(graph, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class RepeatRunner:
    """一次回放运行的编排器。平台件由服务层注入（单测可传替身）。"""

    def __init__(self, *, workflow_id: str, graph: dict[str, Any],
                 resolved: dict[str, Any], flags: dict[str, bool], cfg: Any,
                 inspector: Any, injector: Any, windows: Any, env_provider: Any,
                 gate: SafetyGate,
                 run_id: str,
                 reporter: Reporter | None = None,
                 emergency_stop: threading.Event | None = None,
                 bundle_dir: Path | None = None,
                 anchor_loader: Callable[[str], bytes | None] | None = None,
                 frame_loader: Callable[[str], bytes | None] | None = None,
                 run_root: Path | None = None,
                 unattended: bool = False,
                 allow_blind_coord: bool = False,
                 takeover: Any | None = None,
                 checkpoint: dict[str, Any] | None = None) -> None:
        self.workflow_id = workflow_id
        self.graph = graph
        self.vars = dict(resolved or {})
        self.flags = dict(flags or {})
        self.cfg = cfg
        self.inspector = inspector
        self.injector = injector
        self.windows = windows
        self.env_provider = env_provider
        self.gate = gate
        self.run_id = run_id
        self.reporter = reporter or Reporter()
        self.emergency = emergency_stop or threading.Event()
        self.bundle_dir = bundle_dir
        self.anchor_loader = anchor_loader
        self.frame_loader = frame_loader
        self.run_root = run_root
        self.unattended = unattended
        self.allow_blind_coord = allow_blind_coord

        self.steps = steps_from_graph(graph)
        self._run_grabber: Any = None
        try:
            from ..platform import make_grabber
            self._run_grabber = make_grabber()
        except Exception:  # noqa: BLE001
            pass

        self.rec = RunRecord(run_id=run_id, workflow_id=workflow_id, total_steps=len(self.steps),
                             started_at=datetime.now().astimezone().isoformat(timespec="seconds"),
                             vars=self._redacted_vars())
        self._scope: WindowRef | None = None
        self._scope_title_ok = True
        self._takeover = takeover if takeover is not None else HumanTakeover(injector)
        # RunBudget 的入参是**秒**，cfg.run_timeout_min 是**分钟**。
        # 旧实现直接把 30 当秒传进去 → 回放固定 30 秒后 RP-3003 超时
        # （真机坐实：18 步流程两次运行 duration_ms 恰为 30000/31000）。
        self._run_budget = RunBudget(
            int(getattr(cfg, "run_timeout_min", 30) or 30) * 60)
        self._run_retries_left = int(getattr(cfg, "max_retries_per_run", 20) or 20)
        self._suspended: asyncio.Future[str] | None = None
        self._pending_decision: str | None = None
        self._relaxed = False
        self._modal_degraded = False
        self._pending_approval: tuple[str, Decision] | None = None
        self._last_hit: Hit | None = None
        self._last_attempts: list[Attempt] = []
        self._checkpoint_seed = checkpoint or {}

    # ── 对外控制 ─────────────────────────────────────────────────
    def resume(self, decision: str) -> None:
        """人工裁决：retry / skip / abort。

        竞态处理（§5.2 P11/P12）：裁决可能早于 await_resume 创建 future 到达
        （REST 立即返回后前端秒点"跳过"），此时先记入 _pending_decision，
        进入等待时直接消费——否则未来 set 会丢失导致永久挂起。
        """
        if decision not in ("retry", "skip", "abort"):
            return
        # 裁决本身就是一次人工输入：重置接管基线，否则恢复后第一次 detect 会把
        # 用户刚点"重试"的鼠标当成"回放期间的人工干预"，立即再次挂起（死循环）。
        try:
            self._takeover.mark_injected(None)
        except Exception:  # noqa: BLE001
            pass
        fut = self._suspended
        if fut is not None and not fut.done():
            try:
                fut.get_loop().call_soon_threadsafe(fut.set_result, decision)
            except RuntimeError:  # 循环已关
                self._pending_decision = decision
            return
        self._pending_decision = decision

    def abort(self, reason: str = "user") -> None:
        self.emergency.set()
        self.resume("abort")

    # ── 主入口 ───────────────────────────────────────────────────
    async def run(self) -> RunRecord:
        self.rec.status = "precheck"
        try:
            await self._precheck()
        except RepeatError as e:
            # 目标应用未运行 / 环境不可用：**直接失败，不再挂起等人工**。
            # 旧实现在交互模式下挂起并无限等待，用户既看不到运行面板也不知道
            # 该做什么，run 会一直占着输入租约，之后每次回放都被
            # "已有回放进行中"拒绝（真机坐实：run-e0e3d32b76 挂在 precheck，
            # 0/15 步，页面没有任何中止入口）。
            self._finish_failed(e)
            return self.rec
        self.reporter.run_started(self.workflow_id, self.rec.total_steps)
        self.reporter.precheck_done(self.rec.env_level, self.rec.drift)
        self._notice("info", "回放即将开始", "请勿操作鼠标键盘；紧急停止 Ctrl+Alt+.", ["abort"])
        ready = 0 if self.unattended else int(getattr(self.cfg, "pre_run_ready_s", 3) or 0)
        if ready:
            await asyncio.sleep(min(30, ready))
        self.rec.status = "running"
        # 执行起点重置接管基线：启动回放的那次点击（页面按钮）不应被误判为
        # "回放期间人工介入"，否则每一步都会在 detect 时被立刻挂起（真机坐实）。
        try:
            self._takeover.mark_injected(None)
        except Exception:  # noqa: BLE001
            pass
        await self._execute(self._resume_index(), dry=False)
        return self.rec

    async def dry(self) -> DryRunReport:
        """干跑：只解析不注入（§5.2 P06/P18，A7 判据）。"""
        self.rec.status = "dry_run"
        report = DryRunReport(workflow_id=self.workflow_id)
        try:
            await self._precheck(require_app=False)
        except RepeatError:
            report.env_level = self.rec.env_level or "L0"
            report.ok = False
            report.duration_ms = 0
            return report
        report.env_level = self.rec.env_level
        t0 = time.perf_counter()
        for k, step in enumerate(self.steps):
            if self.emergency.is_set():
                report.ok = False
                break
            report.steps.append(await self._dry_one(k, step))
        report.duration_ms = int((time.perf_counter() - t0) * 1000)
        report.ok = all(s.hit_index >= 0 or s.action in ("manual_confirm", "marker")
                        for s in report.steps)
        self.rec.status = "succeeded" if report.ok else "failed"
        self.rec.finished_at = datetime.now().astimezone().isoformat(timespec="seconds")
        return report

    # ── 预检 ─────────────────────────────────────────────────────
    async def _precheck(self, require_app: bool = True) -> None:
        base = Env.from_dict((self.graph.get("meta") or {}).get("env_base"))
        cur = await asyncio.to_thread(self.env_provider.snapshot)
        level = envcheck.classify(base, cur)
        self.rec.env_level = level
        self.rec.drift = envcheck.drift_detail(base, cur, level)
        await asyncio.to_thread(envcheck.graphics_session, self.env_provider)
        if level in ("L2", "L3") and any(
                spec.kind == "coord" and len(step.locators) == 1
                for step in self.steps for spec in step.locators):
            logger.warning("环境漂移 %s：部分步骤仅剩坐标定位，可能触发 RP-2006", level)
        hints = [s.app_hint for s in self.steps if s.app_hint.get("exe")
                 or s.app_hint.get("window_title_regex")]
        if require_app and hints:
            launch_first = self.steps[0].action == "launch_app"
            if not launch_first:
                found = await asyncio.to_thread(
                    lambda: envcheck.app_ready(self.windows, hints[0], timeout_ms=1500))
                if found is None:
                    raise RepeatError("RP-1002",
                                      f"目标应用未运行：{hints[0].get('exe') or hints[0].get('window_title_regex')}",
                                      detail={"hint": hints[0]})

    # ── 执行循环 ─────────────────────────────────────────────────
    async def _execute(self, start_index: int, *, dry: bool) -> None:
        if dry:
            return
        k = start_index
        while k < len(self.steps):
            if self.emergency.is_set():
                self._finish_aborted("RP-7001", "用户紧急停止")
                return
            try:
                self._run_budget.check()
            except RepeatError as e:
                self._finish_failed(e)
                return
            step = self.steps[k]
            outcome = await self._run_step(k, step)
            if outcome is FATAL:
                return
            if outcome is SUSPEND:
                if self.unattended:
                    self._finish_aborted("RP-4004", f"步骤 {step.sid} 需人工介入，无人值守已中止")
                    return
                decision = await self._await_resume(k, step)
                if decision == "abort":
                    return
                # 任何一种裁决都是用户点了一次按钮带回来的：手可能还在鼠标上。
                # 若立刻继续，紧接着的 takeover.detect() 会把这点指针抖动判成
                # "人工介入"再次挂起（真机坐实：点"重试该步"后连续 RP-7002）。
                # 给一个冷静期，并在冷静期结束时重置基线。
                grace = min(2.0, max(0.8, float(
                    getattr(self.cfg, "pre_run_ready_s", 3) or 3) / 2))
                await asyncio.sleep(grace)
                try:
                    self._takeover.mark_injected(None)
                except Exception:  # noqa: BLE001
                    pass
                if decision == "skip":
                    sr = self.rec.steps[-1]
                    sr.status = "skipped"
                    k += 1
                    continue
                # retry
                if self._pending_approval:
                    self.gate.grant(self._pending_approval[0])
                self.rec.status = "running"
                continue
            # NEXT
            self._write_checkpoint(k, step)
            await asyncio.sleep(BETWEEN_STEP_MS / 1000.0)
            if (getattr(self.cfg, "human_takeover_detect", False)
                    and await asyncio.to_thread(self._takeover.detect)):
                self._finish_suspended(step.sid, "RP-7002", "检测到人工介入，已暂停")
                decision = await self._await_resume(k + 1, self.steps[k + 1] if k + 1 < len(self.steps) else None)
                if decision == "abort":
                    return
                self.rec.status = "running"
                self.reporter.run_resumed(decision)
            k += 1
        self._finish_succeeded()

    async def _run_step(self, k: int, step: StepDef) -> str:
        # 同一步骤可能因"重试/人工裁决"多次进入：复用既有 StepRun，而不是再 append
        # 一行。否则时间线与统计出现重复行（真机截图坐实：同一步骤重复 5 行）。
        sr = next((x for x in self.rec.steps if x.sid == step.sid), None)
        if sr is None:
            sr = StepRun(sid=step.sid, index=k, action=step.action, status="running")
            self.rec.steps.append(sr)
        else:
            sr.index = k
            sr.action = step.action
            sr.status = "running"
            sr.hit_index = -1
            sr.hit_kind = ""
            sr.code = ""
            sr.message = ""
            sr.degraded = False
            sr.blind = False
            sr.unstable = False
            sr.elapsed_ms = 0
        sr_start = time.perf_counter()
        # 放宽标记只属于"触发它的那一步的重试周期"：进入新步骤即复位。
        # 历史 bug：run 级常驻，后续所有步骤的 name_type 匹配都按编辑距离≤2
        # 放宽执行，"保存/保存副本"类近名控件极易被抢先命中且记录显示 uia@0
        # （体检 P0-5，越用越错）。
        self._relaxed = False
        # 模态降级标记（作用域被禁用但没能接管到同进程模态面）：本步定位时
        # 禁用盲坐标兜底，避免隔着模态框凭坐标乱点。
        self._modal_degraded = False
        step_timeout = step.timing.get("step_timeout_ms") or getattr(self.cfg, "step_timeout_ms", 12000)
        budget = StepBudget(int(step_timeout or 12000))
        self.reporter.step_started(step.sid, k, step.action, self._render(step.label))
        # manual_confirm / marker 短路
        if step.action == "manual_confirm":
            sr.status = "suspended"
            sr.code = "RP-4004"
            sr.message = str(step.params.get("prompt") or "此步骤需人工操作（密码/验证码）")
            self._finish_suspended(step.sid, "RP-4004", sr.message)
            return SUSPEND
        if step.action == "marker":
            sr.status = "done"
            self.reporter.step_done(step.sid, "done", -1, "marker", 0)
            return NEXT
        # 1) 作用域窗口
        try:
            await self._ensure_scope(step)
            if self._scope is not None:
                envcheck.check_elevation(self.env_provider, self.windows, self._scope)
        except RepeatError as e:
            return await self._error_path(step, sr, e)
        # 2) 弹窗遮挡（场景 8 轻量）
        if self._modal_blocked(step):
            return await self._error_path(step, sr,
                                          RepeatError("RP-1007", "检测到弹窗/模态框遮挡",
                                                      step_sid=step.sid))
        # 3) resolve + 失败阶梯
        hit: Hit | None = None
        attempts = 0
        guard = 0
        while True:
            guard += 1
            if guard > 24:  # 防御：任何情况下不允许无界循环
                raise RepeatError("RP-7003", "定位循环异常（内部保险丝熔断）", step_sid=step.sid)
            try:
                hit = await self._resolve(step, budget)
                break
            except RepeatError as e:
                # 键入步骤没有真正的输入控件锚点（Chrome 单 HWND）：页面标题一变
                # （百度首页 → 结果页）定位链必然全丢。键入的本质是"向当前焦点
                # 发送键盘"，这里降级为直接输入，而不是让整轮回放失败。
                # 热键（hotkey）本质是"向当前焦点注入按键"：act_hotkey 完全不使用
                # hit 元素，定位链只是一个"会失败的门闩"。一旦录制层把回车绑到
                # "该回车按下之后才出现的页面"上（真机 run-092d827a9a s09：回车
                # 锚定 Dify 根 Pane），就会出现"先定位到页面才允许按键，而页面要
                # 等按键才存在"的因果倒置死锁，重试多少次都不可能成功。降级为焦点
                # 注入——作用域窗口仍由 _ensure_scope 保证，语义不变。
                if e.code in ("RP-2001", "RP-2002") and step.action == "hotkey":
                    logger.info("repeat: %s 热键步骤定位链未命中，降级为当前焦点注入", step.sid)
                    hit = self._focus_hit(step)
                    break
                if e.code in ("RP-2001", "RP-2002") and (
                        _type_anchor_is_weak(step) or _type_timeout_unproven(step, e)):
                    logger.info("repeat: %s 键入步骤无有效控件锚点（或 UIA 超时未确证不存在），"
                                "降级为当前焦点输入", step.sid)
                    hit = self._focus_hit(step)
                    break
                next_move = classify_ladder(
                    e, step=step, run_level=self.rec.env_level,
                    attempts=attempts,
                    retries_left=int(step.policy.get("retries", getattr(self.cfg, "retries", 2))),
                    run_retries_left=self._run_retries_left,
                    on_fail=step.policy.get("on_fail", "suspend_manual"),
                    has_coord=any(spec.kind == "coord" for spec in step.locators))
                if next_move.relaxed:
                    self._relaxed = True
                if next_move.action == "retry":
                    attempts += 1
                    self._run_retries_left = max(0, self._run_retries_left - 1)
                    self.rec.attempts_total += 1
                    sr.attempts.extend(self._last_attempts)
                    self.reporter.step_retry(step.sid, attempts, next_move.reason)
                    await asyncio.sleep(min(3.0, next_move.backoff_ms / 1000.0))
                    continue
                # continue（RP-3004 标记继续）：视为已命中兜底路径不可用，走 error_path
                return await self._error_path(step, sr, e)
        if hit is None:
            return await self._error_path(step, sr,
                                          RepeatError("RP-2001", "定位失败", step_sid=step.sid))
        self._correct_hit_with_recorded_coord(step, hit)
        sr.attempts.extend(self._last_attempts)
        sr.hit_index = int(hit.diagnostics.get("index", 0))
        sr.hit_kind = hit.kind
        sr.degraded = sr.hit_index > 0
        if bool(hit.diagnostics.get("blind")):
            sr.blind = True
            self.rec.blind_used = True
        self._last_hit = hit
        # 4) pre_wait
        try:
            await self._pre_wait(step, budget)
        except RepeatError as e:
            return await self._error_path(step, sr, e)
        # 5) 安全门（唯一出口）
        d = self.gate.check(run_id=self.run_id, sid=step.sid, action=step.action,
                            risk=step.risk, app_hint=step.app_hint,
                            params=self._render_params(step.params),
                            scope_ok=self._scope_ok(step),
                            # 命中可信度决定是否需要人工确认：首级 UIA 命中直接放行
                            hit_kind=str(hit.kind or ""),
                            hit_index=int(hit.diagnostics.get("index", 0) or 0),
                            degraded=bool(sr.degraded),
                            step_index=k)
        if not d.allow:
            if d.pending_approval:
                self._pending_approval = (d.approval_token, d)
                sr.status = "suspended"
                sr.code = d.code or "RP-4003"
                sr.message = d.reason
                self._finish_suspended(step.sid, sr.code, d.reason,
                                       approval_token=d.approval_token)
                return SUSPEND
            return await self._error_path(step, sr,
                                          RepeatError(d.code or "RP-4002", d.reason,
                                                      step_sid=step.sid))
        # 6) act
        act_res: actions_mod.ActionResult | None = None
        try:
            # 单步硬超时（体检 P2-2）：StepBudget 此前只在 resolve_chain 内经
            # left_ms 间接兜住，act/assert/settle 阶段无人调 budget.check，
            # timing.step_timeout_ms 形同虚设。定位/门控已耗掉的时间在这里收敛，
            # 越界即 RP-3003 走失败阶梯（可重试瞬态）。
            budget.check("执行前")
            act_res = await self._act(step, hit, budget)
        except RepeatError as e:
            return await self._error_path(step, sr, e)
        if act_res is not None and act_res.readback_mismatch:
            # 输入已执行但读回不可靠：记降级 + 明确提示，不中断回放
            sr.degraded = True
            sr.message = act_res.message
            self._notice("info", "输入已执行，但未能读回确认",
                         f"{step.sid}：{act_res.message}")
        # 7) settle + assert
        await self._settle(step, budget)
        ok_assert = await self._assert(step, hit)
        if not ok_assert and (step.assertion or {}).get("optional") is not True:
            return await self._error_path(step, sr, RepeatError("RP-3002", "断言失败",
                                                                step_sid=step.sid))
        if not ok_assert:
            sr.unstable = True
        sr.status = "degraded" if sr.degraded else "done"
        sr.elapsed_ms = int((time.perf_counter() - sr_start) * 1000)
        self.reporter.step_done(step.sid, sr.status, sr.hit_index, sr.hit_kind, sr.elapsed_ms)
        return NEXT

    async def _error_path(self, step: StepDef, sr: StepRun, e: RepeatError) -> str:
        sr.status = "failed"
        sr.code, sr.message = e.code, e.message
        self.reporter.step_failed(step.sid, e.code, e.message)
        on_fail = step.policy.get("on_fail", "suspend_manual")
        if on_fail == "skip":
            sr.status = "skipped"
            return NEXT
        if e.code in ("RP-4002", "RP-4003", "RP-4001", "RP-1001", "RP-1006") or on_fail == "abort":
            self._finish_failed(e)
            return FATAL
        diag = self._diagnose(step, sr, e)
        self._finish_suspended(step.sid, e.code, e.message, diag)
        return SUSPEND

    # ── 子阶段 ───────────────────────────────────────────────────
    async def _ensure_scope(self, step: StepDef) -> None:
        hint = dict(step.app_hint or {})
        if not hint:
            return
        # 旧实现只比 exe：同一进程的多个窗口（Chrome 多窗口/多标签、Office 多文档）
        # 会一直复用第一次找到的窗口，后续步骤在**错误窗口**里"命中"同名控件。
        # 真机坐实：百度流程全程操作 Andy-Harness 窗口，点击命中聊天框、文字也
        # 输入到聊天框，最后报 succeeded 但百度毫无反应。
        if self._scope is not None and _window_matches(self._scope, hint):
            self._scope_title_ok = True
            return
        found = await asyncio.to_thread(self.windows.find_window, hint)
        # cls 是比标题更强的身份证据（编译期取自真实目标窗口）。find_window 的
        # 级别顺序把 exe+title 排在 exe+class 之前，而主窗口标题常常恰好也匹配
        # （"EditPlus - [...]" 命中对话框 hint 的 .*EditPlus.*），于是作用域被设成
        # 主窗口而不是对话框。cls 不符时按 cls 精确重找一次。
        if found is not None and str(hint.get("cls", "") or "") and \
                str(getattr(found, "cls", "") or "") != str(hint["cls"]):
            exact = await self._find_by_cls(hint)
            if exact is not None:
                found = exact
        if found is None or not _window_matches(found, hint):
            # 新窗口（保存/另存为等模态对话框）从点击到可枚举有几百毫秒延迟，
            # 紧随其后的步骤立即枚举必扑空——而 find_window 的 exe 兜底会"贴心地"
            # 返回旧窗口，于是扑空表现为"命中了不满足 hint 的窗口"而不是 None。
            # 有明确目标（标题/类名）时做一次有界等待。
            found = await self._wait_window_appear(hint, found)
        if found is None:
            # 同一窗口内的页面跳转会让标题变化（百度首页 → 结果页），按新标题
            # 找不到窗口是正常的。此时若**前台窗口就是当前作用域窗口**，说明只是
            # 标题尚未更新，直接放行；否则标记标题未确认，交给 _scope_ok 决定。
            if self._scope is not None:
                try:
                    fg = self.windows.foreground()
                except Exception:  # noqa: BLE001
                    fg = None
                if fg is not None and getattr(fg, "hwnd", 0) == self._scope.hwnd:
                    self._scope_title_ok = True
                    return
                # 前台已切到同进程的另一个顶层窗口（新弹出的模态对话框，或标题
                # 漂移后的同一窗口）：守着旧作用域会让下一步被 _modal_blocked
                # 判成遮挡 → RP-1007 死锁。接管前台，标题是否仍按正则校验。
                if fg is not None and hint.get("exe") and \
                        str(getattr(fg, "exe", "") or "").lower() == str(hint["exe"]).lower():
                    logger.info("repeat: 目标窗口未出现，接管同进程前台窗口「%s」",
                                getattr(fg, "title", ""))
                    self._scope = fg
                    self._scope_title_ok = self._title_ok(fg, hint)
                    return
                if hint.get("exe") and \
                        str(self._scope.exe).lower() == str(hint["exe"]).lower():
                    self._scope_title_ok = False
            return
        if found.minimized:
            await asyncio.to_thread(self.windows.restore, found)
        if self._scope is None or found.hwnd != self._scope.hwnd:
            # 只有换窗口才抢焦点；同一窗口不重复 SetForegroundWindow
            try:
                await asyncio.to_thread(self.windows.focus, found)
            except Exception:  # noqa: BLE001
                pass
        # 标题确认（体检 P0-4）：find_window 四级放宽 miss 后会按 exe 兜底返回
        # "任一可见顶层窗口"，若不加校验直接 _scope_title_ok=True，就是"假确认"
        # ——错误窗口里的写操作会被 _scope_ok 放行（与既往"18/18 succeeded 但
        # 应用无反应"同链路）。兜底命中且标题明显不符时：若就是当前作用域窗口
        # （同窗口页面跳转，标题变了），仍可确认；否则标未确认，写操作按契约拒绝。
        title_ok = self._title_ok(found, hint)
        if (not title_ok and self._scope is not None
                and found.hwnd == self._scope.hwnd):
            title_ok = True
        self._scope = found
        self._scope_title_ok = title_ok

    @staticmethod
    def _title_ok(win: Any, hint: dict[str, Any]) -> bool:
        """命中窗口的标题是否可确认为 hint 所指的窗口。

        P0-4 的严格校验在此保留（exe 兜底命中无关窗口仍判未确认）；额外接受
        **类名精确命中**这一强证据：编译期的 cls 取自真实目标窗口，而对话框
        标题会随内部焦点漂移（真机 EditPlus「另存为」↔「EditPlus」，两者
        cls 同为 #32770）。此时标题不符属于"同一窗口改名"，不是找错窗口。
        """
        pat = str(hint.get("window_title_regex", "") or "")
        if not pat:
            return True
        try:
            if re.search(pat, str(getattr(win, "title", "") or "")):
                return True
        except re.error:
            return True  # 非法正则不据此拒绝（与 find_window 行为一致）
        cls = str(hint.get("cls", "") or "")
        return bool(cls) and str(getattr(win, "cls", "") or "") == cls

    async def _find_by_cls(self, hint: dict[str, Any]) -> Any | None:
        """按 (exe, cls) 精确重找目标窗口（忽略可能漂移的标题正则）。"""
        h2 = {k: v for k, v in hint.items() if k != "window_title_regex"}
        try:
            return await asyncio.to_thread(self.windows.find_window, h2)
        except Exception:  # noqa: BLE001
            return None

    async def _wait_window_appear(self, hint: dict[str, Any],
                                  fallback: Any | None = None) -> Any | None:
        """目标窗口尚未出现时的有界等待（默认 2.5s，200ms 一探）。

        仅当 app_hint 给出明确目标（标题正则或类名）时才等待，避免把"窗口
        当真不存在"的场景也拖慢。等待期间同样接受 cls 精确命中（标题漂移）。
        """
        pat = str(hint.get("window_title_regex", "") or "")
        cls = str(hint.get("cls", "") or "")
        if not (pat or cls):
            return fallback
        budget_ms = int(getattr(self.cfg, "window_appear_wait_ms", 2500) or 0)
        if budget_ms <= 0:
            return fallback
        waited = 0
        while waited < budget_ms:
            await asyncio.sleep(0.2)
            waited += 200
            try:
                found = self.windows.find_window(hint)
            except Exception:  # noqa: BLE001
                found = None
            if found is None:
                continue
            if cls and str(getattr(found, "cls", "") or "") != cls:
                exact = await self._find_by_cls(hint)
                if exact is not None:
                    found = exact
            if _window_matches(found, hint) or (cls and str(getattr(found, "cls", "") or "") == cls):
                logger.info("repeat: 等待 %dms 后目标窗口出现（%s）", waited,
                            getattr(found, "title", ""))
                return found
        return fallback

    def _scope_ok(self, step: StepDef) -> bool:
        hint = step.app_hint or {}
        if not hint:
            return True
        if self._scope is None:
            return step.risk != "dangerous"
        if hint.get("exe") and str(self._scope.exe).lower() != str(hint["exe"]).lower():
            return False
        # 标题未能确认（同 exe 但找不到匹配标题）：不允许写操作，
        # 避免"在错误窗口里点了别的控件还以为成功"。
        # 注意**不校验 cls**：同一应用的主窗口/文档窗口/对话框 cls 经常不同
        # （WPS：OpusApp vs 编辑器窗口），按 cls 拒绝会连续误杀（真机坐实：
        # 6 次回放全部 RP-4002 停在第 3 步）。
        if (not self._scope_title_ok and hint.get("window_title_regex")
                and step.risk != "read"):
            return False
        return True

    def _modal_blocked(self, step: StepDef | None = None) -> bool:
        """是否处于"无法操作的模态遮挡"状态。

        旧实现只看"作用域窗口被禁用"就一律判遮挡，于是形成**死锁**：模态
        对话框弹出 → 主窗口被系统 Disable → 每一步都在主窗口作用域下被
        RP-1007 拒绝，而"切到对话框继续操作"这一步本身也被拒绝（真机
        EditPlus 点保存 → 另存为对话框 → s11 window_focus 卡死）。

        正确语义：作用域被禁用只说明"存在模态面"，此时
          a) window_focus / wait_window 等**切作用域**动作必须放行——
             它正是进入模态面的手段；
          b) 前台已切到同进程的另一个顶层窗口（对话框）→ 接管为新作用域，
             让定位/点击在模态面内正常进行；
          c) 只有既切不进去、也找不到可接管的模态面时，才是真遮挡。
        """
        if self._scope is None:
            return False
        try:
            enabled = self.windows.is_enabled(self._scope.hwnd)
        except Exception:  # noqa: BLE001
            return False
        if enabled:
            return False
        fg = self._safe_foreground()
        logger.warning(
            "repeat: 作用域窗口被禁用（存在模态面）scope=「%s」/%s；"
            "前台=「%s」/%s；步骤=%s",
            getattr(self._scope, "title", ""), getattr(self._scope, "exe", ""),
            getattr(fg, "title", "") if fg else None,
            getattr(fg, "exe", "") if fg else None,
            getattr(step, "action", ""))
        # a) window_focus / wait_window 等**切作用域**动作必须放行：它正是进入
        #    模态面的手段（先尝试接管，否则聚焦的仍是被禁用的旧窗口）。
        if step is not None and step.action in self._FOCUS_ACTIONS:
            self._adopt_modal_window(step)
            return False
        # b) 前台已切到同进程的另一个顶层窗口（对话框）→ 接管为新作用域。
        if self._adopt_modal_window(step):
            return False
        # c) 接管不了（前台是别的进程——回放时常常是 andy-harness 自己的窗口，
        #    因为用户刚点过「回放」按钮）：**不再 RP-1007 挂起**，改为降级放行，
        #    让定位链在作用域子树里实打实地搜——UIA 树里模态对话框通常是主窗口的
        #    子节点（真机 structure_path：Window[EditPlus]/Window[另存为]/Button[保存(S)]），
        #    能命中就执行，命中不到才是 RP-2001 且诊断包留全证据。
        #    护栏：降级期间禁用"盲坐标兜底"，避免隔着模态框凭坐标乱点。
        if fg is None:
            return True  # 连前台都取不到（UAC 安全桌面之类）→ 才是真遮挡
        self._modal_degraded = True
        logger.warning("repeat: 未接管到同进程模态面，降级放行（禁用盲坐标兜底）")
        return False

    def _safe_foreground(self) -> Any | None:
        try:
            return self.windows.foreground()
        except Exception:  # noqa: BLE001
            return None

    def _adopt_modal_window(self, step: StepDef | None = None) -> bool:
        """把作用域从"被禁用的旧窗口"切到当前前台的模态面（同进程）。

        只接管**同进程**的前台窗口：跨进程窗口（回放时前台常是 andy-harness
        自己）不是目标应用的操作面，接管它会把后续点击带进错误窗口。
        返回 True 表示已接管（调用方继续执行）。
        """
        fg = self._safe_foreground()
        if fg is None or not getattr(fg, "hwnd", 0):
            return False
        if self._scope is None or fg.hwnd == self._scope.hwnd:
            return False
        same_pid = (getattr(fg, "pid", 0) and getattr(self._scope, "pid", 0)
                    and fg.pid == self._scope.pid)
        scope_exe = str(getattr(self._scope, "exe", "") or "").lower()
        same_exe = bool(scope_exe) and str(getattr(fg, "exe", "") or "").lower() == scope_exe
        if not (same_pid or same_exe):
            return False
        hint = dict(getattr(step, "app_hint", None) or {})
        logger.info("repeat: 作用域窗口被禁用，接管同进程模态面「%s」（%s）",
                    getattr(fg, "title", ""), getattr(fg, "cls", ""))
        self._scope = fg
        # 前台即用户可见的操作面；有 hint 时仍按其标题/类名校验，
        # 无 hint（纯切窗口）则直接确认。
        self._scope_title_ok = self._title_ok(fg, hint) if hint else True
        return True

    # 焦点级动作：不需要元素定位链，作用于「当前焦点控件 / 已解析作用窗口」（§6.3）。
    _FOCUS_ACTIONS = frozenset({"hotkey", "window_focus", "wait_window", "wait_element",
                                "assert_element", "assert_text", "launch_app"})

    @staticmethod
    def _recorded_coord(step: StepDef) -> tuple[int, int] | None:
        """录制时该动作的真实点击坐标（来自 coord 定位器）。"""
        for spec in getattr(step, "locators", []) or []:
            if getattr(spec, "kind", "") == "coord":
                pos = getattr(spec, "pos", None)
                if pos and len(pos) >= 2:
                    return (int(pos[0]), int(pos[1]))
        return None

    def _correct_hit_with_recorded_coord(self, step: StepDef, hit: Hit) -> None:
        """UIA 命中的是容器时，用录制坐标而不是元素中心点击。

        典型场景：工具栏里的「新建」按钮。UIA 往往只把整个工具栏暴露成一个
        Pane，用户点的是工具栏最左侧的按钮；若按元素中心点击，会落到工具栏
        中间的其他按钮上（真机坐实：EditPlus 新建文档，回放点了工具栏中心，
        根本没有新建）。
        判定：录制坐标与元素中心的距离超过"元素短边 30%（至少 24px）"即视为
        用户点的是元素内的具体位置。

        注意**不要求坐标落在元素 rect 内**：UIA 快照完全可能抓到别的元素
        （模态对话框弹出时 ElementFromPoint 常返回背后内容），此时坐标才是
        唯一可信的位置（真机坐实：EditPlus 点「保存」按钮，UIA 命中的是背后
        的文档区，按中心点击等于点文档，保存永远不会发生）。
        """
        if step.action not in ("click", "double_click", "right_click", "drag", "type"):
            return
        if not hit.rect or hit.rect[2] <= hit.rect[0] or hit.rect[3] <= hit.rect[1]:
            return
        pos = self._recorded_coord(step)
        if pos is None:
            return
        cx, cy = hit.center
        dist = max(abs(pos[0] - cx), abs(pos[1] - cy))
        short_side = min(hit.rect[2] - hit.rect[0], hit.rect[3] - hit.rect[1])
        if dist <= max(24.0, short_side * 0.3):
            return
        hit.center = pos
        hit.kind = "coord"
        hit.diagnostics["coord_corrected"] = True
        logger.info("repeat: %s UIA 命中容器，改用录制坐标 %s（元素中心 %s，偏差 %dpx）",
                    step.sid, pos, (cx, cy), dist)

    def _focus_hit(self, step: StepDef) -> Hit:
        """焦点级步骤的合成命中：无元素句柄，act_* 走 unicode 注入 / 窗口聚焦分支。"""
        win = self._scope
        if win is not None and win.rect[2] > win.rect[0]:
            wl, wt, wr, wb = win.rect
            cx, cy = (wl + wr) // 2, (wt + wb) // 2
            rect = (wl, wt, wr, wb)
        else:
            cx, cy, rect = 0, 0, (0, 0, 0, 0)
        return Hit(rect=rect, center=(cx, cy), element=None,
                   kind="focus", score=0.0, elapsed_ms=0,
                   diagnostics={"index": 0, "focus_level": True})

    async def _resolve(self, step: StepDef, budget: StepBudget) -> Hit:
        from ..locators import ExecContext, resolve_chain
        specs = list(step.locators)
        # 焦点级：type 无定位链（有作用窗口即可）、hotkey/window_focus 等本就无元素定位
        # → 直接合成焦点命中，交由 act_* 走「当前焦点控件 / 窗口聚焦」路径，而非判 RP-2001。
        if not specs and (step.action in self._FOCUS_ACTIONS
                          or (step.action == "type" and bool(step.app_hint))):
            return self._focus_hit(step)
        if self._relaxed:
            for spec in specs:
                if spec.kind == "uia":
                    spec.relaxed = True
        ctx = ExecContext(
            inspector=self.inspector,
            grabber=self._run_grabber,
            windows=self.windows,
            scope_win=self._scope,
            env_level=self.rec.env_level,
            dry_run=False,
            # 模态降级期间：只认 UIA/图像等"确证命中"，不用坐标兜底
            allow_blind_coord=self.allow_blind_coord and not self._modal_degraded,
            coord_gate=getattr(self.cfg, "coord_gate", True),
            anchors={},
            bundle_dir=str(self.bundle_dir) if self.bundle_dir else None,
        )
        hit, misses, _elapsed = await asyncio.to_thread(
            lambda: resolve_chain(ctx, specs, budget_ms=budget.left_ms() or 6000))
        # 尝试记录：misses 原样入册；命中项补记（含链内序号，供命中率统计）
        attempts = [Attempt(kind=m.kind, by=str(m.diagnostics.get("by", "")), result="miss",
                             reason=m.reason, elapsed_ms=int(m.elapsed_ms or 0))
                    for m in misses]
        if hit is not None:
            idx = int(hit.diagnostics.get("index", -1) or 0)
            spec_hit = specs[idx] if 0 <= idx < len(specs) else hit
            attempts.append(Attempt(kind=hit.kind, by=str(getattr(spec_hit, "by", "") or ""),
                                    result="hit", elapsed_ms=int(hit.elapsed_ms or 0),
                                    score=float(hit.score)))
        self._last_attempts = attempts
        if hit is None:
            gated = any(m.reason == "env_gated" for m in misses)
            raise RepeatError("RP-2006" if gated else "RP-2001",
                              "环境已变化，坐标兜底被禁用" if gated else "目标未找到（定位链耗尽）",
                              step_sid=step.sid,
                              detail={"misses": [{"kind": m.kind, "reason": m.reason} for m in misses]})
        return hit

    async def _pre_wait(self, step: StepDef, budget: StepBudget) -> None:
        pw = step.timing.get("pre_wait") or {}
        if not pw:
            return
        min_delay = int(pw.get("min_delay_ms", 0) or 0)
        if min_delay:
            await asyncio.sleep(min(1.0, min_delay / 1000.0))
        kind = str(pw.get("kind", ""))
        if kind == "element_appear":
            # 元素可用性复查（RP-2005：disabled/offscreen 等 2s 再判）
            if self._last_hit is not None and self._last_hit.element is not None:
                snap = None
                try:
                    snap = await asyncio.to_thread(self.inspector.snapshot_of_token,
                                                   self._last_hit.element, 600)
                except Exception:  # noqa: BLE001
                    snap = None
                if snap is not None and (not snap.is_enabled or snap.is_offscreen):
                    deadline = budget.clamp_ms(2000)
                    waited = 0.0
                    while waited < deadline / 1000.0:
                        await asyncio.sleep(0.2)
                        waited += 0.2
                        try:
                            snap = await asyncio.to_thread(self.inspector.snapshot_of_token,
                                                           self._last_hit.element, 600)
                        except Exception:  # noqa: BLE001
                            snap = None
                        if snap is not None and snap.is_enabled and not snap.is_offscreen:
                            return
                    raise RepeatError("RP-2005", "控件当前不可用", step_sid=step.sid)

    async def _act(self, step: StepDef, hit: Hit, budget: StepBudget) -> actions_mod.ActionResult:
        ctx = actions_mod.ActionContext(
            inspector=self.inspector, injector=self.injector,
            windows=self.windows, grabber=self._run_grabber,
            budget=budget, dry_run=False,
            mark_injected=lambda pos: self._takeover.mark_injected(pos),
        )
        step_for_act = self._with_rendered_params(step)
        res = await asyncio.to_thread(actions_mod.run_action, ctx, step_for_act, hit)
        if step.action == "window_focus":
            # Windows 前台锁会让 SetForegroundWindow 失败，focus() 退化为
            # keybd_event(VK_MENU) 模拟 Alt 键解锁。这会刷新系统"最后输入时间"，
            # 但显然是回放自身行为，不是人操作。不重置基线的话，紧接着的
            # takeover.detect() 会把这一步判成"人工介入"并挂起（真机坐实）。
            self._takeover.mark_injected(None)
        if not res.ok and res.channel != "dry":
            raise RepeatError("RP-5003" if step.action == "type" else "RP-5001",
                              res.message or f"{step.action} 执行失败", step_sid=step.sid)
        if step.action == "type" and res.readback is not None:
            want = self._render(str(step.params.get("value", "")))
            if res.readback != want:
                # 读回不一致 ≠ 输入失败。Chrome 地址栏（omnibox）的 SetValue 常被拒，
                # 实际输入靠剪贴板/unicode 通道完成，而 ValuePattern 只读、读回不可靠。
                # 旧实现直接 RP-5003 挂起整轮（真机坐实：地址栏已成功输入 dify，
                # 却报"输入内容与预期不一致"并停止）。现在只标记降级，让流程继续。
                res.readback_mismatch = True
                res.message = (f"输入已执行，但未能读回确认（期望 {len(want)} 字符，"
                               f"读回 {len(res.readback)} 字符）")
                logger.info("repeat: %s type 读回不一致，降级继续（通道 %s）",
                            step.sid, res.channel)
        return res

    async def _settle(self, step: StepDef, budget: StepBudget) -> None:
        settle = int(step.timing.get("settle_ms", SETTLE_DEFAULT_MS) or SETTLE_DEFAULT_MS)
        if settle:
            await asyncio.sleep(min(2.0, settle / 1000.0))

    async def _assert(self, step: StepDef, hit: Hit) -> bool:
        spec = step.assertion
        if not spec:
            return True
        kind = str(spec.get("kind", "") or "")
        if not kind:
            return True
        spec = {k2: (self._render(str(v)) if isinstance(v, str) and "{{" in v else v)
                for k2, v in spec.items()}
        actx = AssertCtx(self.inspector, self.windows)
        res = await asyncio.to_thread(check_assert, kind, actx, spec, hit, step)
        return res.ok

    async def _dry_one(self, k: int, step: StepDef) -> DryRunStep:
        ds = DryRunStep(sid=step.sid, index=k, label=self._render(step.label), action=step.action)
        if step.action in ("manual_confirm", "marker"):
            ds.hit_index, ds.hit_kind, ds.message = 0, step.action, f"{step.action}（干跑跳过注入）"
            return ds
        budget = StepBudget(6000)
        try:
            await self._ensure_scope(step)
            hit = await self._resolve(step, budget)
            ds.hit_index = int(hit.diagnostics.get("index", 0))
            ds.hit_kind = hit.kind
            ds.elapsed_ms = int(hit.elapsed_ms)
        except RepeatError as e:
            # 与真实回放保持一致：弱锚点 type 会降级为"当前焦点输入"，
            # 干跑若仍报 miss，会误判整条流程不可用（真机实录：s07 type）。
            if e.code in ("RP-2001", "RP-2002") and (
                    _type_anchor_is_weak(step) or _type_timeout_unproven(step, e)):
                ds.hit_index, ds.hit_kind = 0, "focus"
                ds.message = "无有效控件锚点，回放时直接向当前焦点输入"
            elif e.code in ("RP-2001", "RP-2002") and step.action == "hotkey":
                # 与真实回放一致（真机 s09）：热键不依赖元素锚点，注入到当前焦点
                ds.hit_index, ds.hit_kind = 0, "focus"
                ds.message = "热键直接向当前焦点注入（不依赖元素锚点）"
            else:
                ds.message = f"{e.code} {e.message}"
        return ds

    async def _await_resume(self, k: int, step: StepDef | None) -> str:
        # 竞态收口（§5.2 P11/P12）：resume() 可能在"检查 pending"与"挂 future"之间
        # 到达——先挂 future 再二次检查 pending，裁决永不丢失。
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self._suspended = fut
        timed_out = False
        if self._pending_decision is not None:
            decision = self._pending_decision
            self._pending_decision = None
            self._suspended = None
        else:
            # 挂起必须有上限：用户关掉页面/离开后若无限等待，输入租约永不释放，
            # 后续所有回放都会被"已有回放进行中"拒绝（真机坐实）。
            timeout = float(getattr(self.cfg, "suspend_timeout_s", 0) or 0)
            try:
                decision = await asyncio.wait_for(asyncio.shield(fut), timeout=timeout) \
                    if timeout > 0 else await fut
            except asyncio.TimeoutError:
                timed_out = True
                decision = "abort"
            except asyncio.CancelledError:
                decision = "abort"
            finally:
                self._suspended = None
        if decision == "abort":
            if timed_out:
                self._finish_aborted("RP-4005",
                                     f"等待人工裁决超过 {int(timeout)} 秒，已自动中止")
            else:
                self._finish_aborted("RP-7001", "用户中止")
            return "abort"
        self.reporter.run_resumed(decision)
        return decision

    # ── 参数渲染 / 脱敏 ──────────────────────────────────────────
    def _render(self, text: str) -> str:
        from .params import render_placeholders
        return render_placeholders(str(text or ""), self.vars, self.flags)

    def _render_params(self, params: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for k, v in (params or {}).items():
            out[k] = self._render(str(v)) if k == "value" else v
        return out

    def _with_rendered_params(self, step: StepDef) -> StepDef:
        cp = StepDef.from_dict(step.to_dict())
        if "value" in cp.params:
            cp.params["value"] = self._render(str(cp.params["value"]))
        if "combo" in cp.params:
            cp.params["combo"] = self._render(str(cp.params["combo"]))
        cp.evidence = dict(cp.evidence)
        if self._scope is not None:
            cp.evidence["_resolved_win"] = self._scope
        if self._last_hit is not None:
            cp.evidence["_drag_path"] = self._last_hit.diagnostics.get("path") or []
        return cp

    def _mask(self, value: str) -> str:
        sensitive_vals = {str(v) for k, v in self.vars.items() if self.flags.get(k)}
        if value in sensitive_vals:
            return "***"
        return value[:60]

    def _redacted_vars(self) -> dict[str, Any]:
        from .params import redact_vars
        return redact_vars(self.vars, self.flags)

    # ── 检查点 / 诊断 ────────────────────────────────────────────
    def _write_checkpoint(self, k: int, step: StepDef) -> None:
        cp = {"run_id": self.run_id, "workflow_id": self.workflow_id,
              "graph_sha256": graph_sha256(self.graph),
              "completed": [s.sid for s in self.steps[:k + 1]],
              "current": self.steps[k + 1].sid if k + 1 < len(self.steps) else "",
              "vars": self._redacted_vars(),
              "degraded": any(s.degraded for s in self.rec.steps),
              "attempts_total": self.rec.attempts_total,
              "updated_at": datetime.now().astimezone().isoformat(timespec="seconds")}
        self.rec.checkpoint = cp
        if self.run_root is not None:
            try:
                rtdir = Path(self.run_root) / "runtime"
                rtdir.mkdir(parents=True, exist_ok=True)
                (rtdir / "checkpoint.json").write_text(
                    json.dumps(cp, ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception:  # noqa: BLE001
                pass

    def _diagnose(self, step: StepDef, sr: StepRun, e: RepeatError) -> str:
        if self.run_root is None:
            return ""
        try:
            anchor_bytes = None
            if step.evidence.get("anchor") and self.anchor_loader:
                anchor_bytes = self.anchor_loader(str(step.evidence["anchor"]))
            expected_frame = ""
            expected_frame = str((step.evidence.get("frames") or [""])[0])
            return build_diagnostic_package(
                Path(self.run_root), self.run_id, step, e,
                grabber=self._run_grabber or _NullGrabber(),
                windows=self.windows,
                hits=[a.to_dict() for a in sr.attempts],
                anchor_bytes=anchor_bytes, expected_frame=str(expected_frame))
        except Exception as exc:  # noqa: BLE001
            logger.warning("诊断包生成失败: %s", exc)
            return ""

    # ── 收口 ─────────────────────────────────────────────────────
    def _finish_succeeded(self) -> None:
        self.rec.status = "succeeded"
        self.rec.finished_at = datetime.now().astimezone().isoformat(timespec="seconds")
        self.rec.duration_ms = self._elapsed_ms()
        st = self.rec.stats()
        self.reporter.run_done(self.rec.to_dict())
        self._notice("info", "回放完成",
                     f"成功 {st['done']}｜降级 {st['degraded']}｜耗时 {st['duration_ms'] // 1000}s")

    def _finish_failed(self, e: RepeatError) -> None:
        self.rec.status = "failed"
        self.rec.error_code, self.rec.error_message = e.code, e.message
        self.rec.finished_at = datetime.now().astimezone().isoformat(timespec="seconds")
        self.rec.duration_ms = self._elapsed_ms()
        self.reporter.run_error(e.code, e.message)
        self._notice("error", "回放失败", f"{e.code} {e.message}")

    def _finish_aborted(self, code: str, msg: str) -> None:
        if self.rec.status in ("succeeded", "failed", "aborted"):
            return
        self.rec.status = "aborted"
        self.rec.error_code, self.rec.error_message = code, msg
        self.rec.finished_at = datetime.now().astimezone().isoformat(timespec="seconds")
        self.rec.duration_ms = self._elapsed_ms()
        self.reporter.run_error(code, msg)
        self._notice("warn", "回放已中止", msg)

    def _finish_suspended(self, sid: str, code: str, msg: str, diag: str = "",
                          approval_token: str = "") -> None:
        self.rec.status = "suspended"
        self.rec.diagnostics_path = diag or self.rec.diagnostics_path
        self.reporter.run_suspended(sid, code, diag)
        self._notice("warn", "回放等待人工",
                     f"{sid}：{msg}" + (f"｜诊断包 {diag}" if diag else ""))

    def _notice(self, level: str, title: str, body: str, actions: list[str] | None = None) -> None:
        try:
            self.reporter.notice(level, title, body, actions)
        except Exception:  # noqa: BLE001
            pass

    def _resume_index(self) -> int:
        cp = self._checkpoint_seed or {}
        completed = set(cp.get("completed") or [])
        for k, s in enumerate(self.steps):
            if s.sid not in completed:
                return k
        return len(self.steps)

    def _elapsed_ms(self) -> int:
        try:
            a = datetime.fromisoformat(self.rec.started_at)
            b = datetime.fromisoformat(self.rec.finished_at)
            return max(0, int((b - a).total_seconds() * 1000))
        except Exception:  # noqa: BLE001
            return 0


class _NullGrabber:
    def grab(self, rect: Any = None) -> bytes:  # noqa: ARG002
        return b""

    def grab_gray(self, rect: Any = None) -> Any:  # noqa: ARG002
        raise RuntimeError("no grabber")


def _type_anchor_is_weak(step: Any) -> bool:
    """type 步骤是否**没有真正的输入控件锚点**（只有窗口/容器）。

    Chrome / Electron 单 HWND：录制时拿到的锚点是 `Pane[... Chrome]` 或
    `class_index Chrome_WidgetWin_1`。页面标题一变（百度首页 → 结果页），
    这些定位器必然全丢，旧实现直接 RP-2001 让整轮回放失败（真机实录）。
    键入的本质是"向当前焦点发键盘"，因此这类弱锚点允许降级为直接输入。
    """
    if getattr(step, "action", "") != "type":
        return False
    uia = [l for l in getattr(step, "locators", []) if getattr(l, "kind", "") == "uia"]
    if not uia:
        return True
    for spec in uia:
        v = getattr(spec, "value", None)
        text = json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else str(v or "")
        if any(t in text for t in ("Edit", "Document", "ComboBox", "Text Area")):
            return False
    return True


def _type_timeout_unproven(step: Any, err: Any) -> bool:
    """type 步骤定位耗尽，但**没有任何一条链确证元素不存在**（全是 timeout）。

    timeout 只代表"搜索没跑完"（应用忙/UIA 慢），不代表地址栏之类的目标
    不在页面里（真机坐实：Chrome 深树 2s 遍历不完 → type 明明可发焦点，
    却被 RP-2001 判死）。键入本质是"向当前焦点发键盘"，此时降级为直接
    输入比挂起整轮更接近用户意图。

    保守边界：链中只要出现 not_found/disabled 等**确证性**失败，就不得
    降级——元素确实不在，向焦点乱打会把文字送进错误控件。
    """
    if getattr(step, "action", "") != "type":
        return False
    misses = (getattr(err, "detail", None) or {}).get("misses") or []
    reasons = [str(m.get("reason", "")) for m in misses if isinstance(m, dict)]
    if not reasons:
        return False
    return all(r == "timeout" for r in reasons)


def _window_matches(win: Any, hint: dict[str, Any]) -> bool:
    """窗口是否满足 app_hint（exe / cls / 标题正则）。

    作用域隔离的唯一判据：只比 exe 会把同一进程的所有窗口视为同一个
    （Chrome 多窗口、Office 多文档），导致在错误窗口里命中和输入。
    """
    if win is None:
        return False
    exe = str(hint.get("exe", "") or "").lower()
    if exe and str(getattr(win, "exe", "") or "").lower() != exe:
        return False
    cls = str(hint.get("cls", "") or "")
    if cls and str(getattr(win, "cls", "") or "") != cls:
        return False
    pat = str(hint.get("window_title_regex", "") or "")
    if pat:
        try:
            if not re.search(pat, str(getattr(win, "title", "") or "")):
                return False
        except re.error:
            return False
    return True
