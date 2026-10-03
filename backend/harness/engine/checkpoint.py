"""运行检查点（Durable Execution / Checkpoint 断点续跑）。

对应 GAP 分析 G4：长任务中断后可从断点续跑，服务重启不丢进度。

设计：
- ``agent_runs`` 表记录每次 AgentLoop 运行（run_id / session_id / status /
  iteration / snapshot）。AgentLoop 每轮迭代后调用 :meth:`RunCheckpointService.record`
  写入 / 更新检查点（best-effort，任何异常都不影响主流程）。
- 由于消息在每轮 assistant 与每个工具结果后**已即时持久化**到会话，因此会话历史
  本身就是完整的状态快照；续跑时只需基于已持久化的上下文让模型继续产出终答，
  无需重放内存中的中间状态。
- :func:`resume_run` 依据检查点找到会话与默认 provider，重建 AgentLoop 并继续运行，
  用于服务重启 / 崩溃后的恢复。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from harness.infra.database import Database

logger = logging.getLogger("harness.checkpoint")


@dataclass
class RunCheckpoint:
    """单次运行检查点。"""

    run_id: str
    session_id: str
    status: str
    iteration: int
    snapshot: dict[str, Any]
    created_at: str = ""
    updated_at: str = ""


class RunCheckpointService:
    """检查点读写服务（直接操作 ``agent_runs`` 表）。"""

    def __init__(self, db: Database) -> None:
        self._db = db

    def record(
        self,
        run_id: str,
        session_id: str,
        status: str,
        iteration: int,
        snapshot: dict[str, Any] | None = None,
    ) -> None:
        """写入或更新一个检查点（upsert）。"""
        snapshot_json = json.dumps(snapshot or {}, ensure_ascii=False)
        existing = self.get(run_id)
        if existing is None:
            self._db.execute(
                "INSERT INTO agent_runs (run_id, session_id, status, iteration, snapshot_json) VALUES (?, ?, ?, ?, ?)",
                (run_id, session_id, status, iteration, snapshot_json),
            )
        else:
            self._db.execute(
                "UPDATE agent_runs SET status = ?, iteration = ?, "
                "snapshot_json = ?, updated_at = datetime('now') WHERE run_id = ?",
                (status, iteration, snapshot_json, run_id),
            )

    def update_status(self, run_id: str, status: str) -> None:
        """仅更新状态（如标记为 resumed / done）。"""
        self._db.execute(
            "UPDATE agent_runs SET status = ?, updated_at = datetime('now') WHERE run_id = ?",
            (status, run_id),
        )

    def get(self, run_id: str) -> RunCheckpoint | None:
        """按 run_id 读取检查点。"""
        row = self._db.query_one("SELECT * FROM agent_runs WHERE run_id = ?", (run_id,))
        if row is None:
            return None
        return self._row_to_checkpoint(row)

    def list_by_session(self, session_id: str) -> list[RunCheckpoint]:
        """列出某会话的全部检查点（按更新时间升序）。"""
        rows = self._db.query(
            "SELECT * FROM agent_runs WHERE session_id = ? ORDER BY updated_at ASC",
            (session_id,),
        )
        return [self._row_to_checkpoint(r) for r in rows]

    @staticmethod
    def _row_to_checkpoint(row: Any) -> RunCheckpoint:
        try:
            snapshot = json.loads(row["snapshot_json"] or "{}")
        except (ValueError, TypeError):
            snapshot = {}
        return RunCheckpoint(
            run_id=row["run_id"],
            session_id=row["session_id"],
            status=row["status"],
            iteration=row["iteration"],
            snapshot=snapshot,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


async def resume_run(
    run_id: str,
    services: Any,
    hooks: Any,
    tool_registry: Any,
    config: Any | None = None,
    provider_id: str | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    """从检查点续跑一次运行（服务重启 / 崩溃后的恢复）。

    Args:
        run_id: 目标检查点 ID
        services: ServiceRegistry（需含 Database 与 ProviderRegistry）
        hooks: HookManager
        tool_registry: ToolRegistry
        config: AgentLoopConfig（默认新建）
        provider_id: 指定 provider；为 None 时自动选第一个启用且有 Key 的 provider
        model: 指定模型；为 None 时取 provider 默认模型

    Returns:
        ``{"run_id", "session_id", "content", "error"}``

    说明：会话历史已在之前运行中即时持久化，续跑只是基于该上下文让模型继续
    完成剩余工作（追加上一条「继续」引导语），不重放内存态。
    """
    from harness.engine.agent_loop import AgentLoop, AgentLoopConfig
    from harness.modules.model_manager.provider_registry import ProviderRegistry

    db = services.get(Database)
    checkpoint_svc = RunCheckpointService(db)
    cp = checkpoint_svc.get(run_id)
    if cp is None:
        return {
            "run_id": run_id,
            "session_id": "",
            "content": "",
            "error": f"检查点不存在: {run_id}",
        }

    registry: ProviderRegistry = services.get(ProviderRegistry)
    provider = None
    used_model = model
    if provider_id:
        try:
            provider = registry.get_provider(provider_id)
            if used_model is None:
                cfg = registry.get_provider_config(provider_id) or {}
                models = cfg.get("models") or []
                used_model = models[0] if models else None
        except Exception as e:  # noqa: BLE001
            return {
                "run_id": run_id,
                "session_id": cp.session_id,
                "content": "",
                "error": f"无法获取 provider {provider_id}: {e}",
            }
    else:
        chosen_id: str | None = None
        for info in registry.list_providers():
            if info.get("enabled") and info.get("has_api_key"):
                chosen_id = info["id"]
                break
        if chosen_id is None:
            return {
                "run_id": run_id,
                "session_id": cp.session_id,
                "content": "",
                "error": "没有可用的已启用且已配置 Key 的 provider",
            }
        provider = registry.get_provider(chosen_id)
        if used_model is None:
            cfg = registry.get_provider_config(chosen_id) or {}
            models = cfg.get("models") or []
            used_model = models[0] if models else None

    loop = AgentLoop(
        services=services,
        hooks=hooks,
        tool_registry=tool_registry,
        config=config or AgentLoopConfig(),
    )
    continuation = "（系统）上一轮任务因中断未结束，请基于已有上下文继续执行并完成剩余工作，不要重复已经完成的步骤。"
    try:
        result = await loop.run(
            session_id=cp.session_id,
            user_message=continuation,
            provider=provider,
            model=used_model,
        )
    except Exception as e:  # noqa: BLE001
        checkpoint_svc.update_status(run_id, "error")
        return {
            "run_id": run_id,
            "session_id": cp.session_id,
            "content": "",
            "error": f"续跑失败: {e}",
        }

    checkpoint_svc.update_status(run_id, "resumed")
    return {
        "run_id": run_id,
        "session_id": cp.session_id,
        "content": result.content,
        "error": result.error,
    }
