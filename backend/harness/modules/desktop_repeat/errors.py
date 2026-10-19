"""desktop_repeat 错误码体系（详细设计 §7.1 + 附录 B，共 37 个）。

约定：``RP-<域><序号>``；域 1 环境 / 2 定位 / 3 时序 / 4 权限 / 5 输入 /
6 数据 / 7 系统 / 8 参数。``RepeatError`` 携带面向用户的中文提示与恢复性标记，
供失败阶梯（executor/recovery.py）与状态机分流处理。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class ErrorCode:
    code: str
    domain: str          # env|loc|timing|perm|input|data|sys|param
    name: str            # 名称（诊断用）
    recoverable: bool    # 是否可能续跑/降级
    default_action: str  # abort|suspend|retry|degrade|reject|continue


def _ec(code: str, domain: str, name: str, recoverable: bool, action: str) -> ErrorCode:
    return ErrorCode(code, domain, name, recoverable, action)


# 37 个错误码（附录 B 逐项对齐）
RP = {
    "RP-1001": _ec("RP-1001", "env", "无图形会话", False, "abort"),
    "RP-1002": _ec("RP-1002", "env", "目标进程未运行", True, "launch_or_suspend"),
    "RP-1003": _ec("RP-1003", "env", "目标窗口未找到", True, "retry_then_suspend"),
    "RP-1004": _ec("RP-1004", "env", "显示器拓扑变化", True, "degrade_or_suspend"),
    "RP-1005": _ec("RP-1005", "env", "缩放导致逻辑分辨率变化", True, "disable_coord"),
    "RP-1006": _ec("RP-1006", "env", "应用无响应", True, "abort_snapshot"),
    "RP-1007": _ec("RP-1007", "env", "意外弹窗", True, "suspend"),
    "RP-1008": _ec("RP-1008", "env", "窗口不可见（cloaked/其他桌面）", True, "suspend"),
    "RP-2001": _ec("RP-2001", "loc", "定位链全部失效", True, "retry_then_suspend"),
    "RP-2002": _ec("RP-2002", "loc", "UIA 查询超时", True, "skip_locator"),
    "RP-2003": _ec("RP-2003", "loc", "图像锚点未匹配", True, "degrade"),
    "RP-2004": _ec("RP-2004", "loc", "目标被遮挡", True, "degrade_or_suspend"),
    "RP-2005": _ec("RP-2005", "loc", "目标不可用", True, "suspend"),
    "RP-2006": _ec("RP-2006", "loc", "坐标兜底被闸门禁用", True, "suspend"),
    "RP-3001": _ec("RP-3001", "timing", "前置等待超时", True, "retry_then_suspend"),
    "RP-3002": _ec("RP-3002", "timing", "断言失败", False, "on_fail"),
    "RP-3003": _ec("RP-3003", "timing", "步骤预算耗尽", True, "abort"),
    "RP-3004": _ec("RP-3004", "timing", "界面持续变化", True, "continue_mark"),
    "RP-4001": _ec("RP-4001", "perm", "UIPI 拦截", False, "abort"),
    "RP-4002": _ec("RP-4002", "perm", "安全策略拦截", False, "abort"),
    "RP-4003": _ec("RP-4003", "perm", "审批拒绝/超时", True, "abort"),
    "RP-4004": _ec("RP-4004", "perm", "密码字段未授权", True, "suspend"),
    # 挂起等待人工裁决超时后的自动中止码（executor/_await_resume 使用）。
    # 旧代码发 "RP-4005" 但字典无此项 → 落到 default 兜底（recoverable=False、
    # action=abort），REST 语义与错误字典不一致（体检 P1-6）。
    "RP-4005": _ec("RP-4005", "perm", "人工裁决等待超时", True, "abort"),
    "RP-5001": _ec("RP-5001", "input", "前台激活失败", True, "suspend"),
    "RP-5002": _ec("RP-5002", "input", "剪贴板失败", True, "degrade_unicode"),
    "RP-5003": _ec("RP-5003", "input", "输入结果不一致", True, "retry_then_suspend"),
    "RP-5004": _ec("RP-5004", "input", "输入租约被占用", True, "reject"),
    "RP-6001": _ec("RP-6001", "data", "bundle 校验失败", False, "reject"),
    "RP-6002": _ec("RP-6002", "data", "Schema 版本不兼容", False, "reject"),
    "RP-6003": _ec("RP-6003", "data", "事件流截断", True, "drop_last"),
    "RP-6004": _ec("RP-6004", "data", "流程定义非法", False, "reject"),
    "RP-7001": _ec("RP-7001", "sys", "用户紧急停止", True, "abort"),
    "RP-7002": _ec("RP-7002", "sys", "检测到人工介入", True, "suspend"),
    "RP-7003": _ec("RP-7003", "sys", "内部异常", True, "abort_diagnose"),
    "RP-7004": _ec("RP-7004", "sys", "录制器失联", True, "finalize_truncated"),
    "RP-8001": _ec("RP-8001", "param", "必填参数缺失", True, "reject"),
    "RP-8002": _ec("RP-8002", "param", "参数格式非法", True, "reject"),
    "RP-8003": _ec("RP-8003", "param", "无人值守遇 runtime_only 参数", True, "reject_notify"),
}


class RepeatError(Exception):
    """带错误码、面向用户中文提示与恢复性标记的运行时异常（§7.1）。"""

    def __init__(
        self,
        code: str,
        message: str = "",
        step_sid: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        meta = RP.get(code)
        self.code = code
        self.domain = meta.domain if meta else "sys"
        self.recoverable = meta.recoverable if meta else False
        self.default_action = meta.default_action if meta else "abort"
        self.message = message or (meta.name if meta else code)
        self.step_sid = step_sid
        self.detail: dict[str, Any] = detail or {}
        super().__init__(f"{code} {self.message}")

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "step_sid": self.step_sid,
                "recoverable": self.recoverable, "detail": self.detail}


# 兼容字符串前缀解析（日志/REST 里可能出现 "RP-2001 ..." 形式）
_CODE_RE = re.compile(r"RP-\d{4}")


def parse_code(text: str) -> str:
    m = _CODE_RE.search(text or "")
    return m.group(0) if m else "RP-7003"
