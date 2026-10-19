"""desktop_repeat 数据契约（详细设计 §3.3/§4.3/§4.5/§6.1）。

全部为纯数据 dataclass：可 JSON 化、可单测、无平台依赖。
字段命名与设计文档表格严格一致；未知扩展一律进 ``ext`` 袋子（§4.2 演进规则 1）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

SCHEMA_VERSION = 1

# ── 事件类型（§3.3，共 19 种 + 控制事件） ─────────────────────────
EVENT_TYPES = (
    "session_start", "session_end", "pointer_move", "pointer_down", "pointer_up",
    "click", "double_click", "right_click", "middle_click", "drag", "scroll",
    "key", "text_input", "clipboard", "window_change", "marker",
    "pause", "resume", "discard",
)
EVENT_SOURCES = ("hook", "derived", "marker", "system")

# 关键事件：触发 UIA 快照与关键帧（§3.5）
SNAPSHOT_EVENT_TYPES = frozenset({"click", "double_click", "right_click", "middle_click", "drag", "window_change"})

# ── 步骤动作（§4.5） ────────────────────────────────────────────
STEP_ACTIONS = (
    "click", "double_click", "right_click", "type", "hotkey", "scroll", "drag",
    "window_focus", "wait_window", "wait_element", "assert_element", "assert_text",
    "manual_confirm", "marker", "launch_app",
)

RISK_LEVELS = ("read", "write", "dangerous")

# 步骤/动作 → 默认风险（§9.2；回放动作均改外部状态，查询类为 read）
_ACTION_RISK: dict[str, str] = {
    "click": "write", "double_click": "write", "right_click": "write",
    "type": "write", "hotkey": "write", "scroll": "read", "drag": "write",
    "window_focus": "read", "wait_window": "read", "wait_element": "read",
    "assert_element": "read", "assert_text": "read", "manual_confirm": "read",
    "marker": "read", "launch_app": "write",
}


def default_action_risk(action: str) -> str:
    return _ACTION_RISK.get(action, "write")


# ── 坐标与窗口 ─────────────────────────────────────────────────
@dataclass(slots=True)
class Pos:
    """物理坐标点（ADR-04：phys 为唯一真值空间）。"""

    x: int
    y: int
    space: str = "phys"
    monitor: int = 1
    scale: float = 1.0
    rel: tuple[float, float] | None = None  # 相对目标窗口客户区 0–1 比例

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"x": self.x, "y": self.y, "space": self.space,
                             "monitor": self.monitor, "scale": self.scale}
        if self.rel is not None:
            d["rel"] = [round(self.rel[0], 4), round(self.rel[1], 4)]
        return d

    @staticmethod
    def from_dict(d: dict[str, Any] | None) -> Pos | None:
        if not d:
            return None
        rel = d.get("rel")
        return Pos(
            x=int(d.get("x", 0)), y=int(d.get("y", 0)),
            space=str(d.get("space", "phys")),
            monitor=int(d.get("monitor", 1)),
            scale=float(d.get("scale", 1.0)),
            rel=(float(rel[0]), float(rel[1])) if isinstance(rel, (list, tuple)) and len(rel) == 2 else None,
        )


@dataclass(slots=True)
class WindowRef:
    """窗口上下文（§3.3 win 字段）。hwnd 仅录制期关联，回放不得依赖。"""

    hwnd: int = 0
    title: str = ""
    cls: str = ""
    pid: int = 0
    exe: str = ""
    rect: tuple[int, int, int, int] = (0, 0, 0, 0)
    client: tuple[int, int, int, int] = (0, 0, 0, 0)
    minimized: bool = False
    elevated: bool = False
    cloaked: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "hwnd": self.hwnd, "title": self.title, "cls": self.cls,
            "pid": self.pid, "exe": self.exe,
            "rect": list(self.rect), "client": list(self.client),
            "minimized": self.minimized, "elevated": self.elevated, "cloaked": self.cloaked,
        }

    @staticmethod
    def from_dict(d: dict[str, Any] | None) -> WindowRef | None:
        if not d:
            return None
        return WindowRef(
            hwnd=int(d.get("hwnd", 0) or 0), title=str(d.get("title", "")),
            cls=str(d.get("cls", "")), pid=int(d.get("pid", 0) or 0), exe=str(d.get("exe", "")),
            rect=tuple(int(v) for v in d.get("rect", (0, 0, 0, 0))),   # type: ignore[arg-type]
            client=tuple(int(v) for v in d.get("client", (0, 0, 0, 0))),  # type: ignore[arg-type]
            minimized=bool(d.get("minimized", False)),
            elevated=bool(d.get("elevated", False)),
            cloaked=bool(d.get("cloaked", False)),
        )


@dataclass(slots=True)
class UiaSnapshot:
    """UIA 最小属性集（§3.5）。"""

    control_type: str = ""
    name: str = ""
    automation_id: str = ""
    class_name: str = ""
    framework_id: str = ""
    path: str = ""
    depth: int = 0
    is_password: bool = False
    is_enabled: bool = True
    is_offscreen: bool = False
    patterns: list[str] = field(default_factory=list)
    rect: tuple[int, int, int, int] = (0, 0, 0, 0)
    ancestors: list[str] = field(default_factory=list)
    is_editable: bool = False
    value: str = ""  # 录制期 IME 差分校验用；is_password 时恒为空

    def to_dict(self) -> dict[str, Any]:
        return {
            "control_type": self.control_type, "name": self.name,
            "automation_id": self.automation_id, "class_name": self.class_name,
            "framework_id": self.framework_id, "path": self.path, "depth": self.depth,
            "is_password": self.is_password, "is_enabled": self.is_enabled,
            "is_offscreen": self.is_offscreen, "patterns": list(self.patterns),
            "rect": list(self.rect), "ancestors": list(self.ancestors),
            "is_editable": self.is_editable,
        }

    @staticmethod
    def from_dict(d: dict[str, Any] | None) -> UiaSnapshot | None:
        if not d:
            return None
        return UiaSnapshot(
            control_type=str(d.get("control_type", "")), name=str(d.get("name", "")),
            automation_id=str(d.get("automation_id", "")), class_name=str(d.get("class_name", "")),
            framework_id=str(d.get("framework_id", "")), path=str(d.get("path", "")),
            depth=int(d.get("depth", 0) or 0),
            is_password=bool(d.get("is_password", False)), is_enabled=bool(d.get("is_enabled", True)),
            is_offscreen=bool(d.get("is_offscreen", False)),
            patterns=[str(p) for p in d.get("patterns", []) or []],
            rect=tuple(int(v) for v in d.get("rect", (0, 0, 0, 0))),  # type: ignore[arg-type]
            ancestors=[str(a) for a in d.get("ancestors", []) or []],
            is_editable=bool(d.get("is_editable", False)),
            value=str(d.get("value", "")),
        )


@dataclass(slots=True)
class MonitorInfo:
    index: int = 1
    name: str = ""
    primary: bool = False
    rect: tuple[int, int, int, int] = (0, 0, 0, 0)
    work: tuple[int, int, int, int] = (0, 0, 0, 0)
    dpi: int = 96
    scale: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        return {"index": self.index, "name": self.name, "primary": self.primary,
                "rect": list(self.rect), "work": list(self.work),
                "dpi": self.dpi, "scale": self.scale}

    @staticmethod
    def from_dict(d: dict[str, Any]) -> MonitorInfo:
        return MonitorInfo(
            index=int(d.get("index", 1)), name=str(d.get("name", "")),
            primary=bool(d.get("primary", False)),
            rect=tuple(int(v) for v in d.get("rect", (0, 0, 0, 0))),   # type: ignore[arg-type]
            work=tuple(int(v) for v in d.get("work", (0, 0, 0, 0))),   # type: ignore[arg-type]
            dpi=int(d.get("dpi", 96) or 96), scale=float(d.get("scale", 1.0) or 1.0),
        )


@dataclass(slots=True)
class Env:
    """环境基线（§4.4 env）。"""

    os: str = ""
    session_id: int = 0
    input_desktop: str = "Default"
    virtual_screen: tuple[int, int, int, int] = (0, 0, 0, 0)
    monitors: list[MonitorInfo] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"os": self.os, "session_id": self.session_id, "input_desktop": self.input_desktop,
                "virtual_screen": list(self.virtual_screen),
                "monitors": [m.to_dict() for m in self.monitors]}

    @staticmethod
    def from_dict(d: dict[str, Any] | None) -> Env:
        d = d or {}
        return Env(
            os=str(d.get("os", "")), session_id=int(d.get("session_id", 0) or 0),
            input_desktop=str(d.get("input_desktop", "Default")),
            virtual_screen=tuple(int(v) for v in d.get("virtual_screen", (0, 0, 0, 0))),  # type: ignore[arg-type]
            monitors=[MonitorInfo.from_dict(m) for m in d.get("monitors", []) or []],
        )


# ── 事件 ───────────────────────────────────────────────────────
@dataclass(slots=True)
class RecEvent:
    """events.jsonl 单行（§4.3）。"""

    seq: int
    t: int            # 会话内毫秒（monotonic），回放节拍唯一依据
    ts: int           # epoch ms
    type: str
    src: str = "hook"
    v: int = SCHEMA_VERSION
    pos: Pos | None = None
    win: WindowRef | None = None
    uia: UiaSnapshot | None = None
    data: dict[str, Any] = field(default_factory=dict)
    flags: dict[str, bool] = field(default_factory=dict)

    def to_line(self) -> str:
        d: dict[str, Any] = {
            "v": self.v, "seq": self.seq, "t": self.t, "ts": self.ts,
            "type": self.type, "src": self.src, "data": self.data,
        }
        if self.pos is not None:
            d["pos"] = self.pos.to_dict()
        if self.win is not None:
            d["win"] = self.win.to_dict()
        # 隐私红线：uia.value 永不落盘（录制期内存态用于 IME 差分）
        if self.uia is not None:
            d["uia"] = self.uia.to_dict()
        if self.flags:
            d["flags"] = self.flags
        import json
        return json.dumps(d, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def from_line(line: str) -> RecEvent:
        import json
        d = json.loads(line)
        ev = RecEvent(
            seq=int(d["seq"]), t=int(d["t"]), ts=int(d["ts"]),
            type=str(d["type"]), src=str(d.get("src", "hook")), v=int(d.get("v", 1)),
            pos=Pos.from_dict(d.get("pos")), win=WindowRef.from_dict(d.get("win")),
            uia=UiaSnapshot.from_dict(d.get("uia")),
            data=dict(d.get("data") or {}),
            flags={str(k): bool(v) for k, v in (d.get("flags") or {}).items()},
        )
        return ev


# ── 定位规格与命中 ─────────────────────────────────────────────
@dataclass(slots=True)
class LocatorSpec:
    """单个定位器规格（§4.5 target.locators 元素）。"""

    kind: str                                   # uia | image | relative | coord
    by: str = ""                                # uia 子方式：automation_id|name_type|structure_path|class_index
    value: Any = None
    timeout_ms: int = 2000
    confidence: float = 0.5
    # image
    anchor_ref: str = ""
    threshold: float = 0.82
    scales: tuple[float, ...] = (0.8, 0.9, 1.0, 1.1, 1.25)
    # relative / coord
    anchor: str = "window"
    ratio: tuple[float, float] | None = None
    pos: tuple[int, int] | None = None
    scale: float = 1.0
    screen: tuple[int, int] | None = None
    monitor: int = 1
    # 降级放宽标记（失败阶梯用）
    relaxed: bool = False

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"kind": self.kind, "confidence": round(self.confidence, 3)}
        if self.by:
            d["by"] = self.by
        if self.value is not None:
            d["value"] = self.value
        if self.kind == "uia":
            d["timeout_ms"] = self.timeout_ms
        if self.kind == "image":
            d.update({"anchor_ref": self.anchor_ref, "threshold": self.threshold,
                      "scales": list(self.scales)})
        if self.kind == "relative":
            d.update({"anchor": self.anchor, "ratio": list(self.ratio) if self.ratio else None})
        if self.kind == "coord":
            d.update({"pos": list(self.pos) if self.pos else None, "scale": self.scale,
                      "screen": list(self.screen) if self.screen else None, "monitor": self.monitor})
        return d

    @staticmethod
    def from_dict(d: dict[str, Any]) -> LocatorSpec:
        ratio = d.get("ratio")
        pos = d.get("pos")
        screen = d.get("screen")
        return LocatorSpec(
            kind=str(d.get("kind", "")), by=str(d.get("by", "")), value=d.get("value"),
            timeout_ms=int(d.get("timeout_ms", 2000) or 2000),
            confidence=float(d.get("confidence", 0.5) or 0.5),
            anchor_ref=str(d.get("anchor_ref", "")),
            threshold=float(d.get("threshold", 0.82) or 0.82),
            scales=tuple(float(s) for s in d.get("scales", (0.8, 0.9, 1.0, 1.1, 1.25)) or (1.0,)),
            anchor=str(d.get("anchor", "window")),
            ratio=(float(ratio[0]), float(ratio[1])) if isinstance(ratio, (list, tuple)) and len(ratio) == 2 else None,
            pos=(int(pos[0]), int(pos[1])) if isinstance(pos, (list, tuple)) and len(pos) == 2 else None,
            scale=float(d.get("scale", 1.0) or 1.0),
            screen=(int(screen[0]), int(screen[1])) if isinstance(screen, (list, tuple)) and len(screen) == 2 else None,
            monitor=int(d.get("monitor", 1) or 1),
            relaxed=bool(d.get("relaxed", False)),
        )


@dataclass(slots=True)
class Hit:
    """定位命中（§6.1 契约）。rect/center 为物理坐标。"""

    rect: tuple[int, int, int, int]
    center: tuple[int, int]
    element: Any | None = None      # UIA 元素引用（平台对象，不序列化）
    kind: str = ""
    score: float = 0.0
    elapsed_ms: int = 0
    diagnostics: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return True


@dataclass(slots=True)
class Miss:
    """定位失败（reason: not_found | timeout | disabled | occluded | env_gated）。"""

    kind: str = ""
    reason: str = "not_found"
    elapsed_ms: int = 0
    diagnostics: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return False


# ── 步骤 ───────────────────────────────────────────────────────
@dataclass(slots=True)
class StepDef:
    """可执行步骤（§4.5）。工作流 gui 节点的 data 即步骤体。"""

    sid: str
    action: str
    label: str = ""
    scope: str = "window"                       # window | desktop
    app_hint: dict[str, Any] = field(default_factory=dict)
    locators: list[LocatorSpec] = field(default_factory=list)
    params: dict[str, Any] = field(default_factory=dict)
    timing: dict[str, Any] = field(default_factory=dict)
    assertion: dict[str, Any] | None = None
    risk: str = "write"
    policy: dict[str, Any] = field(default_factory=lambda: {"on_fail": "ladder", "retries": 2,
                                                             "backoff_ms": 800, "idempotent": True})
    evidence: dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.5
    needs_review: bool = False
    step_schema: int = SCHEMA_VERSION
    ext: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "sid": self.sid, "step_schema": self.step_schema, "action": self.action,
            "label": self.label,
            "target": {"scope": self.scope, "app_hint": self.app_hint,
                       "locators": [x.to_dict() for x in self.locators]},
            "params": self.params, "timing": self.timing, "assertion": self.assertion,
            "risk": self.risk, "policy": self.policy, "evidence": self.evidence,
            "meta": {"confidence": round(self.confidence, 3), "needs_review": self.needs_review},
            "ext": self.ext,
        }

    @staticmethod
    def from_dict(d: dict[str, Any]) -> StepDef:
        target = d.get("target") or {}
        meta = d.get("meta") or {}
        return StepDef(
            sid=str(d.get("sid", "")), action=str(d.get("action", "")),
            label=str(d.get("label", "")),
            scope=str(target.get("scope", "window")),
            app_hint=dict(target.get("app_hint") or {}),
            locators=[LocatorSpec.from_dict(x) for x in target.get("locators") or []],
            params=dict(d.get("params") or {}),
            timing=dict(d.get("timing") or {}),
            assertion=d.get("assertion"),
            risk=str(d.get("risk", "write")),
            policy=dict(d.get("policy") or {"on_fail": "ladder", "retries": 2, "backoff_ms": 800, "idempotent": True}),
            evidence=dict(d.get("evidence") or {}),
            confidence=float(meta.get("confidence", d.get("confidence", 0.5)) or 0.5),
            needs_review=bool(meta.get("needs_review", False)),
            step_schema=int(d.get("step_schema", SCHEMA_VERSION) or SCHEMA_VERSION),
            ext=dict(d.get("ext") or {}),
        )

    def validate(self) -> str | None:
        """返回校验失败原因；None 为合法（RP-6004 数据源）。"""
        if not self.sid:
            return "步骤缺少 sid"
        if self.action not in STEP_ACTIONS:
            return f"步骤 {self.sid} 动作非法: {self.action}"
        if self.risk not in RISK_LEVELS:
            return f"步骤 {self.sid} 风险等级非法: {self.risk}"
        if self.step_schema > SCHEMA_VERSION:
            return f"步骤 {self.sid} schema 版本过新: {self.step_schema}"
        # 指针类动作必须有元素定位；type/hotkey 允许「焦点级」执行（§6.3：
        # 文本输入到当前焦点控件），但 type 至少要有一个作用窗口锚点。
        needs_target = self.action in ("click", "double_click", "right_click", "scroll", "drag")
        if needs_target and not self.locators:
            return f"步骤 {self.sid}({self.action}) 缺少定位链"
        if self.action == "type" and not self.locators and not (self.app_hint or {}):
            return f"步骤 {self.sid}(type) 既无定位链也无作用窗口"
        return None


# ── 编译产物 ───────────────────────────────────────────────────
@dataclass(slots=True)
class CompileWarning:
    code: str
    sid: str = ""
    message: str = ""
    severity: str = "warn"      # info | warn | error

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "sid": self.sid, "message": self.message, "severity": self.severity}


@dataclass(slots=True)
class CompileStats:
    steps: int = 0
    low_confidence: int = 0
    needs_review: int = 0
    loop_candidates: int = 0
    param_candidates: int = 0
    duration_ms: int = 0
    events_scanned: int = 0
    pauses_excluded: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {"steps": self.steps, "low_confidence": self.low_confidence,
                "needs_review": self.needs_review, "loop_candidates": self.loop_candidates,
                "param_candidates": self.param_candidates, "duration_ms": self.duration_ms,
                "events_scanned": self.events_scanned, "pauses_excluded": self.pauses_excluded}


@dataclass(slots=True)
class CompileResult:
    session_id: str
    steps: list[StepDef] = field(default_factory=list)
    warnings: list[CompileWarning] = field(default_factory=list)
    stats: CompileStats = field(default_factory=CompileStats)
    draft_graph: dict[str, Any] = field(default_factory=lambda: {"nodes": [], "edges": []})
    params: list[dict[str, Any]] = field(default_factory=list)  # start 节点参数定义（§6.6.1）

    def to_dict(self) -> dict[str, Any]:
        return {"session_id": self.session_id,
                "steps": [s.to_dict() for s in self.steps],
                "warnings": [w.to_dict() for w in self.warnings],
                "stats": self.stats.to_dict(),
                "params": self.params}


# ── 运行记录 ───────────────────────────────────────────────────
@dataclass(slots=True)
class Attempt:
    """一次定位/放宽尝试（进 repeat_step_hits.attempts_json）。"""

    kind: str
    by: str = ""
    result: str = "miss"        # hit | miss
    reason: str = ""
    elapsed_ms: int = 0
    relaxed: bool = False
    score: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "by": self.by, "result": self.result, "reason": self.reason,
                "elapsed_ms": self.elapsed_ms, "relaxed": self.relaxed, "score": round(self.score, 3)}


@dataclass(slots=True)
class StepRun:
    sid: str = ""
    index: int = 0
    action: str = ""
    status: str = "pending"     # pending | running | done | degraded | skipped | failed | suspended
    hit_index: int = -1
    hit_kind: str = ""
    attempts: list[Attempt] = field(default_factory=list)
    elapsed_ms: int = 0
    code: str = ""
    message: str = ""
    degraded: bool = False
    blind: bool = False
    unstable: bool = False
    screenshot: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"sid": self.sid, "index": self.index, "action": self.action, "status": self.status,
                "hit_index": self.hit_index, "hit_kind": self.hit_kind,
                "attempts": [a.to_dict() for a in self.attempts],
                "elapsed_ms": self.elapsed_ms, "code": self.code, "message": self.message,
                "degraded": self.degraded, "blind": self.blind, "unstable": self.unstable,
                "screenshot": self.screenshot}


@dataclass(slots=True)
class RunRecord:
    run_id: str = ""
    workflow_id: str = ""
    # §5.2 取值：pending|precheck|running|retrying|suspended|resuming|dry_run|succeeded|failed|aborted
    status: str = "pending"
    total_steps: int = 0
    env_level: str = "L0"
    drift: dict[str, Any] = field(default_factory=dict)
    steps: list[StepRun] = field(default_factory=list)
    vars: dict[str, Any] = field(default_factory=dict)
    attempts_total: int = 0
    error_code: str = ""
    error_message: str = ""
    diagnostics_path: str = ""
    checkpoint: dict[str, Any] = field(default_factory=dict)
    started_at: str = ""
    finished_at: str = ""
    duration_ms: int = 0
    blind_used: bool = False

    def stats(self) -> dict[str, Any]:
        done = sum(1 for s in self.steps if s.status in ("done", "degraded"))
        failed = sum(1 for s in self.steps if s.status == "failed")
        skipped = sum(1 for s in self.steps if s.status == "skipped")
        degraded = sum(1 for s in self.steps if s.degraded)
        return {"total": self.total_steps, "done": done, "failed": failed,
                "skipped": skipped, "degraded": degraded, "retries": self.attempts_total,
                "duration_ms": self.duration_ms}

    def to_dict(self) -> dict[str, Any]:
        # vars/checkpoint/attempts_total 必须随 to_dict 序列化：repository.finish_run
        # 从本字典取这三个键，缺失即被默认值覆盖（attempts_total→0、checkpoint→{}、
        # vars_json→{}），历史详情与断点续跑数据全丢（体检 P0-2）。
        # vars 在 RunRecord 构造时已过 redact_vars 脱敏，可安全落盘。
        return {"run_id": self.run_id, "workflow_id": self.workflow_id, "status": self.status,
                "total_steps": self.total_steps, "env_level": self.env_level, "drift": self.drift,
                "steps": [s.to_dict() for s in self.steps],
                "vars": self.vars, "checkpoint": self.checkpoint,
                "attempts_total": self.attempts_total,
                "stats": self.stats(), "error_code": self.error_code, "error_message": self.error_message,
                "diagnostics_path": self.diagnostics_path,
                "started_at": self.started_at, "finished_at": self.finished_at,
                "duration_ms": self.duration_ms, "blind_used": self.blind_used}


@dataclass(slots=True)
class DryRunStep:
    sid: str = ""
    index: int = 0
    label: str = ""
    action: str = ""
    hit_index: int = -1
    hit_kind: str = ""
    elapsed_ms: int = 0
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"sid": self.sid, "index": self.index, "label": self.label, "action": self.action,
                "hit_index": self.hit_index, "hit_kind": self.hit_kind,
                "elapsed_ms": self.elapsed_ms,
                "verdict": ("hit@" + str(self.hit_index)) if self.hit_index >= 0 else "miss",
                "message": self.message}


@dataclass(slots=True)
class DryRunReport:
    workflow_id: str = ""
    env_level: str = "L0"
    ok: bool = True
    steps: list[DryRunStep] = field(default_factory=list)
    duration_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        hits = sum(1 for s in self.steps if s.hit_index >= 0)
        primary = sum(1 for s in self.steps if s.hit_index == 0)
        return {"workflow_id": self.workflow_id, "env_level": self.env_level, "ok": self.ok,
                "summary": {"total": len(self.steps), "hit": hits, "primary_hit": primary,
                            "miss": len(self.steps) - hits},
                "steps": [s.to_dict() for s in self.steps], "duration_ms": self.duration_ms}
