"""EvalCase —— 评测任务定义与加载。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class EvalCase:
    """单个评测任务。

    Attributes:
        id: 稳定唯一标识（用于报告与历史对比）。
        prompt: 发给 Agent 的用户消息。
        expect_keywords: 终答中应出现的关键词（默认全部命中，见 `keyword_mode`）。
        keyword_mode: `"all"`（默认，全部命中）或 `"any"`（命中任一即可）。
        expect_tools: 期望被调用的工具名（子集即可，顺序不限）。
        forbid_tools: 不应被调用的工具名（命中即失败）。
        max_iterations: 允许的最大工具迭代数（超出失败）。
        max_latency_ms: 允许的最大耗时（毫秒，0 表示不限制）。
        model: 指定模型（None 用运行器默认）。
        tags: 分组标签（便于按能力维度筛选）。
        notes: 人工备注（不参与打分）。
    """

    id: str
    prompt: str
    expect_keywords: list[str] = field(default_factory=list)
    keyword_mode: str = "all"
    expect_tools: list[str] = field(default_factory=list)
    forbid_tools: list[str] = field(default_factory=list)
    max_iterations: int = 10
    max_latency_ms: int = 0
    model: str | None = None
    tags: list[str] = field(default_factory=list)
    notes: str = ""

    def __post_init__(self) -> None:
        if not self.id or not self.id.strip():
            raise ValueError("EvalCase.id 不能为空")
        if not self.prompt or not self.prompt.strip():
            raise ValueError(f"EvalCase {self.id}: prompt 不能为空")
        if self.keyword_mode not in {"all", "any"}:
            raise ValueError(f"EvalCase {self.id}: keyword_mode 只能是 'all' 或 'any'")
        if self.max_iterations < 0:
            raise ValueError(f"EvalCase {self.id}: max_iterations 不能为负")
        if self.max_latency_ms < 0:
            raise ValueError(f"EvalCase {self.id}: max_latency_ms 不能为负")

    def to_dict(self) -> dict[str, Any]:
        """序列化为可 JSON 化的字典。"""
        return {
            "id": self.id,
            "prompt": self.prompt,
            "expect_keywords": self.expect_keywords,
            "keyword_mode": self.keyword_mode,
            "expect_tools": self.expect_tools,
            "forbid_tools": self.forbid_tools,
            "max_iterations": self.max_iterations,
            "max_latency_ms": self.max_latency_ms,
            "model": self.model,
            "tags": self.tags,
            "notes": self.notes,
        }


def _coerce_case(raw: Any) -> EvalCase:
    """从 dict 构造 EvalCase，未知字段忽略并给出提示。"""
    if not isinstance(raw, dict):
        raise ValueError(f"评测任务必须是对象，收到 {type(raw).__name__}")
    known = {
        "id",
        "prompt",
        "expect_keywords",
        "keyword_mode",
        "expect_tools",
        "forbid_tools",
        "max_iterations",
        "max_latency_ms",
        "model",
        "tags",
        "notes",
    }
    unknown = set(raw) - known
    if unknown:
        raise ValueError(f"EvalCase {raw.get('id')}: 未知字段 {sorted(unknown)}")
    return EvalCase(**raw)


def load_cases(source: str | Path | list[dict[str, Any]]) -> list[EvalCase]:
    """加载评测任务。

    Args:
        source: JSON 文件路径（顶层为 case 列表，或含 `cases` 键的对象），
            或直接传入 case dict 列表。

    Returns:
        EvalCase 列表（保持原顺序）。
    """
    if isinstance(source, list):
        data: Any = source
    else:
        path = Path(source)
        if not path.exists():
            raise FileNotFoundError(f"评测文件不存在: {path}")
        text = path.read_text(encoding="utf-8")
        data = json.loads(text)

    if isinstance(data, dict) and "cases" in data:
        data = data["cases"]
    if not isinstance(data, list):
        raise ValueError("评测文件顶层必须是任务列表，或含 'cases' 列表的对象")

    cases = [_coerce_case(item) for item in data]
    ids = [c.id for c in cases]
    dup = {i for i in ids if ids.count(i) > 1}
    if dup:
        raise ValueError(f"评测任务 id 重复: {sorted(dup)}")
    return cases
