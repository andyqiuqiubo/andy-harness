"""LLM 语义增强（§6.2，可选）。

铁律（设计约束，违反即数据事故）：
- 只改写步骤 label / 流程标题与描述；
- **不增删步骤、不改定位链、不改动作/参数/时序**；
- 无可用 Provider 或调用失败 → 静默跳过（降级原则）；
- 输入脱敏：只给控件 name/窗口标题/动作类型，绝不发送输入框实际值（可能含敏感数据）。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

from ..models import CompileResult, StepDef

logger = logging.getLogger("harness.repeat.semantic")

_PROMPT_TMPL = (
    "你是流程命名助手。以下是一段桌面自动化录制的步骤序列（已脱敏，不含实际输入值）。\n"
    "请为整个流程给出 name（≤20 字）与 description（≤60 字），并为每一步给出更业务化的"
    "中文 label（≤30 字，说明\"对什么做什么\"，不改变动作语义）。\n"
    "只输出 JSON：{\"name\":..., \"description\":..., \"labels\": [与步骤同序的字符串]}\n"
    "步骤：\n{steps}\n"
)


def _safe_input(steps: list[StepDef]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for s in steps:
        d: dict[str, Any] = {"sid": s.sid, "action": s.action}
        if s.label:
            d["label"] = s.label
        # 只透传控件语义（name/control_type），不透传 params.value（敏感）
        loc = s.locators[0] if s.locators else None
        if loc is not None:
            if loc.kind == "uia" and isinstance(loc.value, dict):
                d["target"] = {"name": loc.value.get("name", ""),
                               "type": loc.value.get("control_type", "")}
            elif loc.kind == "uia" and isinstance(loc.value, str):
                d["target"] = {"by": loc.by, "hint": loc.value[:40]}
        out.append(d)
    return out


async def apply_semantic(result: CompileResult,
                         chat_provider_getter: Callable[[], Any | None]) -> CompileResult:
    """调用 LLM 改写 label/标题/描述。任何失败 → 原样返回。"""
    getter = chat_provider_getter()
    if getter is None:
        logger.info("语义命名跳过：无可用 Provider")
        return result
    provider = None
    model = ""
    try:
        # 约定 ProviderRegistry.get_default()/first-enabled；取不到即跳过
        if hasattr(getter, "list_providers"):
            for pid in getter.list_providers():
                p = getter.get_provider(pid if isinstance(pid, str) else pid.get("id", ""),
                                         include_disabled=False)
                if p is not None:
                    provider = p
                    model = str(getattr(p, "models", [""])[0] if getattr(p, "models", None) else "")
                    break
        elif hasattr(getter, "get_provider"):
            provider = getter
        if provider is None:
            return result
        payload = _safe_input(result.steps)
        prompt = _PROMPT_TMPL.replace("{steps}", json.dumps(payload, ensure_ascii=False)[:6000])
        chunks: list[str] = []
        async for chunk in provider.chat([{"role": "user", "content": prompt}],
                                         model or "default", stream=True):
            d = chunk.get("delta")
            if isinstance(d, str):
                chunks.append(d)
        text = "".join(chunks).strip()
        parsed = _extract_json(text)
        if not isinstance(parsed, dict):
            return result
        labels = parsed.get("labels")
        if not isinstance(labels, list) or len(labels) != len(result.steps):
            return result  # 结构不符：拒绝应用（宁可不改）
        changed = 0
        for s, new_label in zip(result.steps, labels):
            nl = str(new_label).strip()[:60]
            if nl:
                s.label = nl
                s.ext["semantic_named"] = True
                changed += 1
        if parsed.get("name"):
            result.draft_graph.setdefault("meta", {})["semantic_name"] = str(parsed["name"])[:40]
        if parsed.get("description"):
            result.draft_graph.setdefault("meta", {})["semantic_description"] = str(parsed["description"])[:120]
        logger.info("语义命名完成：%s/%s 步已改写", changed, len(result.steps))
    except Exception as e:  # noqa: BLE001
        logger.info("语义命名失败（跳过）: %s", e)
    return result


def _extract_json(text: str) -> Any:
    import re
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
