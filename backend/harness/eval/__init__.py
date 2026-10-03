"""E5 · Eval 回归框架。

提供任务级评测能力：把一批带期望的任务（EvalCase）跑过 AgentLoop，
按「答案关键词 + 工具调用 + 迭代/耗时 + 无错误」客观打分，聚合为可对比的报告。

用法：

- 离线 / 脚本化（测试与确定性回归）：
  ```python
  from harness.eval import EvalRunner, EvalCase
  runner = EvalRunner(provider_factory=lambda case: my_scripted_provider(case))
  report = await runner.run(cases)
  assert report.pass_rate == 1.0
  ```
- 真实模型（需在项目环境内、已配置 provider）：
  ```bash
  python -m harness.eval --cases tests/evals/eval_cases.json --provider deepseek
  ```

设计取舍：
- 评测框架只做「运行 + 打分 + 汇总」，不引入新依赖；cases 用 JSON 描述（可直接手写）。
- 每个 case 独立会话、独立 AgentLoop，互不污染；provider 由工厂按需构造。
- 打分维度稳定可复现，避免「感觉没坏」式回归。
"""

from __future__ import annotations

from harness.eval.cases import EvalCase, load_cases
from harness.eval.report import EvalCaseResult, EvalReport
from harness.eval.runner import EvalRunner
from harness.eval.scoring import ScoreExpectation, score_case

__all__ = [
    "EvalCase",
    "EvalCaseResult",
    "EvalReport",
    "EvalRunner",
    "ScoreExpectation",
    "load_cases",
    "score_case",
]
