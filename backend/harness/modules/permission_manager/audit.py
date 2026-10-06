"""权限审计日志落库。

与 ``decide()`` 解耦：审计**只**在「真实执行路径」(agent_loop 工具调用前) 与
「人工确认收口」处落库，**绝不在 ``decide()`` 内部**——因为 ``list_tool_risks()``
会为每个工具各调一次 ``decide()`` 仅用于前端渲染，若写进 ``decide()`` 会把这些
"展示用"假决策灌进审计表。

写入走既有 ``Database`` 单例（SQLite 单连接 + RLock + WAL），单次 INSERT 足够轻。
任何写入异常都被吞掉并记 warning：审计失败绝不应阻断真实工具执行。
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime

from harness.infra.database import DEFAULT_DB_PATH, Database

logger = logging.getLogger("harness.permissions.audit")

# 按库路径缓存连接。旧实现每次调用都 `Database()`：新建 sqlite 连接并把
# init_schema（20+ 张 CREATE TABLE + PRAGMA + 多次 commit）完整跑一遍，
# 而该函数位于**每次工具调用**的热路径上，且与主连接争抢写锁。
_DB_CACHE: dict[str, Database] = {}


def _db() -> Database:
    """取当前库路径对应的 Database 实例（按路径缓存，测试换库时自动切换）。"""
    path = os.environ.get("HARNESS_DB_PATH") or DEFAULT_DB_PATH
    db = _DB_CACHE.get(path)
    if db is None:
        db = Database()
        _DB_CACHE[path] = db
    return db


def record_audit_event(
    *,
    tool_name: str,
    risk: str,
    action: str,
    stage: str,
    outcome: str | None = None,
    reason: str = "",
    policy_mode: str | None = None,
    decided_by: str = "policy",
    user_id: str | None = None,
    session_id: str | None = None,
    agent_run_id: str | None = None,
    trace_id: str | None = None,
) -> None:
    """向 ``permission_audit`` 追加一条审计记录。

    Args:
        stage: ``decision``（策略判定）/ ``resolved``（人类最终裁决）。
        outcome: decision 阶段等于 action；resolved 阶段为 ``executed`` / ``rejected``。
            传 ``None`` 时自动回落为 ``action``。
        decided_by: ``policy`` / ``override`` / ``human``。
    """
    try:
        _db().execute(
            """INSERT INTO permission_audit
               (created_at, user_id, session_id, agent_run_id, tool_name, risk,
                action, stage, outcome, reason, policy_mode, decided_by, trace_id)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                datetime.now(UTC).isoformat(),
                user_id,
                session_id,
                agent_run_id,
                tool_name,
                risk,
                action,
                stage,
                outcome if outcome is not None else action,
                reason,
                policy_mode,
                decided_by,
                trace_id,
            ),
        )
    except Exception as e:  # 防御性：审计写入失败不得影响主流程
        logger.warning("审计日志写入失败（已跳过）: %s", e)
