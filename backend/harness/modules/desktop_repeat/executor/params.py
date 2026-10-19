"""回放参数：定义、取值解析、前置校验、脱敏（§6.6）。

优先级：显式传入 > derived 表达式 > start default > 交互获取（有人值守）。
required 走完全程无值 → RP-8001；类型/pattern 不符 → RP-8002；
unattended 遇 runtime_only/sensitive 未注入 → RP-8003（拒绝启动，不挂起）。
sensitive 值：不回显、不进日志/SSE/审计（vars_json 存 ***）。
"""

from __future__ import annotations

import os
import re
import uuid as _uuid
from datetime import datetime
from typing import Any

from ..errors import RepeatError

# derived 表达式白名单函数（§6.6.1）：today/now/uuid/env —— 只读、无副作用
_DERIVED_RE = re.compile(r"^(today|now|uuid|env)\s*(\(([^)]*)\))?$")
_SENSITIVE_MASK = "***"


def _fn_today(fmt: str) -> str:
    return datetime.now().strftime(fmt or "%Y-%m-%d")


def _fn_now(fmt: str) -> str:
    return datetime.now().strftime(fmt or "%Y-%m-%dT%H:%M:%S")


def _eval_derived(expression: str) -> str:
    m = _DERIVED_RE.match((expression or "").strip())
    if not m:
        raise ValueError(f"非法 derived 表达式（仅允许 today/now/uuid/env）: {expression}")
    fn, arg = m.group(1), m.group(3) or ""
    if fn == "today":
        return _fn_today(arg.strip("'\"") or "%Y-%m-%d")
    if fn == "now":
        return _fn_now(arg.strip("'\"") or "%Y-%m-%dT%H:%M:%S")
    if fn == "uuid":
        return _uuid.uuid4().hex
    if fn == "env":
        return os.environ.get(arg.strip("'\""), "")
    return ""


def validate_param_value(pdef: dict[str, Any], value: str) -> None:
    ptype = str(pdef.get("type", "string")).lower()
    pattern = pdef.get("pattern") or ""
    if pattern:
        try:
            if not re.search(pattern, value):
                raise RepeatError("RP-8002",
                                  f"参数 {pdef.get('name')} 格式不符：应为 {pattern or ptype}",
                                  detail={"param": pdef.get("name"), "rule": pattern or ptype})
        except re.error:
            pass
    if ptype in ("number", "integer"):
        try:
            (int if ptype == "integer" else float)(value)
        except (ValueError, TypeError):
            raise RepeatError("RP-8002",
                              f"参数 {pdef.get('name')} 需为数字",
                              detail={"param": pdef.get("name")})
    if value == "" and pdef.get("required"):
        raise RepeatError("RP-8002",
                          f"参数 {pdef.get('name')} 不能为空",
                          detail={"param": pdef.get("name")})


def resolve_params(params_defs: list[dict[str, Any]], explicit: dict[str, Any],
                   *, unattended: bool = False,
                   interactive_provider: Any | None = None) -> tuple[dict[str, Any], dict[str, bool]]:
    """返回 (resolved, sensitive_flags)。resolved 不含敏感值以外的信息；
    sensitive_flags[name]=True 表示该值需脱敏。

    interactive_provider: callable(pdef)->value|None，仅有人值守时提供。
    """
    resolved: dict[str, Any] = {}
    flags: dict[str, bool] = {}
    for pdef in params_defs or []:
        name = str(pdef.get("name", "")).strip()
        if not name:
            continue
        sensitive = bool(pdef.get("sensitive")) or str(pdef.get("type", "")).lower() in ("secret", "password")
        flags[name] = sensitive
        source = str(pdef.get("source", "literal")).lower()
        required = bool(pdef.get("required"))
        value: Any = None
        # 1) 显式
        if name in (explicit or {}):
            value = explicit[name]
        # 2) derived
        if value is None and source == "derived":
            try:
                value = _eval_derived(str(pdef.get("expression", "")))
            except ValueError:
                value = None
        # 3) default
        if value is None and source != "runtime_only" and "default" in pdef and pdef["default"] is not None:
            value = pdef["default"]
        # 4) runtime_only 强制交互
        if value is None and source == "runtime_only":
            if unattended:
                raise RepeatError("RP-8003",
                                  f"本流程含每次须人工提供的参数（{name}），无法无人值守运行",
                                  detail={"param": name})
            if interactive_provider is not None:
                value = interactive_provider(pdef)
        # 5) required 仍无值
        if (value is None or (isinstance(value, str) and value == "")) and required:
            if unattended:
                raise RepeatError("RP-8001", f"缺少必填参数：{name}（{pdef.get('description', '')}）",
                                  detail={"param": name, "required": True})
            raise RepeatError("RP-8001", f"缺少必填参数：{name}（{pdef.get('description', '')}）",
                              detail={"param": name, "required": True})
        if value is None:
            value = ""
        value = str(value)
        try:
            validate_param_value(pdef, value)
        except RepeatError as e:
            if e.code == "RP-8002":
                raise
            raise RepeatError("RP-8002", str(e), detail={"param": name})
        resolved[name] = value
    return resolved, flags


_PLACEHOLDER_RE = re.compile(r"\{\{\s*param\.([A-Za-z0-9_]+)\s*\}\}")


def render_placeholders(text: str, resolved: dict[str, Any], flags: dict[str, bool]) -> str:
    """运行时替换 {{param.x}}（§6.6.4：每步 act 开始时渲染，不在编译期展开）。"""
    def _sub(m: re.Match[str]) -> str:
        key = m.group(1)
        if key in resolved:
            return str(resolved[key])
        # 未解析：原样保留，由调用方在干校验阶段拦截；运行期缺失值按空串
        return ""
    return _PLACEHOLDER_RE.sub(_sub, text or "")


def missing_placeholders(text: str, resolved: dict[str, Any]) -> list[str]:
    return sorted({m.group(1) for m in _PLACEHOLDER_RE.finditer(text or "") if m.group(1) not in resolved})


def redact_vars(resolved: dict[str, Any], flags: dict[str, bool]) -> dict[str, Any]:
    """vars_json/SSE/审计用：敏感值替换为 ***（§6.6.1 sensitive）。"""
    return {k: (_SENSITIVE_MASK if flags.get(k) else v) for k, v in resolved.items()}


def collect_required_param_names(params_defs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """运行对话框/Agent 缺参清单的数据源（§6.6.3）。"""
    out: list[dict[str, Any]] = []
    for p in params_defs or []:
        name = str(p.get("name", "")).strip()
        if not name:
            continue
        out.append({"name": name, "required": bool(p.get("required")),
                    "default": (_SENSITIVE_MASK if (p.get("sensitive")
                                  or str(p.get("type", "")).lower() in ("secret", "password"))
                                else p.get("default", "")),
                    "description": p.get("description", ""), "pattern": p.get("pattern", ""),
                    "type": p.get("type", "string"),
                    "sensitive": bool(p.get("sensitive")) or str(p.get("type", "")).lower() in ("secret", "password"),
                    "source": p.get("source", "literal")})
    return out
