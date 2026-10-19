"""CLI 入口（§10.5）：repeat record|compile|replay|dry-run|skill|inspect。

harness 内调用：python -m harness.modules.desktop_repeat.cli …
独立包形态（andy-repeat）复用同一实现。
CLI 运行在用户终端（非受限沙箱）时，钩子/UIA/注入完整可用；
若被沙箱拦截则如实报错（RP-7003）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any


def _default_db() -> Any:
    from harness.infra.database import Database
    return Database()


def _default_sessions_root() -> Path:
    from harness.modules.artifact_store.service import artifacts_dir
    p = Path(artifacts_dir()) / "repeat" / "sessions"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _service(db: Any = None) -> Any:
    from .config import RepeatConfig
    from .service import DesktopRepeatService
    db = db or _default_db()
    cfg = RepeatConfig.load()
    try:
        from harness.modules.workflows.service import WorkflowService  # harness 内运行时
        wfsvc = WorkflowService(db, None, None)  # type: ignore[arg-type]
    except Exception:  # noqa: BLE001
        wfsvc = None
    return DesktopRepeatService(db, _default_sessions_root(), cfg, workflow_service=wfsvc)


async def cmd_record(args: argparse.Namespace) -> int:
    from .config import RecordConfig
    svc = _service()
    rec = RecordConfig(label=args.label or "", frame_tier=args.tier or "",
                       countdown_s=int(args.countdown) if args.countdown is not None else -1)
    doc = await svc.start_recording(rec)
    print(json.dumps({"session_id": doc.get("session_id"), "status": "recording",
                      "hint": "Ctrl+Alt+P 暂停 / Ctrl+Alt+X 丢弃；再次运行 record --stop 结束"},
                     ensure_ascii=False))
    return 0


async def cmd_record_stop(args: argparse.Namespace) -> int:
    svc = _service()
    doc = await svc.stop_recording(args.session or None, discard=bool(args.discard))
    print(json.dumps({k: doc.get(k) for k in ("session_id", "status", "counters", "error")},
                     ensure_ascii=False, indent=1))
    return 0 if doc.get("status") == "ready" else 2


async def cmd_compile(args: argparse.Namespace) -> int:
    svc = _service()
    sid = args.target
    if not (sid or "").startswith("r-"):
        p = Path(sid)
        sid = p.name
    res = await svc.compile_session(sid)
    out = res.to_dict()
    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=1))
    else:
        st = res.stats
        print(f"步骤 {st.steps}（低置信 {st.low_confidence}，需确认 {st.needs_review}，"
              f"循环候选 {st.loop_candidates}，参数候选 {st.param_candidates}）"
              f"耗时 {st.duration_ms}ms")
        for w in res.warnings[:30]:
            print(f"  [{w.severity}] {w.sid}: {w.code} {w.message}")
        if args.name:
            wf = await svc.save_as_workflow(sid, args.name)
            print(f"已存工作流: {wf.get('id')}（画布可编辑后发布）")
    return 0


async def cmd_dry_run(args: argparse.Namespace) -> int:
    svc = _service()
    params = _load_params(args)
    rep = await svc.dry_run(args.workflow, params)
    print(json.dumps(rep, ensure_ascii=False, indent=1))
    return 0 if rep.get("ok") else 2


async def cmd_replay(args: argparse.Namespace) -> int:
    svc = _service()
    params = _load_params(args)
    res = await svc.start_run(args.workflow, params,
                              unattended=not sys.stdin.isatty(),
                              allow_blind_coord=bool(args.allow_blind_coord))
    run_id = res.get("run_id")
    if res.get("rejected"):
        print(json.dumps(res, ensure_ascii=False, indent=1))
        return 2
    print(f"run={run_id} 执行中…（Ctrl+Alt+. 紧急停止）")
    async for ev in svc.subscribe(run_id):
        t = ev.get("type")
        if t == "step_done":
            print(f"  ✔ {ev.get('sid')} [{ev.get('kind')}@{ev.get('hit_index')}] {ev.get('elapsed_ms')}ms")
        elif t == "step_failed":
            print(f"  ✘ {ev.get('sid')} {ev.get('code')} {ev.get('message')}")
        elif t in ("run_done", "run_error"):
            rec = ev.get("record") or {}
            print("完成:", json.dumps(rec.get("stats", {}), ensure_ascii=False) if rec else ev.get("message"))
            break
        elif t == "run_suspended":
            print("挂起等待人工:", ev.get("code"), ev.get("sid"))
            break
    return 0


async def cmd_skill(args: argparse.Namespace) -> int:
    svc = _service()
    res = await svc.export_skill(args.workflow, slug=args.slug)
    print(json.dumps(res, ensure_ascii=False, indent=1))
    return 0


async def cmd_inspect(args: argparse.Namespace) -> int:
    svc = _service()
    if args.target.startswith("run-"):
        print(json.dumps(svc.get_run(args.target), ensure_ascii=False, indent=1))
    else:
        print(json.dumps(svc.get_session(args.target), ensure_ascii=False, indent=1))
    return 0


def _load_params(args: argparse.Namespace) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if getattr(args, "params", None):
        p = Path(args.params)
        if p.exists():
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
                out.update({str(k): str(v) for k, v in d.items()})
            except Exception as e:  # noqa: BLE001
                print(f"--params 解析失败: {e}", file=sys.stderr)
    for item in getattr(args, "param", None) or []:
        if "=" in item:
            k, v = item.split("=", 1)
            out[k.strip()] = v
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="repeat", description="andy-repeat CLI")
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("record", help="开始录制（--stop 结束）")
    r.add_argument("--label", default="")
    r.add_argument("--tier", default="", help="minimal|standard|forensic")
    r.add_argument("--countdown", default=None)
    r.add_argument("--stop", action="store_true")
    r.add_argument("--session", default="")
    r.add_argument("--discard", action="store_true")

    c = sub.add_parser("compile", help="事件流 → 步骤")
    c.add_argument("target", help="session id 或 bundle 路径")
    c.add_argument("--name", default="", help="编译并保存为工作流")
    c.add_argument("--json", action="store_true")

    d = sub.add_parser("dry-run", help="只解析不注入的体检")
    d.add_argument("workflow")
    d.add_argument("--params", default="")
    d.add_argument("--param", action="append")

    p = sub.add_parser("replay", help="回放")
    p.add_argument("workflow")
    p.add_argument("--params", default="")
    p.add_argument("--param", action="append")
    p.add_argument("--allow-blind-coord", action="store_true", dest="allow_blind_coord")

    s = sub.add_parser("skill", help="导出技能 SKILL.md")
    s.add_argument("workflow")
    s.add_argument("--slug", default="")

    i = sub.add_parser("inspect", help="查看会话/运行")
    i.add_argument("target")

    args = ap.parse_args(argv)
    if args.cmd == "record":
        return asyncio.run(cmd_record_stop(args) if args.stop else cmd_record(args))
    fn = {"compile": cmd_compile, "dry-run": cmd_dry_run, "replay": cmd_replay,
          "skill": cmd_skill, "inspect": cmd_inspect}[args.cmd]
    try:
        return asyncio.run(fn(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
