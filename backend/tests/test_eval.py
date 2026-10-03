"""E5 Eval 框架测试（离线 / 确定性：脚本化 provider，不依赖网络）。"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from harness.eval import EvalCase, EvalRunner, load_cases, score_case

EVAL_CASES_FILE = Path(__file__).parent / "evals" / "eval_cases.json"


class ScriptedProvider:
    """按预置脚本回放模型响应的 provider（一次模型调用一组 chunk）。"""

    def __init__(self, scripts: list[list[dict[str, Any]]]) -> None:
        self._scripts = scripts
        self._index = 0

    async def chat(
        self,
        messages: list[dict[str, str]],
        model: str,
        stream: bool = True,
        **kwargs: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        if self._index < len(self._scripts):
            chunks = self._scripts[self._index]
            self._index += 1
        else:
            chunks = [{"delta": "(脚本结束)"}]
        for chunk in chunks:
            yield chunk


# 每个 case id 对应的脚本
def _scripts_for(case: EvalCase) -> ScriptedProvider:
    """按 case 构造能满足期望的脚本 provider。"""
    if case.id == "chat-greeting":
        return ScriptedProvider([[{"delta": "你好，我是你的智能助手。"}]])
    if case.id == "tool-calculator":
        return ScriptedProvider(
            [
                [
                    {
                        "delta": "",
                        "tool_calls": [
                            {
                                "function": {
                                    "name": "calculator",
                                    "arguments": '{"expression": "123*456"}',
                                }
                            }
                        ],
                    }
                ],
                [{"delta": "123*456 = 56088"}],
            ]
        )
    if case.id == "tool-current-time":
        return ScriptedProvider(
            [
                [
                    {
                        "delta": "",
                        "tool_calls": [
                            {
                                "function": {
                                    "name": "current_time",
                                    "arguments": "{}",
                                }
                            }
                        ],
                    }
                ],
                [{"delta": "现在是 2026-09-30 12:00:00。"}],
            ]
        )
    if case.id == "tool-calculator-forbidden":
        return ScriptedProvider([[{"delta": "7 加 8 等于 15。"}]])
    return ScriptedProvider([[{"delta": "默认回答"}]])


class TestCaseLoading:
    """用例加载与校验。"""

    def test_load_from_file(self) -> None:
        cases = load_cases(EVAL_CASES_FILE)
        assert len(cases) == 4
        assert cases[0].id == "chat-greeting"
        assert cases[1].expect_tools == ["calculator"]

    def test_load_from_list(self) -> None:
        cases = load_cases(
            [
                {"id": "a", "prompt": "p1"},
                {"id": "b", "prompt": "p2", "tags": ["x"]},
            ]
        )
        assert [c.id for c in cases] == ["a", "b"]

    def test_load_from_object_with_cases(self, tmp_path: Path) -> None:
        f = tmp_path / "c.json"
        f.write_text(
            json.dumps({"meta": {}, "cases": [{"id": "a", "prompt": "p"}]}),
            encoding="utf-8",
        )
        cases = load_cases(f)
        assert len(cases) == 1

    def test_duplicate_ids_rejected(self) -> None:
        with pytest.raises(ValueError, match="重复"):
            load_cases([{"id": "a", "prompt": "p"}, {"id": "a", "prompt": "q"}])

    def test_empty_prompt_rejected(self) -> None:
        with pytest.raises(ValueError, match="prompt"):
            EvalCase(id="a", prompt="  ")

    def test_unknown_field_rejected(self) -> None:
        with pytest.raises(ValueError, match="未知字段"):
            load_cases([{"id": "a", "prompt": "p", "bogus": 1}])

    def test_missing_file(self) -> None:
        with pytest.raises(FileNotFoundError):
            load_cases("/no/such/file.json")


class TestScoring:
    """打分逻辑。"""

    def _fake_result(self, **kw: Any) -> Any:
        base: dict[str, Any] = {
            "content": "",
            "tool_calls_made": [],
            "iterations": 0,
            "latency_ms": 0,
            "error": None,
        }
        base.update(kw)

        class _R:
            def __init__(self, d: dict[str, Any]) -> None:
                for k, v in d.items():
                    setattr(self, k, v)

        return _R(base)

    def test_keywords_all_pass(self) -> None:
        case = EvalCase(id="a", prompt="p", expect_keywords=["x", "y"])
        s = score_case(case, self._fake_result(content="x y z"))
        assert s.passed
        assert s.missing_keywords == []

    def test_keywords_any_pass(self) -> None:
        case = EvalCase(id="a", prompt="p", expect_keywords=["x", "y"], keyword_mode="any")
        s = score_case(case, self._fake_result(content="only x here"))
        assert s.passed

    def test_missing_keyword_fails(self) -> None:
        case = EvalCase(id="a", prompt="p", expect_keywords=["x"])
        s = score_case(case, self._fake_result(content="nothing"))
        assert not s.passed
        assert s.missing_keywords == ["x"]

    def test_missing_tool_fails(self) -> None:
        case = EvalCase(id="a", prompt="p", expect_tools=["calculator"])
        s = score_case(case, self._fake_result())
        assert not s.passed
        assert s.missing_tools == ["calculator"]

    def test_forbidden_tool_fails(self) -> None:
        case = EvalCase(id="a", prompt="p", forbid_tools=["calculator"])
        s = score_case(
            case,
            self._fake_result(tool_calls_made=[{"tool_name": "calculator", "result": "1"}]),
        )
        assert not s.passed
        assert s.forbidden_hits == ["calculator"]

    def test_too_many_iterations_fails(self) -> None:
        case = EvalCase(id="a", prompt="p", max_iterations=2)
        s = score_case(case, self._fake_result(iterations=3))
        assert not s.passed

    def test_latency_limit_fails(self) -> None:
        case = EvalCase(id="a", prompt="p", max_latency_ms=100)
        s = score_case(case, self._fake_result(latency_ms=200))
        assert not s.passed

    def test_error_fails(self) -> None:
        case = EvalCase(id="a", prompt="p")
        s = score_case(case, self._fake_result(error="boom"))
        assert not s.passed


class TestEvalRunner:
    """端到端：跑种子评测集。"""

    @pytest.mark.asyncio
    async def test_seed_set_all_pass(self) -> None:
        cases = load_cases(EVAL_CASES_FILE)
        runner = EvalRunner(provider_factory=_scripts_for)
        report = await runner.run(cases)

        assert report.total == 4
        assert report.passed_count == 4
        assert report.failed_count == 0
        assert report.pass_rate == 1.0

    @pytest.mark.asyncio
    async def test_metrics_aggregation(self) -> None:
        cases = load_cases(EVAL_CASES_FILE)
        runner = EvalRunner(provider_factory=_scripts_for)
        report = await runner.run(cases)

        # calculator 与 current_time 各被调用一次
        assert report.tool_usage.get("calculator") == 1
        assert report.tool_usage.get("current_time") == 1
        assert report.total_tool_calls == 2
        # iterations 统计模型调用轮次：纯对话 2 例各 1 轮、工具 2 例各 2 轮 = 6
        assert report.total_iterations == 6
        assert report.duration_s >= 0

        tpr = report.tag_pass_rate()
        assert tpr["math"]["rate"] == 1.0
        assert tpr["constraint"]["rate"] == 1.0

    @pytest.mark.asyncio
    async def test_failure_is_reported(self) -> None:
        # 让所有 case 都返回不含期望关键词的脚本 → 有关键词期望的 case 失败
        cases = load_cases(EVAL_CASES_FILE)

        def bad_factory(case: EvalCase) -> ScriptedProvider:
            return ScriptedProvider([[{"delta": "完全不对"}]])

        runner = EvalRunner(provider_factory=bad_factory)
        report = await runner.run(cases)
        assert report.failed_count >= 1
        assert report.pass_rate < 1.0

    @pytest.mark.asyncio
    async def test_report_exports(self) -> None:
        cases = load_cases(EVAL_CASES_FILE)
        runner = EvalRunner(provider_factory=_scripts_for)
        report = await runner.run(cases)

        d = report.to_dict()
        assert d["total"] == 4
        assert d["pass_rate"] == 1.0

        js = report.to_json()
        assert json.loads(js)["total"] == 4

        md = report.to_markdown()
        assert "Eval 报告" in md
        assert "100%" in md

    @pytest.mark.asyncio
    async def test_cases_are_isolated(self) -> None:
        """重复跑同一 case 两次，结果应独立、互不污染。"""
        case = EvalCase(id="dup", prompt="p", expect_keywords=["你好"])

        def factory(c: EvalCase) -> ScriptedProvider:
            return ScriptedProvider([[{"delta": "你好呀"}]])

        runner = EvalRunner(provider_factory=factory)
        report = await runner.run([case, case])
        assert report.passed_count == 2
