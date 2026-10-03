"""报告 —— 聚合各 case 结果，产出可对比的通过率 / 成本 / 工具使用指标。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from harness.eval.cases import EvalCase
from harness.eval.scoring import CaseScore


@dataclass
class EvalCaseResult:
    """单 case 的运行 + 打分结果。"""

    case: EvalCase
    passed: bool
    content: str
    iterations: int
    latency_ms: int
    tool_calls: list[str]
    score: CaseScore
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """序列化为可 JSON 化的字典。"""
        return {
            "id": self.case.id,
            "passed": self.passed,
            "iterations": self.iterations,
            "latency_ms": self.latency_ms,
            "tool_calls": self.tool_calls,
            "tags": self.case.tags,
            "error": self.error,
            "score": self.score.to_dict(),
        }


@dataclass
class EvalReport:
    """整批评测的汇总报告。"""

    results: list[EvalCaseResult] = field(default_factory=list)
    model: str = ""
    duration_s: float = 0.0

    # ---- 汇总指标（在 add 时累计）----
    @property
    def total(self) -> int:
        """case 总数。"""
        return len(self.results)

    @property
    def passed_count(self) -> int:
        """通过数。"""
        return sum(1 for r in self.results if r.passed)

    @property
    def failed_count(self) -> int:
        """失败数。"""
        return self.total - self.passed_count

    @property
    def pass_rate(self) -> float:
        """通过率（0.0~1.0）。"""
        return self.passed_count / self.total if self.total else 0.0

    @property
    def total_latency_ms(self) -> int:
        """各 case 耗时之和。"""
        return sum(r.latency_ms for r in self.results)

    @property
    def total_iterations(self) -> int:
        """各 case 工具迭代数之和。"""
        return sum(r.iterations for r in self.results)

    @property
    def total_tool_calls(self) -> int:
        """工具调用总次数。"""
        return sum(len(r.tool_calls) for r in self.results)

    @property
    def tool_usage(self) -> dict[str, int]:
        """每个工具被调用的次数。"""
        counts: dict[str, int] = {}
        for r in self.results:
            for t in r.tool_calls:
                counts[t] = counts.get(t, 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))

    def tag_pass_rate(self) -> dict[str, dict[str, float]]:
        """按标签分组的通过率（便于看哪类能力弱）。"""
        agg: dict[str, list[int]] = {}
        for r in self.results:
            for tag in r.case.tags or ["(untagged)"]:
                agg.setdefault(tag, []).append(1 if r.passed else 0)
        return {
            tag: {
                "passed": sum(v),
                "total": len(v),
                "rate": sum(v) / len(v),
            }
            for tag, v in sorted(agg.items())
        }

    # ---- 导出 ----
    def to_dict(self) -> dict[str, Any]:
        """序列化为可 JSON 化的字典。"""
        return {
            "model": self.model,
            "total": self.total,
            "passed": self.passed_count,
            "failed": self.failed_count,
            "pass_rate": round(self.pass_rate, 4),
            "wall_duration_s": round(self.duration_s, 3),
            "total_latency_ms": self.total_latency_ms,
            "total_iterations": self.total_iterations,
            "total_tool_calls": self.total_tool_calls,
            "tool_usage": self.tool_usage,
            "tag_pass_rate": self.tag_pass_rate(),
            "results": [r.to_dict() for r in self.results],
        }

    def to_json(self, indent: int = 2) -> str:
        """导出为 JSON 文本。"""
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)

    def to_markdown(self) -> str:
        """导出为 Markdown 报告（含失败原因）。"""
        lines: list[str] = []
        lines.append(f"# Eval 报告 · {self.model or '(默认模型)'}")
        lines.append("")
        lines.append(
            f"**通过率 {self.passed_count}/{self.total} "
            f"= {self.pass_rate * 100:.1f}%** ｜ "
            f"失败 {self.failed_count} ｜ 墙钟 {self.duration_s:.1f}s ｜ "
            f"工具调用 {self.total_tool_calls} 次 ｜ 迭代 {self.total_iterations} 轮"
        )
        lines.append("")

        tu = self.tool_usage
        if tu:
            lines.append("## 工具使用")
            lines.append("")
            lines.append("| 工具 | 次数 |")
            lines.append("|---|---|")
            for name, n in tu.items():
                lines.append(f"| `{name}` | {n} |")
            lines.append("")

        tpr = self.tag_pass_rate()
        if len(tpr) > 1 or "(untagged)" not in tpr:
            lines.append("## 分类通过率")
            lines.append("")
            lines.append("| 分类 | 通过 / 总数 | 通过率 |")
            lines.append("|---|---|---|")
            for tag, v in tpr.items():
                lines.append(f"| {tag} | {v['passed']}/{v['total']} | {v['rate'] * 100:.0f}% |")
            lines.append("")

        lines.append("## 明细")
        lines.append("")
        lines.append("| 结果 | ID | 迭代 | 耗时(ms) | 工具 | 失败原因 |")
        lines.append("|---|---|---|---|---|---|")
        for r in self.results:
            mark = "✅" if r.passed else "❌"
            reason = "; ".join(r.score.reasons) if not r.passed else ""
            tools = ", ".join(f"`{t}`" for t in r.tool_calls)
            lines.append(f"| {mark} | {r.case.id} | {r.iterations} | {r.latency_ms} | {tools} | {reason} |")
        lines.append("")
        return "\n".join(lines)
