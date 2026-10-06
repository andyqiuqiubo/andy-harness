"""工作流编排器（Workflow Studio）—— 图编排的定义 / 运行 / 历史。

图结构（graph_json）：
    {"nodes":[{"id","type","position":{"x","y"},"data":{...}}],
     "edges":[{"id","source","target","sourceHandle","targetHandle"}]}

执行：委托 GraphEngine 做分支感知拓扑调度，每节点结果写回 workflow_runs，
前端可直接渲染节点状态与输出（Dify 风格：节点旁显示成功/失败与日志）。
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime
from typing import Any

from harness.infra.database import Database
from harness.kernel.services import ServiceRegistry
from harness.modules.workflows.engine import GraphEngine, WorkflowValidationError

logger = logging.getLogger("harness.workflows")


class WorkflowService:
    """工作流服务：CRUD + 图执行编排。"""

    def __init__(self, db: Database, services: ServiceRegistry, tool_registry: Any = None) -> None:
        self._db = db
        self._services = services
        self._tool_registry = tool_registry

    # ── CRUD ────────────────────────────────────────
    def list_workflows(self) -> list[dict[str, Any]]:
        rows = self._db.query(
            "SELECT id, name, description, graph_json, created_at FROM workflows ORDER BY created_at DESC"
        )
        return [self._row_to_wf(r) for r in rows]

    def create_workflow(self, name: str, description: str, graph: dict[str, Any]) -> dict[str, Any]:
        if not name.strip():
            raise ValueError("工作流名称不能为空")
        wf_id = uuid.uuid4().hex[:12]
        now = datetime.now().isoformat(timespec="seconds")
        self._db.execute(
            "INSERT INTO workflows (id, name, description, graph_json, created_at) VALUES (?, ?, ?, ?, ?)",
            (
                wf_id,
                name.strip(),
                description,
                json.dumps(graph or {"nodes": [], "edges": []}, ensure_ascii=False),
                now,
            ),
        )
        return self.get_workflow(wf_id)  # type: ignore[return-value]

    def get_workflow(self, workflow_id: str) -> dict[str, Any] | None:
        row = self._db.query_one(
            "SELECT id, name, description, graph_json, created_at FROM workflows WHERE id = ?",
            (workflow_id,),
        )
        if row is None:
            return None
        return self._row_to_wf(row)

    def update_workflow(self, workflow_id: str, name: str, description: str, graph: dict[str, Any]) -> dict[str, Any]:
        if self._db.query_one("SELECT id FROM workflows WHERE id = ?", (workflow_id,)) is None:
            raise KeyError(f"工作流不存在: {workflow_id}")
        self._db.execute(
            "UPDATE workflows SET name = ?, description = ?, graph_json = ? WHERE id = ?",
            (
                name.strip(),
                description,
                json.dumps(graph or {"nodes": [], "edges": []}, ensure_ascii=False),
                workflow_id,
            ),
        )
        return self.get_workflow(workflow_id)  # type: ignore[return-value]

    def delete_workflow(self, workflow_id: str) -> bool:
        cur = self._db.execute("DELETE FROM workflows WHERE id = ?", (workflow_id,))
        self._db.execute("DELETE FROM workflow_runs WHERE workflow_id = ?", (workflow_id,))
        return cur.rowcount > 0

    @staticmethod
    def _row_to_wf(row: Any) -> dict[str, Any]:
        d = dict(row)
        try:
            d["graph"] = json.loads(d.pop("graph_json") or "{}")
        except (json.JSONDecodeError, KeyError):
            d["graph"] = {"nodes": [], "edges": []}
        return d

    # ── 运行 ────────────────────────────────────────
    async def run_workflow(self, workflow_id: str, inputs: dict[str, Any]) -> dict[str, Any]:
        wf = self.get_workflow(workflow_id)
        if wf is None:
            raise KeyError(f"工作流不存在: {workflow_id}")
        graph = wf.get("graph") or {"nodes": [], "edges": []}
        run_id = uuid.uuid4().hex[:12]
        now = datetime.now().isoformat(timespec="seconds")
        self._db.execute(
            "INSERT INTO workflow_runs (id, workflow_id, vars_json, status, steps_json, created_at) "
            "VALUES (?, ?, ?, 'running', '[]', ?)",
            (run_id, workflow_id, json.dumps(inputs or {}, ensure_ascii=False), now),
        )
        engine = GraphEngine(self._services)
        try:
            result = await engine.execute(graph, inputs or {})
        except WorkflowValidationError as e:
            self._finish(run_id, "error", str(e))
            return self.get_run(run_id)  # type: ignore[return-value]
        except Exception as e:  # noqa: BLE001
            logger.error("工作流运行异常: %s", e)
            self._finish(run_id, "error", str(e))
            return self.get_run(run_id)  # type: ignore[return-value]

        steps = self._result_to_steps(result)
        # 成功分支同样必须落 finished_at：否则所有成功运行该列恒为 NULL，
        # 前端无法计算耗时、也无法区分「运行中」与「已结束」。
        self._db.execute(
            "UPDATE workflow_runs SET status = ?, steps_json = ?, finished_at = ? WHERE id = ?",
            (
                result.get("status", "error"),
                json.dumps(steps, ensure_ascii=False),
                datetime.now().isoformat(timespec="seconds"),
                run_id,
            ),
        )
        run = self.get_run(run_id)
        if run is not None:
            run["engine_result"] = result  # 携带节点级详情供前端展示
        return run  # type: ignore[return-value]

    @staticmethod
    def _result_to_steps(result: dict[str, Any]) -> list[dict[str, Any]]:
        steps: list[dict[str, Any]] = []
        results = result.get("results", {})
        for nid, r in results.items():
            steps.append(
                {
                    "node_id": nid,
                    "type": "",  # 由前端按 graph 补充
                    "status": r.get("status"),
                    "outputs": r.get("outputs", {}),
                    "error": r.get("error", ""),
                    "elapsed_ms": r.get("elapsed_ms", 0),
                    "branch": r.get("branch"),
                }
            )
        return steps

    def _finish(self, run_id: str, status: str, error: str = "") -> None:
        self._db.execute(
            "UPDATE workflow_runs SET status = ?, error = ?, finished_at = ? WHERE id = ?",
            (status, error, datetime.now().isoformat(timespec="seconds"), run_id),
        )

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        row = self._db.query_one(
            "SELECT id, workflow_id, vars_json, status, steps_json, error, created_at, finished_at "
            "FROM workflow_runs WHERE id = ?",
            (run_id,),
        )
        if row is None:
            return None
        d = dict(row)
        d["vars"] = json.loads(d.pop("vars_json") or "{}")
        d["steps"] = json.loads(d.pop("steps_json") or "[]")
        return d

    def list_runs(self, workflow_id: str | None = None) -> list[dict[str, Any]]:
        if workflow_id:
            rows = self._db.query(
                "SELECT id, workflow_id, vars_json, status, steps_json, error, created_at, finished_at "
                "FROM workflow_runs WHERE workflow_id = ? ORDER BY created_at DESC",
                (workflow_id,),
            )
        else:
            rows = self._db.query(
                "SELECT id, workflow_id, vars_json, status, steps_json, error, created_at, finished_at "
                "FROM workflow_runs ORDER BY created_at DESC"
            )
        out = []
        for r in rows:
            d = dict(r)
            d["vars"] = json.loads(d.pop("vars_json") or "{}")
            d["steps"] = json.loads(d.pop("steps_json") or "[]")
            out.append(d)
        return out
