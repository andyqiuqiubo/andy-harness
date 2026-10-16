"""工程强逆向 —— 流水线编排器。

按 REA → Frida → Firecracker → PentAGI → Evidence v2 顺序执行；单阶段失败不阻断整体
（失败原因记入结果），最终统一由 Evidence v2 固化，保证"跑过就有可校验的证据"。

运行状态三态语义：

- ``ok``：全部阶段真实执行且成功；
- ``degraded``：无失败，但存在降级替代（substituted）或跳过（skipped）的阶段；
- ``failed``：任一阶段失败，或编排器自身异常。

（历史缺陷：全链降级也记 ``ok``，CLI/前端无法区分"真跑通"和"全线替代"。）
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import traceback
import uuid
from pathlib import Path
from typing import Any

from harness.modules.evidence_v2.service import sha256_file
from harness.modules.reverse_engineering.models import (
    STAGE_EVIDENCE,
    STAGE_LABELS,
    STAGE_ORDER,
    STAGE_REBUILD,
    PipelineRun,
    RunContext,
    StageResult,
    StageStatus,
)
from harness.modules.reverse_engineering.progress import ProgressReporter, progress_store
from harness.modules.reverse_engineering.stages import build_stages

logger = logging.getLogger("harness.reverse_engineering")

# run_id 同时是目录名与 URL 段 —— 必须白名单校验，否则 config.run_id / 路径参数
# 可用 ".." 目录穿越（在 runs_dir 之外读写文件）。
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,64}$")

# index.json 读改写串行化（同进程并发跑流水线时避免丢记录）
_INDEX_LOCK = threading.Lock()

# 已做过「遗留 running 收敛」的 runs_dir（进程级去重：目录扫描每次实例化都做太浪费）
_RECONCILED_DIRS: set[str] = set()


def _reconcile_stale_runs(runs_dir: Path) -> int:
    """把磁盘上遗留的 ``running`` 记录收敛为 ``interrupted``（返回收敛条数）。

    流水线是**进程内后台任务**：后端重启后内存任务全部消失，磁盘上仍为
    ``running`` 的记录必然是上个进程被终止的残影，永远不会再有终态。
    不收敛的后果：历史列表长期挂着一串假 running，前端还会自动附加这些
    死 run 的进度流（SSE 永不关闭），把「选择文件 / 开始运行」按钮永久锁死。
    幂等：已是终态的记录不动；收敛只写 run.json 并同步索引状态。
    """
    now = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    stale_ids: list[str] = []
    for run_json in runs_dir.glob("*/run.json"):
        try:
            data = json.loads(run_json.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(data, dict) or data.get("status") != "running":
            continue
        run_id = str(data.get("run_id") or run_json.parent.name)
        data["status"] = "interrupted"
        data["finished_at"] = now
        data["error"] = "后端进程重启时运行仍在进行中，已被标记为 interrupted（结果不完整，请重新发起）"
        try:
            run_json.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            logger.exception("[rev-eng %s] interrupted 收敛落盘失败", run_id)
            continue
        stale_ids.append(run_id)

    if stale_ids:
        # 同步索引，让 GET /runs 历史列表立刻反映 interrupted
        try:
            with _INDEX_LOCK:
                idx_path = runs_dir / "index.json"
                idx: dict[str, Any] = {}
                if idx_path.is_file():
                    try:
                        loaded = json.loads(idx_path.read_text(encoding="utf-8"))
                        if isinstance(loaded, dict):
                            idx = loaded
                    except (json.JSONDecodeError, OSError):
                        idx = {}
                for run_id in stale_ids:
                    if run_id in idx and isinstance(idx[run_id], dict):
                        idx[run_id]["status"] = "interrupted"
                tmp = idx_path.with_suffix(".json.tmp")
                tmp.write_text(json.dumps(idx, ensure_ascii=False, indent=2), encoding="utf-8")
                os.replace(tmp, idx_path)
        except OSError:
            logger.exception("[rev-eng] interrupted 收敛的索引同步失败（run.json 已更新）")
    return len(stale_ids)


def _validate_run_id(run_id: Any) -> str:
    """校验 run_id 形态；非法即抛 ``ValueError``（REST 层转 400）。"""
    if not isinstance(run_id, str) or not _RUN_ID_RE.match(run_id):
        raise ValueError(f"非法 run_id：{run_id!r}（仅允许 6~64 位 [A-Za-z0-9_-]）")
    return run_id


def _run_status(results: list[StageResult]) -> str:
    """ok / degraded / failed 三态（全链降级曾被记成 ok，掩盖质量事实）。"""
    if any(r.status == StageStatus.FAILED for r in results):
        return "failed"
    if any(r.status in (StageStatus.SUBSTITUTED, StageStatus.SKIPPED) for r in results):
        return "degraded"
    return "ok"


class ReverseEngineeringPipeline:
    """端到端逆向流水线。"""

    def __init__(self, runs_dir: str | Path | None = None) -> None:
        src_root = Path(__file__).resolve().parents[4]
        self.runs_dir = Path(runs_dir) if runs_dir else src_root / "rev_eng" / "runs"
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        # 每个目录每进程只收敛一次（幂等，无副作用）；失败不影响服务
        if str(self.runs_dir) not in _RECONCILED_DIRS:
            _RECONCILED_DIRS.add(str(self.runs_dir))
            try:
                n = _reconcile_stale_runs(self.runs_dir)
                if n:
                    logger.info("[rev-eng] 启动收敛：%d 条遗留 running 记录已标记为 interrupted", n)
            except Exception:  # noqa: BLE001 —— 收敛绝不能挡住流水线服务
                logger.exception("[rev-eng] 遗留 running 收敛失败（忽略，不影响服务）")
        self._stages = build_stages()

    # ── 工具链体检 ────────────────────────────────────────────────────
    def toolchain_status(self) -> dict[str, Any]:
        items = []
        for st in self._stages:
            try:
                ok, why = st.check()
            except Exception as exc:  # noqa: BLE001 —— 体检自身绝不能把 /status 打成 500
                logger.exception("toolchain check crashed: %s", st.name)
                ok, why = False, f"体检异常：{exc}"
            items.append(
                {
                    "stage": st.name,
                    "label": STAGE_LABELS.get(st.name, st.name),
                    "target_engine": st.target_engine,
                    "available": ok,
                    "reason": "" if ok else why,
                    "will_engine": st.target_engine if ok else "local-substitute",
                }
            )
        return {
            "stages": items,
            "degraded": [i["stage"] for i in items if not i["available"] and i["stage"] != STAGE_EVIDENCE],
        }

    # ── 执行 ──────────────────────────────────────────────────────────
    async def run(
        self,
        subject: str | Path,
        config: dict[str, Any] | None = None,
        reporter: ProgressReporter | None = None,
    ) -> PipelineRun:
        subject_path = Path(subject)
        if not subject_path.is_file():
            raise FileNotFoundError(f"分析目标不存在：{subject_path}")

        cfg = dict(config or {})
        run_id = _validate_run_id(cfg["run_id"]) if cfg.get("run_id") else uuid.uuid4().hex[:12]
        selected = cfg.get("stages")
        if selected:
            if not isinstance(selected, (list, tuple)):
                raise ValueError("config.stages 必须是阶段名数组")
            unknown = [s for s in selected if s not in STAGE_ORDER]
            if unknown:
                raise ValueError(f"未知阶段：{unknown}；可选：{STAGE_ORDER}")

        run_dir = self.runs_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=True)

        rec = PipelineRun(
            run_id=run_id,
            subject_name=subject_path.name,
            subject_path=str(subject_path),
            subject_sha256=sha256_file(subject_path),
            created_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            run_dir=str(run_dir),
        )
        # 进度发射器：默认接入模块级单例总线（供 SSE 端点订阅）；可注入自定义
        # reporter 用于单测断言。
        rp = reporter or ProgressReporter(run_id, progress_store)
        ctx = RunContext(run_id=run_id, subject=subject_path, work_dir=run_dir, config=cfg, reporter=rp)

        # 起始落盘 running 快照：让 GET /runs/{id} 在后台执行期间即可看到"运行中"状态。
        self._persist(rec, ctx)

        total = len(self._stages)
        results: list[StageResult] = []
        try:
            for idx, stage in enumerate(self._stages):
                selected_stage = not (
                    cfg.get("stages") and stage.name not in cfg["stages"] and stage.name != STAGE_EVIDENCE
                )
                label = STAGE_LABELS.get(stage.name, stage.name)
                # ① 阶段开始：明确标识当前所处阶段（需求 1）
                rp.phase_start(stage.name, label, idx, total)
                if not selected_stage:
                    res = StageResult(
                        stage=stage.name,
                        status=StageStatus.SKIPPED,
                        engine="none",
                        target_engine=stage.target_engine,
                        summary="未在本次运行选中的阶段",
                    )
                else:
                    logger.info("[rev-eng %s] 阶段开始: %s", run_id, stage.name)
                    if stage.name == STAGE_REBUILD:
                        # 重建工具以 run.json 为唯一事实来源：先落盘一次当前快照，
                        # 保证它读到的是刚跑完的各阶段事实（而不是启动时的空快照）。
                        for r in results:
                            rec.stages[r.stage] = r
                        self._persist(rec, ctx)
                    try:
                        # 阶段契约：异常也必须转成 FAILED 结果（单阶段崩溃不得炸掉整个 run，
                        # 否则 run.json 都不会落盘，历史记录与证据全部丢失）
                        res = await stage.run(ctx)
                    except Exception as exc:  # noqa: BLE001
                        logger.exception("[rev-eng %s] 阶段崩溃: %s", run_id, stage.name)
                        res = StageResult(
                            stage=stage.name,
                            status=StageStatus.FAILED,
                            engine="none",
                            target_engine=stage.target_engine,
                            summary=f"阶段内部异常：{exc}",
                            error=traceback.format_exc()[-3000:],
                            logs=[f"ERROR: {exc}"],
                        )
                results.append(res)
                ctx.merge_facts(res.facts)
                ctx.logs.extend([f"[{res.stage}] {x}" for x in res.logs])
                logger.info("[rev-eng %s] 阶段结束: %s -> %s", run_id, stage.name, res.status.value)
                # ③ 阶段结束：明确标记该阶段完成（需求 3）
                rp.phase_done(stage.name, res.status.value, res.summary)
                if stage.name != STAGE_EVIDENCE:
                    # 把已完成的阶段结果注入 ctx，供 Evidence v2 阶段生成溯源与清单
                    cfg["_stage_results"] = list(results)
                    ctx.config = cfg

            for r in results:
                rec.stages[r.stage] = r
            ev = rec.stages.get(STAGE_EVIDENCE)
            if ev:
                rec.evidence_bundle_id = str(ev.facts.get("bundle_id", ""))
                rec.evidence_dir = str(ev.facts.get("evidence_dir", ""))
                rec.evidence_report = dict(ev.facts.get("verify") or {})
            rec.finished_at = time.strftime("%Y-%m-%dT%H:%M:%S%z")
            rec.status = _run_status(results)
        except Exception as exc:  # noqa: BLE001 —— 编排器自身异常：先落盘再抛
            logger.exception("[rev-eng %s] 流水线编排异常", run_id)
            for r in results:
                rec.stages[r.stage] = r
            rec.finished_at = time.strftime("%Y-%m-%dT%H:%M:%S%z")
            rec.status = "failed"
            rec.error = f"{exc}\n{traceback.format_exc()[-2000:]}"
            self._persist(rec, ctx)
            # 运行级异常也通知订阅者，避免前端进度面板空挂
            rp.run_error(rec.error)
            raise

        self._persist(rec, ctx)
        # 运行级完成：携带最终记录，前端据此收尾并拉取详情（需求 2/3 收口）
        rp.run_done(rec.to_dict())
        return rec

    # ── 运行记录存取 ──────────────────────────────────────────────────
    def _persist(self, rec: PipelineRun, ctx: RunContext) -> None:
        """落盘 run.json / run.log 并刷新索引；落盘失败只记日志，不再抛出。"""
        run_dir = Path(rec.run_dir)
        try:
            (run_dir / "run.json").write_text(json.dumps(rec.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
            (run_dir / "run.log").write_text("\n".join(ctx.logs), encoding="utf-8")
        except OSError:
            logger.exception("[rev-eng %s] run 记录落盘失败", rec.run_id)
        try:
            self._index(rec)
        except OSError:
            logger.exception("[rev-eng %s] 索引更新失败", rec.run_id)

    def _index_path(self) -> Path:
        return self.runs_dir / "index.json"

    def _read_index(self) -> dict[str, Any]:
        """读索引；损坏/不存在按空索引处理（旧版损坏即在列表接口抛 JSONDecodeError）。"""
        p = self._index_path()
        if not p.is_file():
            return {}
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            logger.warning("rev_eng/runs/index.json 损坏，按空索引处理")
            return {}
        return data if isinstance(data, dict) else {}

    def _index(self, rec: PipelineRun) -> None:
        entry = {
            "run_id": rec.run_id,
            "subject_name": rec.subject_name,
            "subject_sha256": rec.subject_sha256,
            "created_at": rec.created_at,
            "status": rec.status,
            "run_dir": rec.run_dir,
        }
        with _INDEX_LOCK:
            idx = self._read_index()
            idx[rec.run_id] = entry
            # 临时文件 + 原子替换：并发读者只会看到旧或新完整索引，不会读到半截
            tmp = self._index_path().with_suffix(".json.tmp")
            tmp.write_text(json.dumps(idx, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self._index_path())

    def list_runs(self) -> list[dict[str, Any]]:
        with _INDEX_LOCK:
            idx = self._read_index()
        return sorted(idx.values(), key=lambda x: str(x.get("created_at", "")), reverse=True)

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        try:
            _validate_run_id(run_id)
        except ValueError:
            return None
        p = self.runs_dir / run_id / "run.json"
        if not p.is_file():
            return None
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        return data if isinstance(data, dict) else None
