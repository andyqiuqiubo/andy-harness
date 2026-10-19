"""逐步骤视觉核对：把"这一步做了什么 / 画面是否变化 / 是否生效"交给 VL 模型。

产物：bundle/vision.json
    {"generated_at", "model", "steps": [{"sid", "summary", "action",
                                          "screen_changed", "effective",
                                          "issues", "raw"}]}
消费方：审查面板 / REST GET；绝不参与回放决策（旁路）。
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from .client import BailianVisionClient, parse_json_answer

logger = logging.getLogger("harness.modules.desktop_repeat.vision")

_SYSTEM = ("你是桌面自动化录制流程的审查助手。给定同一步骤的前帧与后帧截图，"
           "判断这一步实际做了什么、画面是否发生变化、操作是否生效。"
           "只输出一个 JSON 对象，不要输出任何其他文字。")

_PROMPT_TMPL = """请对比这两张屏幕截图（图1=操作前，图2=操作后），并参考下面这条已录制的步骤元数据：

步骤: sid={sid}
动作: {action}
标签: {label}
定位目标: {target}

输出严格 JSON（字段固定）：
{{
  "summary": "一句话说明这一步实际做了什么（中文，≤40字）",
  "screen_changed": true/false,
  "effective": true/false,
  "issues": ["如果画面没变化/操作可疑/目标可能点错，用一句话说明；否则空数组"]
}}
注意：图2 与图1 完全相同或几乎无变化时 effective=false。"""


def build_prompt(step: dict[str, Any]) -> str:
    target = step.get("target") or {}
    locs = target.get("locators") or []
    loc_desc = "；".join(
        str(loc.get("by") or loc.get("kind") or "") + ":" +
        str(loc.get("value") or loc.get("anchor") or "")[:60]
        for loc in locs[:2]) or "（无定位器，坐标/相对点击）"
    return _PROMPT_TMPL.format(
        sid=step.get("sid", ""), action=step.get("action", ""),
        label=step.get("label", ""), target=loc_desc)


def _frame_pairs(steps: list[dict[str, Any]], bundle_dir: Path,
                 max_steps: int) -> list[tuple[dict[str, Any], list[Path]]]:
    """每步取 pre/post 两帧（存在才收）；无帧的步骤跳过（不烧 token）。"""
    out: list[tuple[dict[str, Any], list[Path]]] = []
    for st in steps:
        frames = ((st.get("evidence") or {}).get("frames") or [])
        paths = [bundle_dir / f for f in frames
                 if str(f).endswith((".jpg", ".png"))][:2]
        if len(paths) < 2:
            continue
        if not all(p.is_file() and p.stat().st_size > 0 for p in paths):
            continue
        out.append((st, paths))
        if len(out) >= max_steps:
            break
    return out


async def analyze_session(client: BailianVisionClient, bundle_dir: Path,
                          steps: list[dict[str, Any]],
                          max_steps: int = 24) -> dict[str, Any]:
    """对已封版会话做逐步视觉核对；返回 vision.json 的内容结构。"""
    pairs = _frame_pairs(steps, bundle_dir, max_steps)
    result: dict[str, Any] = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "model": client.model,
        "analyzed": 0, "skipped_no_frames": max(0, len(steps) - len(pairs)),
        "failed": 0, "steps": [],
    }
    for st, frames in pairs:
        answer = await client.analyze_images(frames, build_prompt(st), system=_SYSTEM)
        item: dict[str, Any] = {
            "sid": st.get("sid", ""), "action": st.get("action", ""),
            "summary": "", "screen_changed": None, "effective": None,
            "issues": [], "raw": "",
        }
        if answer is None:
            result["failed"] += 1
            item["issues"] = ["视觉分析调用失败"]
        else:
            item["raw"] = (answer.get("text") or "")[:500]
            obj = parse_json_answer(answer.get("text") or "")
            if obj:
                item["summary"] = str(obj.get("summary") or "")[:200]
                item["screen_changed"] = bool(obj.get("screen_changed"))
                item["effective"] = bool(obj.get("effective"))
                issues = obj.get("issues")
                if isinstance(issues, list):
                    item["issues"] = [str(x)[:200] for x in issues][:5]
            else:
                result["failed"] += 1
                item["issues"] = ["视觉分析返回不可解析"]
        result["steps"].append(item)
        result["analyzed"] += 1
    # 汇总：疑似未生效的步骤，供审查一眼定位
    result["suspects"] = [s["sid"] for s in result["steps"]
                          if s.get("effective") is False]
    return result
