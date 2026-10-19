"""S9 风险标注（§5.4）：危险词库命中 → risk 升级 / 强制人工。

词库来自 RepeatConfig.risk_words（竖线分隔正则 alternation）。
命中面：目标控件 name、窗口标题、步骤文本（type 内容不参与——内容可参数化）。
"""

from __future__ import annotations

import re


def compile_words(pipe_words: str) -> re.Pattern[str]:
    parts = [p for p in re.split(r"[|｜]", str(pipe_words or "")) if p.strip()]
    if not parts:
        return re.compile(r"(?!x)x")  # 永不命中
    return re.compile("|".join(re.escape(p.strip()) for p in parts), re.IGNORECASE)


def annotate(risk_re: re.Pattern[str], *, action: str, name: str = "",
             title: str = "", base_risk: str = "write") -> tuple[str, bool, str | None]:
    """返回 (risk, needs_review, reason)。"""
    hay = f"{name}|{title}"
    if risk_re.search(hay or ""):
        return "dangerous", True, f"命中危险词库：{action} 目标『{name or title}』"
    return base_risk, False, None
