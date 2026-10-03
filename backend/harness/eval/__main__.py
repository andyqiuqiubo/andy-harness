"""`python -m harness.eval` —— 运行评测任务集（真实模型）。

示例：
    python -m harness.eval --cases tests/evals/eval_cases.json --provider deepseek
    python -m harness.eval -c tests/evals/eval_cases.json -p deepseek --tag math --report out.md
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any

from harness.eval.cases import load_cases
from harness.eval.report import EvalReport
from harness.eval.runner import EvalRunner


async def _bootstrap_live_provider(provider_id: str, model: str | None) -> Any:
    """复用应用装配，启动插件并取出指定 provider。"""
    import harness.main as app_main

    await app_main._init_components()
    import pathlib

    plugins_dir = pathlib.Path(app_main.__file__).resolve().parent.parent / "plugins"
    await app_main._loader.load_and_activate_all(plugins_dir)

    from harness.modules.model_manager.provider_registry import ProviderRegistry

    registry = app_main._services.get(ProviderRegistry)
    return registry.get_provider(provider_id)


async def _main_async(args: argparse.Namespace) -> int:
    cases = load_cases(args.cases)
    if args.tag:
        cases = [c for c in cases if args.tag in c.tags]
    if not cases:
        print("没有匹配的评测任务。", file=sys.stderr)
        return 2

    if not args.provider:
        print(
            "在线评测需要 --provider <id>。可先在设置页查看已配置的 provider。",
            file=sys.stderr,
        )
        return 2

    provider = await _bootstrap_live_provider(args.provider, args.model)
    default_model = args.model or getattr(provider, "default_model", "live-model")

    runner = EvalRunner(
        provider_factory=lambda case: provider,
        default_model=default_model,
    )
    report = await runner.run(cases)
    report.model = default_model

    _emit(report, args.report)
    return 0 if report.failed_count == 0 else 1


def _emit(report: EvalReport, report_path: str | None) -> None:
    """输出报告：控制台 Markdown，可另存文件。"""
    if report_path:
        path = Path(report_path)
        if path.suffix.lower() == ".json":
            path.write_text(report.to_json(), encoding="utf-8")
        else:
            path.write_text(report.to_markdown(), encoding="utf-8")
        print(f"报告已写入: {path}")
    print(report.to_markdown())


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="运行 Eval 评测任务集")
    parser.add_argument("-c", "--cases", required=True, help="评测任务 JSON 文件路径")
    parser.add_argument("-p", "--provider", default="", help="provider id（在线评测必填）")
    parser.add_argument("-m", "--model", default=None, help="模型名覆盖")
    parser.add_argument("-t", "--tag", default=None, help="仅运行含某标签的任务")
    parser.add_argument("-r", "--report", default=None, help="报告输出路径（.md / .json）")
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    return asyncio.run(_main_async(args))


if __name__ == "__main__":
    sys.exit(main())
