"""评测实验室（Eval Lab）—— 数据集管理与模型 × 提示词对比运行（P1）。

复用既有评测框架（``harness.eval`` 的 EvalRunner / score_case / EvalReport），
新增「数据集持久化」与「矩阵对比运行」：

- 数据集：用例格式与 ``tests/evals/eval_cases.json`` 完全一致
  （expect_keywords / keyword_mode / expect_tools / forbid_tools / tags）。
- 对比运行：矩阵 = 组合列表 [{provider_id, model, system_prompt?}] × 数据集全部用例，
  每个组合一次 EvalRunner.run，结果逐组合写回 eval_runs 行（前端轮询进度）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime
from typing import Any

from harness.eval import EvalRunner, load_cases
from harness.infra.database import Database

logger = logging.getLogger("harness.evals_lab")


class EvalLabService:
    """评测实验室服务：数据集 CRUD + 对比运行编排。"""

    def __init__(self, db: Database, tool_registry: Any = None) -> None:
        self._db = db
        self._tool_registry = tool_registry
        # 测试可注入：provider_id -> provider
        self._provider_factory: Any = None

    def set_provider_factory(self, factory: Any) -> None:
        """覆盖 provider 解析（测试注入脚本化 provider 用）。签名 `(provider_id) -> provider`。"""
        self._provider_factory = factory

    # ── 数据集 ──────────────────────────────────────
    def list_datasets(self) -> list[dict[str, Any]]:
        rows = self._db.query(
            "SELECT id, name, description, cases_json, created_at FROM eval_datasets ORDER BY created_at DESC"
        )
        out = []
        for r in rows:
            d = dict(r)
            d["cases"] = json.loads(d.pop("cases_json") or "[]")
            d["case_count"] = len(d["cases"])
            out.append(d)
        return out

    def create_dataset(self, name: str, description: str, cases: list[dict[str, Any]]) -> dict[str, Any]:
        # 校验：复用评测框架的用例解析（字段缺失/类型错误会抛出）
        validated = load_cases(cases)
        ds_id = uuid.uuid4().hex[:12]
        now = datetime.now().isoformat(timespec="seconds")
        self._db.execute(
            "INSERT INTO eval_datasets (id, name, description, cases_json, created_at) VALUES (?, ?, ?, ?, ?)",
            (ds_id, name, description, json.dumps([c.to_dict() for c in validated], ensure_ascii=False), now),
        )
        return self.get_dataset(ds_id)  # type: ignore[return-value]

    def get_dataset(self, dataset_id: str) -> dict[str, Any] | None:
        row = self._db.query_one(
            "SELECT id, name, description, cases_json, created_at FROM eval_datasets WHERE id = ?",
            (dataset_id,),
        )
        if row is None:
            return None
        d = dict(row)
        d["cases"] = json.loads(d.pop("cases_json") or "[]")
        d["case_count"] = len(d["cases"])
        return d

    def delete_dataset(self, dataset_id: str) -> bool:
        """删除数据集，并级联删除其全部运行记录。

        `eval_runs` 未声明外键级联，若不显式清理，删除数据集后其历史运行
        会成为永久孤儿数据（且没有任何 API 能再删除它们）。
        """
        self._db.execute("DELETE FROM eval_runs WHERE dataset_id = ?", (dataset_id,))
        cur = self._db.execute("DELETE FROM eval_datasets WHERE id = ?", (dataset_id,))
        return cur.rowcount > 0

    def delete_run(self, run_id: str) -> bool:
        """删除单条评测运行记录。"""
        cur = self._db.execute("DELETE FROM eval_runs WHERE id = ?", (run_id,))
        return cur.rowcount > 0

    # ── 运行 ────────────────────────────────────────
    def create_run(self, dataset_id: str, combos: list[dict[str, Any]]) -> dict[str, Any]:
        """创建对比运行并调度后台执行。须在事件循环内调用。"""
        dataset = self.get_dataset(dataset_id)
        if dataset is None:
            raise KeyError(f"数据集不存在: {dataset_id}")
        if not dataset["cases"]:
            raise ValueError("数据集没有任何用例")
        if not combos:
            raise ValueError("对比矩阵不能为空（至少一个 组合）")
        run_id = uuid.uuid4().hex[:12]
        now = datetime.now().isoformat(timespec="seconds")
        self._db.execute(
            "INSERT INTO eval_runs (id, dataset_id, matrix_json, status, results_json, created_at) "
            "VALUES (?, ?, ?, 'running', '[]', ?)",
            (run_id, dataset_id, json.dumps(combos, ensure_ascii=False), now),
        )
        asyncio.create_task(self._execute_run(run_id, dataset, combos))
        return self.get_run(run_id)  # type: ignore[return-value]

    async def _execute_run(self, run_id: str, dataset: dict[str, Any], combos: list[dict[str, Any]]) -> None:
        results: list[dict[str, Any]] = []
        try:
            for combo in combos:
                provider_id = str(combo.get("provider_id") or "deepseek")
                model = str(combo.get("model") or "")
                system_prompt = str(combo.get("system_prompt") or "")
                entry: dict[str, Any] = {
                    "provider_id": provider_id,
                    "model": model,
                    "system_prompt": system_prompt,
                    "status": "running",
                }
                results.append(entry)
                self._write_results(run_id, results)
                try:
                    report = await EvalRunner(
                        provider_factory=self._make_provider_factory(provider_id),
                        default_model=model or "eval-model",
                        extra_tools=self._collect_tools(),
                        system_prompt=system_prompt,
                    ).run(load_cases(dataset["cases"]))
                    entry.update(
                        {
                            "status": "done",
                            "pass_rate": report.pass_rate,
                            "total": report.total,
                            "passed": report.passed_count,
                            "failed": report.failed_count,
                            "duration_s": round(report.duration_s, 2),
                            "avg_latency_ms": (report.total_latency_ms // report.total if report.total else 0),
                            "tool_usage": report.tool_usage,
                            "cases": [r.to_dict() for r in report.results],
                        }
                    )
                except Exception as e:  # noqa: BLE001
                    logger.warning("评测组合执行失败: %s", e)
                    entry["status"] = "error"
                    entry["error"] = str(e)
                self._write_results(run_id, results)
            self._db.execute(
                "UPDATE eval_runs SET status = 'done', finished_at = ? WHERE id = ?",
                (datetime.now().isoformat(timespec="seconds"), run_id),
            )
        except Exception as e:  # noqa: BLE001
            logger.error("评测运行异常: %s", e)
            self._db.execute(
                "UPDATE eval_runs SET status = 'error', error = ?, finished_at = ? WHERE id = ?",
                (str(e), datetime.now().isoformat(timespec="seconds"), run_id),
            )

    def _make_provider_factory(self, provider_id: str) -> Any:
        """构造 `(case) -> provider` 工厂（EvalRunner 契约）。"""

        def factory(_case: Any) -> Any:
            if self._provider_factory is not None:
                return self._provider_factory(provider_id)
            # 延迟取主服务的注册表（单例，与主应用共享 DB 与配置）
            from harness.main import _services  # noqa: PLC0415
            from harness.modules.model_manager.provider_registry import ProviderRegistry

            pr = _services.get(ProviderRegistry)
            return pr.get_provider(provider_id, include_disabled=True)

        return factory

    def _collect_tools(self) -> list[Any]:
        """把主工具注册表的全部工具实例注入评测（保持与真实对话一致的能力）。"""
        if self._tool_registry is None:
            return []
        out = []
        for info in self._tool_registry.list_tools():
            try:
                out.append(self._tool_registry.get(info.get("name", "")))
            except Exception:  # noqa: BLE001
                continue
        return out

    def _write_results(self, run_id: str, results: list[dict[str, Any]]) -> None:
        self._db.execute(
            "UPDATE eval_runs SET results_json = ? WHERE id = ?",
            (json.dumps(results, ensure_ascii=False), run_id),
        )

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        row = self._db.query_one(
            "SELECT id, dataset_id, matrix_json, status, results_json, error, created_at, finished_at "
            "FROM eval_runs WHERE id = ?",
            (run_id,),
        )
        if row is None:
            return None
        d = dict(row)
        d["matrix"] = json.loads(d.pop("matrix_json") or "[]")
        d["results"] = json.loads(d.pop("results_json") or "[]")
        return d

    def list_runs(self, dataset_id: str | None = None) -> list[dict[str, Any]]:
        if dataset_id:
            rows = self._db.query(
                "SELECT id, dataset_id, matrix_json, status, results_json, error, created_at, finished_at "
                "FROM eval_runs WHERE dataset_id = ? ORDER BY created_at DESC",
                (dataset_id,),
            )
        else:
            rows = self._db.query(
                "SELECT id, dataset_id, matrix_json, status, results_json, error, created_at, finished_at "
                "FROM eval_runs ORDER BY created_at DESC"
            )
        out = []
        for r in rows:
            d = dict(r)
            d["matrix"] = json.loads(d.pop("matrix_json") or "[]")
            results = json.loads(d.pop("results_json") or "[]")
            # 列表页只带摘要（完整 case 明细在 get_run 里）
            d["summary"] = [
                {
                    "provider_id": x.get("provider_id"),
                    "model": x.get("model"),
                    "status": x.get("status"),
                    "pass_rate": x.get("pass_rate"),
                    "total": x.get("total"),
                    "passed": x.get("passed"),
                }
                for x in results
            ]
            out.append(d)
        return out
