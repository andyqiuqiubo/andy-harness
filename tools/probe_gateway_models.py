#!/usr/bin/env python3
"""真机探测网关免费模型：对每个模型发一次最小请求，判定是否**真实可用**。

与 `tools/check_gateway.py`（只查登录态/注册/快照，不发请求）互补：本脚本会
真实调用上游，用来回答"这个模型到底能不能用、有多快"。

用法（必须在 backend 目录下、使用 backend 的虚拟环境，以便读到本机登录态）：

    cd backend
    python ../tools/probe_gateway_models.py                      # 全量探测
    python ../tools/probe_gateway_models.py zen/big-pickle       # 只测指定模型
    python ../tools/probe_gateway_models.py --timeout 90 -c 2    # 调整超时与并发

输出只包含模型 id、判定、耗时与回复片段，**不输出任何 token / 邮箱 / 机器码**。

判定含义：
    OK                有正文内容，可用
    ONLY-REASONING    只有思维链、没有正文（界面可能显示为空）
    EMPTY             200 但无内容
    ERR:<code>        上游明确报错（code 即网关错误码，如 REGION_BLOCKED）
    TIMEOUT           超过 --timeout 仍未返回（上游过载/挂起）

⚠️ 陷阱：同一轮探测中，某个模型若触发**通道级冷却**（如 503 / 429，默认冷却 30s），
同通道的后续模型会被网关直接挡下并返回 `GW_NO_CAPACITY`——这是探测自身的副作用，
**不代表那些模型不可用**。要复测请用 `-c 1`（串行）逐个跑。
"""

from __future__ import annotations

import argparse
import asyncio
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

# 允许从仓库任意位置启动：把 backend 加入 sys.path
BACKEND = Path(__file__).resolve().parents[1] / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from harness.kernel.services import ServiceRegistry  # noqa: E402
from harness.modules.gateway.auth import KeyContext  # noqa: E402
from harness.modules.gateway.contracts import GatewayError  # noqa: E402
from harness.modules.gateway.service import GatewayService  # noqa: E402

CONFIG = BACKEND / "data" / "gateway.json"
PROMPT = "只回复两个字：可用"


class _FakeDB:
    """最小内存 sqlite，满足 gateway 的 execute/query/query_one 协议。"""

    def __init__(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        cur = self.conn.execute(sql, params)
        self.conn.commit()
        return cur

    def query(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        return list(self.conn.execute(sql, params).fetchall())

    def query_one(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, params).fetchone()


async def probe(
    svc: GatewayService, ctx: KeyContext, model: str, timeout: float
) -> tuple[str, str, float, str]:
    """对单个模型发一次最小请求，返回 (模型, 判定, 耗时, 片段)。"""
    started = time.perf_counter()
    try:
        res = await asyncio.wait_for(
            svc.chat(
                {
                    "model": model,
                    "stream": False,
                    "messages": [{"role": "user", "content": PROMPT}],
                },
                ctx,
            ),
            timeout=timeout,
        )
    except GatewayError as exc:
        return model, f"ERR:{exc.code}", time.perf_counter() - started, exc.message[:70]
    except TimeoutError:
        return model, "TIMEOUT", time.perf_counter() - started, ""
    except Exception as exc:  # noqa: BLE001 —— 探测脚本要兜住一切异常
        return model, f"EXC:{type(exc).__name__}", time.perf_counter() - started, str(exc)[:70]

    payload: dict[str, Any] = dict(res.payload or {})
    choices = payload.get("choices") or [{}]
    message = (choices[0] or {}).get("message") or {}
    content = str(message.get("content") or "").strip()
    reasoning = str(message.get("reasoning_content") or "").strip()
    if content:
        verdict = "OK"
    elif reasoning:
        verdict = "ONLY-REASONING"
    else:
        verdict = "EMPTY"
    return model, verdict, time.perf_counter() - started, content[:50]


async def main() -> int:
    parser = argparse.ArgumentParser(description="真机探测网关免费模型是否可用")
    parser.add_argument("models", nargs="*", help="只探测指定模型（默认全部）")
    parser.add_argument("--timeout", type=float, default=60.0, help="单请求超时秒数")
    parser.add_argument("-c", "--concurrency", type=int, default=3, help="并发数")
    args = parser.parse_args()

    if not CONFIG.exists():
        print(f"找不到 {CONFIG} —— 先启动过一次后端以生成运行时配置")
        return 1

    svc = GatewayService(
        _FakeDB(),
        ServiceRegistry(),
        config_path=CONFIG,
        bootstrap_key="agw-probe",
        watch_config=False,
    )
    await svc.start()
    ctx = KeyContext(
        tenant_id="default",
        key_id="probe",
        name="probe",
        scopes=["*"],
        allowed_models=["*"],
        allowed_channels=["*"],
        rpm=10000,
        tpm=10_000_000,
    )
    try:
        snapshot = svc.availability_snapshot()
        print("=== 启动时可用性快照（界面视角）===")
        for cid, info in snapshot["channels"].items():
            models = info.get("models") or {}
            if not models:
                continue
            usable = sum(1 for m in models.values() if m.get("available"))
            print(
                f"  {cid:<12} enabled={info.get('enabled')} "
                f"available={info.get('available')} 模型 {usable}/{len(models)}"
            )

        targets = args.models or [
            f"{cid}/{model}"
            for cid, info in snapshot["channels"].items()
            for model in (info.get("models") or {})
        ]
        print(f"\n=== 开始探测 {len(targets)} 个模型（并发 {args.concurrency}，上限 {args.timeout:.0f}s）===")
        gate = asyncio.Semaphore(max(1, args.concurrency))

        async def run(model: str) -> tuple[str, str, float, str]:
            async with gate:
                result = await probe(svc, ctx, model, args.timeout)
                print(f"  {result[0]:<42} {result[1]:<20} {result[2]:6.1f}s  {result[3]}")
                return result

        results = await asyncio.gather(*[run(m) for m in targets])
        ok = [r for r in results if r[1] == "OK"]
        print("\n=== 汇总 ===")
        print(f"真实可用：{len(ok)}/{len(results)}")
        for row in sorted(results, key=lambda r: (r[1] != "OK", r[0])):
            print(f"  {row[1]:<20} {row[0]:<42} {row[2]:6.1f}s {row[3]}")
        return 0 if ok else 2
    finally:
        await svc.stop()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
