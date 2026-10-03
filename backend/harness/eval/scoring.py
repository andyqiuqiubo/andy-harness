"""打分 —— 把 AgentLoopResult 对照 EvalCase 期望判为通过 / 失败。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from harness.eval.cases import EvalCase


@dataclass
class ScoreExpectation:
    """单条期望的判定结果（用于报告里解释失败原因）。"""

    kind: str  # keyword | tool | forbid_tool | iterations | latency | error
    passed: bool
    detail: str = ""


@dataclass
class CaseScore:
    """单 case 的打分明细。"""

    passed: bool
    checks: list[ScoreExpectation] = field(default_factory=list)
    matched_keywords: list[str] = field(default_factory=list)
    missing_keywords: list[str] = field(default_factory=list)
    called_tools: list[str] = field(default_factory=list)
    missing_tools: list[str] = field(default_factory=list)
    forbidden_hits: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """序列化为可 JSON 化的字典。"""
        return {
            "passed": self.passed,
            "matched_keywords": self.matched_keywords,
            "missing_keywords": self.missing_keywords,
            "called_tools": self.called_tools,
            "missing_tools": self.missing_tools,
            "forbidden_hits": self.forbidden_hits,
            "reasons": self.reasons,
            "checks": [{"kind": c.kind, "passed": c.passed, "detail": c.detail} for c in self.checks],
        }


def score_case(case: EvalCase, result: Any) -> CaseScore:
    """对一次运行结果打分。

    Args:
        case: 评测任务及期望。
        result: `AgentLoopResult`（只要含 content / tool_calls_made /
            iterations / latency_ms / error 字段即可）。

    Returns:
        CaseScore
    """
    content = result.content or ""
    called = [tc.get("tool_name") for tc in (result.tool_calls_made or [])]
    called = [t for t in called if t]
    iterations = getattr(result, "iterations", 0)
    latency_ms = getattr(result, "latency_ms", 0)
    error = getattr(result, "error", None)

    checks: list[ScoreExpectation] = []
    reasons: list[str] = []

    # 1) 无错误
    err_ok = not error
    checks.append(ScoreExpectation("error", err_ok, "运行无错误" if err_ok else f"运行错误: {error}"))
    if not err_ok:
        reasons.append(f"运行错误: {error}")

    # 2) 关键词
    missing_kw = [kw for kw in case.expect_keywords if kw not in content]
    matched_kw = [kw for kw in case.expect_keywords if kw in content]
    if case.keyword_mode == "all":
        kw_ok = not missing_kw
        detail = (
            f"关键词全部命中（{len(matched_kw)}/{len(case.expect_keywords)}）" if kw_ok else f"缺少关键词: {missing_kw}"
        )
    else:
        kw_ok = (
            len(case.expect_keywords) == 0  # 没期望时不算失败
            or len(matched_kw) > 0
        )
        detail = "命中至少一个关键词" if kw_ok else f"未命中任一关键词，期望: {case.expect_keywords}"
    checks.append(ScoreExpectation("keyword", kw_ok, detail))
    if not kw_ok:
        reasons.append(detail)

    # 3) 期望工具（子集）
    missing_tools = [t for t in case.expect_tools if t not in called]
    tools_ok = not missing_tools
    checks.append(
        ScoreExpectation(
            "tool",
            tools_ok,
            "期望工具均被调用" if tools_ok else f"未调用期望工具: {missing_tools}",
        )
    )
    if not tools_ok:
        reasons.append(f"未调用期望工具: {missing_tools}")

    # 4) 禁用工具
    forbidden_hits = [t for t in case.forbid_tools if t in called]
    forbid_ok = not forbidden_hits
    checks.append(
        ScoreExpectation(
            "forbid_tool",
            forbid_ok,
            "未调用禁用工具" if forbid_ok else f"调用了禁用工具: {forbidden_hits}",
        )
    )
    if not forbid_ok:
        reasons.append(f"调用了禁用工具: {forbidden_hits}")

    # 5) 迭代数
    iter_ok = iterations <= case.max_iterations
    checks.append(
        ScoreExpectation(
            "iterations",
            iter_ok,
            f"迭代数 {iterations} ≤ {case.max_iterations}"
            if iter_ok
            else f"迭代数 {iterations} 超过上限 {case.max_iterations}",
        )
    )
    if not iter_ok:
        reasons.append(f"迭代数 {iterations} 超过上限 {case.max_iterations}")

    # 6) 耗时
    if case.max_latency_ms > 0:
        lat_ok = latency_ms <= case.max_latency_ms
        checks.append(
            ScoreExpectation(
                "latency",
                lat_ok,
                f"耗时 {latency_ms}ms ≤ {case.max_latency_ms}ms"
                if lat_ok
                else f"耗时 {latency_ms}ms 超过上限 {case.max_latency_ms}ms",
            )
        )
        if not lat_ok:
            reasons.append(f"耗时 {latency_ms}ms 超过上限 {case.max_latency_ms}ms")

    passed = all(c.passed for c in checks)
    return CaseScore(
        passed=passed,
        checks=checks,
        matched_keywords=matched_kw,
        missing_keywords=missing_kw,
        called_tools=called,
        missing_tools=missing_tools,
        forbidden_hits=forbidden_hits,
        reasons=reasons,
    )
