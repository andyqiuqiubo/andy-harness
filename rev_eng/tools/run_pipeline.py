"""工程强逆向流水线 CLI —— 在不启动后端的情况下跑通六阶段并留下证据。

用法：
    python run_pipeline.py --subject <样本路径> [--notes "说明"] [--decompile-limit 3]
                           [--frida-timeout-ms 6000] [--sandbox-args "/?"] [--stages a,b,c]

产物落在 ``rev_eng/runs/<run_id>/``：
    run.json / run.log / artifacts/* / payloads/* / evidence.json / evidence.md
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[2]  # .../src
sys.path.insert(0, str(SRC_ROOT / "backend"))

from harness.modules.reverse_engineering.pipeline import (  # noqa: E402
    ReverseEngineeringPipeline,
)


async def main_async(args: argparse.Namespace) -> int:
    pipeline = ReverseEngineeringPipeline()
    status = pipeline.toolchain_status()
    print("── 工具链体检 ─────────────────────────────────────")
    for s in status["stages"]:
        flag = "可用" if s["available"] else "降级"
        print(f"  [{flag}] {s['stage']:<22} 目标引擎={s['target_engine']:<14} 实际={s['will_engine']}")
        if not s["available"]:
            print(f"        原因：{s['reason']}")
    print()

    config: dict[str, object] = {
        "decompile_limit": args.decompile_limit,
        "frida_timeout_ms": args.frida_timeout_ms,
        "sandbox_timeout": args.sandbox_timeout,
        "notes": args.notes,
        "subject_type": args.subject_type,
    }
    if args.sandbox_args:
        config["sandbox_args"] = [a for a in args.sandbox_args.split(" ") if a]
    if args.stages:
        config["stages"] = [s.strip() for s in args.stages.split(",") if s.strip()]

    print(f"── 开始运行：{args.subject} ──────────────────────")
    rec = await pipeline.run(args.subject, config)

    print("\n── 阶段结果 ───────────────────────────────────────")
    for name, st in rec.stages.items():
        print(f"  {name:<22} {st.status.value:<12} engine={st.engine:<18} {st.duration_ms:>6}ms")
        print(f"      {st.summary}")
        if st.error:
            print(f"      ERROR: {st.error}")

    print(f"\n── 运行 ID：{rec.run_id}  目录：{rec.run_dir}")
    if rec.evidence_bundle_id:
        v = rec.evidence_report or {}
        print(f"── Evidence v2：bundle={rec.evidence_bundle_id}  条目={v.get('items_checked')}  "
              f"校验={'通过' if v.get('ok') else '失败'}")
        if v.get("problems"):
            for p in v["problems"]:
                print(f"      ! {p}")
    print(json.dumps({"run_id": rec.run_id, "status": rec.status}, ensure_ascii=False))
    # degraded = 无失败但有阶段降级替代（本机 Windows 常态），不算运行失败
    return 0 if rec.status in ("ok", "degraded") else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="工程强逆向流水线")
    ap.add_argument("--subject", required=True)
    ap.add_argument("--notes", default="")
    ap.add_argument("--subject-type", default="pe32+")
    ap.add_argument("--decompile-limit", type=int, default=3)
    ap.add_argument("--frida-timeout-ms", type=int, default=6000)
    ap.add_argument("--sandbox-timeout", type=float, default=20)
    ap.add_argument("--sandbox-args", default="")
    ap.add_argument("--stages", default="")
    return asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
