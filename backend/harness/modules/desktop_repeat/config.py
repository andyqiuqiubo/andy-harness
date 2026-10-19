"""desktop_repeat 配置（详细设计 附录 C 全量键）。

加载优先级：环境变量 HARNESS_REPEAT_* > 插件配置 dict（plugin.json config 已合并）
> 内置默认。敏感/布尔/数值解析容错，与 computer_use/config.py 同风格。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

# 危险词库（§5.4 S9 可配，默认词表与设计文档一致）
DEFAULT_RISK_WORDS = (
    "删除|移除|清空|提交|确认支付|支付|转账|发送|发布|卸载|格式化|覆盖|重置|注销|退出登录|关闭"
)

_BOOL = {"1", "true", "yes", "on"}


def _as_bool(v: Any, default: bool) -> bool:
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in _BOOL


def _as_int(v: Any, default: int) -> int:
    try:
        return int(float(str(v)))
    except (TypeError, ValueError):
        return default


def _as_float(v: Any, default: float) -> float:
    try:
        return float(str(v))
    except (TypeError, ValueError):
        return default


def _as_list_str(v: Any) -> list[str]:
    if isinstance(v, list):
        return [str(x) for x in v if str(x).strip()]
    if isinstance(v, str) and v.strip():
        return [x.strip() for x in v.split(",") if x.strip()]
    return []


@dataclass(slots=True)
class RepeatConfig:
    # 采集
    frame_tier: str = "standard"            # minimal | standard | forensic
    countdown_s: int = 3
    uia_enabled: bool = True
    uia_timeout_ms: int = 300
    capture_clipboard: bool = False
    mask_windows: list[str] = field(default_factory=list)
    mask_keys: list[str] = field(default_factory=list)
    auto_pause_mask: list[str] = field(default_factory=list)
    queue_capacity: int = 4096
    hotkeys: dict[str, str] = field(default_factory=lambda: {
        "toggle": "ctrl+alt+r", "pause": "ctrl+alt+p",
        "discard": "ctrl+alt+x", "marker": "f10",
    })
    # 执行 / 护栏
    coord_gate: bool = True
    allow_password_replay: bool = False
    step_timeout_ms: int = 12000
    run_timeout_min: int = 30
    # 「目标窗口尚未出现」的有界等待上限（毫秒）。
    # 点击按钮弹出模态对话框（保存/另存为/打开文件）从点击到可见有几百毫秒延迟，
    # 紧接着的 window_focus 立即枚举必扑空 → 作用域停留在**已被禁用的旧窗口** →
    # 被模态遮挡判定 RP-1007 拦死（真机 EditPlus「另存为」s11 坐实）。
    # 仅当 app_hint 给出了明确目标（标题正则或类名）时才等待，避免拖慢"窗口当真
    # 不存在"的场景；0 表示关闭等待。
    window_appear_wait_ms: int = 2500
    # 挂起（等待人工裁决）上限：超时自动中止并释放输入租约。
    # 旧实现无限等待：用户关掉页面后 run 永久挂起、租约不释放，后续回放全部被拒。
    suspend_timeout_s: int = 900
    # 停止录制等待封版收口的轮询轮数（每轮 0.15s）。默认 140≈21s，覆盖 recorder
    # 收尾（flush+manifest+横幅）；测试环境可调小（体检 P2-10）。
    stop_wait_rounds: int = 140
    # 运行期"检测到人工介入自动暂停"（默认关）。
    # GetLastInputInfo 对任何指针移动/滚轮都会刷新，误报率极高——用户手搭在
    # 鼠标上、看一眼进度条就会触发 RP-7002 中断，体验极差（真机多次坐实）。
    # 默认改用"紧急停止热键 Ctrl+Alt+."与页面「中止」按钮兜底；
    # 需要严格无人值守的场景可显式打开。
    human_takeover_detect: bool = False
    # 写操作审批策略（见 safety_gate.SafetyGate）：
    #   degraded   只对"图像/相对/坐标"等降级定位的写操作要求确认
    #   first_write 本轮首次写操作确认一次（旧默认，打断多）
    #   always     每次写操作都确认
    #   never      写操作与高风险动作都不再中途询问（默认；审计仍记录）
    # 注：默认取 never（2026-10-19 16:40 决议：回放默认不做人工审批，
    # 靠 Ctrl+Alt+. 紧急停止 + 页面「中止」兜底；需确认的场景显式改配置）。
    approval_policy: str = "never"
    app_hang_timeout_ms: int = 5000
    retries: int = 2
    retry_backoff_ms: int = 800
    max_retries_per_run: int = 20
    emergency_hotkey: str = "ctrl+alt+."
    # 容量与保留
    retention_days_standard: int = 30
    retention_days_forensic: int = 7
    bundle_max_mb: int = 500
    events_retention_days: int = 180
    # 语义 / 参数
    llm_semantic: bool = False
    # 视觉辅助（百炼 VL，默认关）：解析完成后异步用图像理解模型核对每一步
    # 「做了什么 / 画面是否变化 / 是否生效」，结果落 bundle/vision.json 供审查。
    # 纯旁路：失败只记日志，绝不阻塞/改变录制与回放主链路。
    vision_enabled: bool = False
    # 默认 qwen3-vl-plus：在 dashscope 通用兼容端点实测可用（2026-10-20 真机验证）；
    # 免费额度的 qwen3.8-omni-flash 需 workspace 专属域名（AccessDenied.Unpurchased），
    # 如需切换：repeat.vision_model + repeat.vision_base_url。
    vision_model: str = "qwen3-vl-plus"
    vision_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    vision_api_key: str = ""            # 优先 env DASHSCOPE_API_KEY
    vision_api_key_file: str = ""       # 为空时回退尝试项目根 key.txt
    vision_max_steps: int = 24          # 单次分析步数上限（控制 token 成本）
    vision_timeout_s: int = 60
    params_save_last: bool = True
    unattended_policy: str = "reject"       # reject | todo
    # 提示（§9.5）
    pre_run_notice: bool = True
    pre_run_ready_s: int = 3
    skip_notice_unattended: bool = True
    result_banner_s: int = 5
    record_banner_s: int = 8
    suppress_when_fullscreen: bool = True
    notify_channel: str = ""
    # 编译
    risk_words: str = DEFAULT_RISK_WORDS

    # ── 工厂 ──────────────────────────────────────────────
    @staticmethod
    def load(raw: dict[str, Any] | None = None) -> RepeatConfig:
        c = RepeatConfig()
        d = dict(raw or {})

        def pick(key: str, env: str | None = None) -> Any:
            if env is not None and os.environ.get(env):
                return os.environ[env]
            if key in d:
                return d[key]
            alt = key.replace(".", "_")
            if alt in d:
                return d[alt]
            # 插件 config_schema 用裸键（无 repeat. 前缀）
            bare = key.split(".")[-1]
            if bare in d:
                return d[bare]
            bare_alt = bare.replace(".", "_")
            return d.get(bare_alt, None)

        tier = str(pick("repeat.frame_tier", "HARNESS_REPEAT_FRAME_TIER") or c.frame_tier).lower()
        c.frame_tier = tier if tier in ("minimal", "standard", "forensic") else "standard"
        c.countdown_s = max(0, min(10, _as_int(pick("repeat.countdown_s", "HARNESS_REPEAT_COUNTDOWN"), c.countdown_s)))
        c.uia_enabled = _as_bool(pick("repeat.uia_enabled"), c.uia_enabled)
        c.uia_timeout_ms = max(50, min(3000, _as_int(pick("repeat.uia_timeout_ms"), c.uia_timeout_ms)))
        c.capture_clipboard = _as_bool(pick("repeat.capture_clipboard"), c.capture_clipboard)
        c.mask_windows = _as_list_str(pick("repeat.mask_windows"))
        c.mask_keys = [k.lower() for k in _as_list_str(pick("repeat.mask_keys"))]
        c.auto_pause_mask = _as_list_str(pick("repeat.auto_pause_mask"))
        c.coord_gate = _as_bool(pick("repeat.coord_gate", "HARNESS_REPEAT_COORD_GATE"), c.coord_gate)
        c.allow_password_replay = _as_bool(pick("repeat.allow_password_replay"), c.allow_password_replay)
        c.step_timeout_ms = max(1000, _as_int(pick("repeat.step_timeout_ms"), c.step_timeout_ms))
        c.window_appear_wait_ms = max(0, min(10000, _as_int(
            pick("repeat.window_appear_wait_ms"), c.window_appear_wait_ms)))
        c.run_timeout_min = max(1, _as_int(pick("repeat.run_timeout_min"), c.run_timeout_min))
        c.suspend_timeout_s = max(0, _as_int(pick("repeat.suspend_timeout_s"), c.suspend_timeout_s))
        c.human_takeover_detect = _as_bool(pick("repeat.human_takeover_detect"),
                                           c.human_takeover_detect)
        pol = str(pick("repeat.approval_policy", "HARNESS_REPEAT_APPROVAL_POLICY")
                  or c.approval_policy).strip().lower()
        c.approval_policy = pol if pol in ("degraded", "first_write", "always", "never") \
            else "never"
        c.retries = max(0, min(5, _as_int(pick("repeat.retries"), c.retries)))
        c.retry_backoff_ms = max(100, _as_int(pick("repeat.retry_backoff_ms"), c.retry_backoff_ms))
        c.max_retries_per_run = max(c.retries, _as_int(pick("repeat.max_retries_per_run"), c.max_retries_per_run))
        c.emergency_hotkey = str(pick("repeat.emergency_hotkey") or c.emergency_hotkey).lower()
        c.retention_days_standard = _as_int(pick("repeat.retention_days.standard"), c.retention_days_standard)
        c.retention_days_forensic = _as_int(pick("repeat.retention_days.forensic"), c.retention_days_forensic)
        c.bundle_max_mb = max(50, _as_int(pick("repeat.bundle_max_mb"), c.bundle_max_mb))
        c.llm_semantic = _as_bool(pick("repeat.llm_semantic"), c.llm_semantic)
        c.vision_enabled = _as_bool(pick("repeat.vision_enabled"), c.vision_enabled)
        c.vision_model = str(pick("repeat.vision_model") or c.vision_model)
        c.vision_base_url = str(pick("repeat.vision_base_url") or c.vision_base_url).rstrip("/")
        c.vision_api_key = str(pick("repeat.vision_api_key") or c.vision_api_key)
        c.vision_api_key_file = str(pick("repeat.vision_api_key_file") or c.vision_api_key_file)
        c.vision_max_steps = max(1, min(200, _as_int(pick("repeat.vision_max_steps"), c.vision_max_steps)))
        c.vision_timeout_s = max(5, min(300, _as_int(pick("repeat.vision_timeout_s"), c.vision_timeout_s)))
        c.params_save_last = _as_bool(pick("repeat.params_save_last"), c.params_save_last)
        pol = str(pick("repeat.unattended_policy") or c.unattended_policy).lower()
        c.unattended_policy = pol if pol in ("reject", "todo") else "reject"
        c.pre_run_notice = _as_bool(pick("repeat.prompt.pre_run_notice"), c.pre_run_notice)
        c.pre_run_ready_s = max(0, min(30, _as_int(pick("repeat.prompt.pre_run_ready_s"), c.pre_run_ready_s)))
        c.skip_notice_unattended = _as_bool(pick("repeat.prompt.skip_notice_unattended"), c.skip_notice_unattended)
        c.result_banner_s = _as_int(pick("repeat.prompt.result_banner_s"), c.result_banner_s)
        c.suppress_when_fullscreen = _as_bool(
            pick("repeat.prompt.suppress_when_fullscreen"), c.suppress_when_fullscreen)
        c.notify_channel = str(pick("repeat.prompt.notify_channel") or "")
        c.risk_words = str(pick("repeat.risk_words") or c.risk_words)
        hk = d.get("hotkeys")
        if isinstance(hk, dict):
            for k, v in hk.items():
                if str(v).strip():
                    c.hotkeys[str(k)] = str(v).strip().lower()
        return c

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "frame_tier": self.frame_tier, "countdown_s": self.countdown_s,
            "uia_enabled": self.uia_enabled, "uia_timeout_ms": self.uia_timeout_ms,
            "capture_clipboard": self.capture_clipboard, "coord_gate": self.coord_gate,
            "allow_password_replay": self.allow_password_replay,
            "step_timeout_ms": self.step_timeout_ms, "run_timeout_min": self.run_timeout_min,
            "suspend_timeout_s": self.suspend_timeout_s,
            "window_appear_wait_ms": self.window_appear_wait_ms,
            "human_takeover_detect": self.human_takeover_detect,
            "approval_policy": self.approval_policy,
            "retries": self.retries, "max_retries_per_run": self.max_retries_per_run,
            "emergency_hotkey": self.emergency_hotkey, "unattended_policy": self.unattended_policy,
            "llm_semantic": self.llm_semantic,
            "vision_enabled": self.vision_enabled, "vision_model": self.vision_model,
            "vision_max_steps": self.vision_max_steps, "hotkeys": dict(self.hotkeys),
        }


@dataclass(slots=True)
class RecordConfig:
    """单次录制会话配置（可覆盖 RepeatConfig 默认值）。"""

    label: str = ""
    frame_tier: str = ""                  # 空 = 用全局配置
    countdown_s: int = -1                 # -1 = 用全局
    mask_windows: list[str] = field(default_factory=list)
    mask_keys: list[str] = field(default_factory=list)
    auto_pause_mask: list[str] = field(default_factory=list)

    @staticmethod
    def from_dict(d: dict[str, Any] | None) -> RecordConfig:
        d = d or {}
        return RecordConfig(
            label=str(d.get("label", "")),
            frame_tier=str(d.get("frame_tier", "")),
            countdown_s=_as_int(d.get("countdown_s"), -1),
            mask_windows=_as_list_str(d.get("mask_windows")),
            mask_keys=[k.lower() for k in _as_list_str(d.get("mask_keys"))],
            auto_pause_mask=_as_list_str(d.get("auto_pause_mask")),
        )
