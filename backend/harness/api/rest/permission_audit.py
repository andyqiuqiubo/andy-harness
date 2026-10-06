"""REST API —— 权限审计日志（只读查询）。

设计要点：
- 与 ``permissions.py`` 同构（``APIRouter`` + ``require_admin`` + ``Database`` 单例）。
- 审计查看属管理面，所有端点走 ``Depends(require_admin)``：
  认证未启用（单用户）时自动放行，启用后仅管理员可访问。
- 列表过滤用白名单列名 + 参数化占位符，杜绝 SQL 注入。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from harness.api.deps import require_admin
from harness.infra.database import Database

logger = logging.getLogger("harness.api.audit")

router = APIRouter(prefix="/api/permissions/audit", tags=["permission-audit"])

# 允许过滤的列（白名单，防止任意列名注入）
_FILTER_COLUMNS = {
    "user_id": "user_id",
    "session_id": "session_id",
    "tool_name": "tool_name",
    "risk": "risk",
    "action": "action",
    "stage": "stage",
    "outcome": "outcome",
    "decided_by": "decided_by",
}


@router.get("")
async def list_audit(
    user_id: str | None = Query(default=None),
    session_id: str | None = Query(default=None),
    tool_name: str | None = Query(default=None),
    risk: str | None = Query(default=None),
    action: str | None = Query(default=None),
    stage: str | None = Query(default=None),
    outcome: str | None = Query(default=None),
    decided_by: str | None = Query(default=None),
    start: str | None = Query(default=None, description="created_at 区间起点（ISO）"),
    end: str | None = Query(default=None, description="created_at 区间终点（ISO）"),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    _admin: dict[str, Any] | None = Depends(require_admin),
) -> dict[str, Any]:
    """分页查询审计记录，支持按主体 / 工具 / 风险 / 动作 / 阶段 / 结果过滤。"""
    filters = {
        "user_id": user_id,
        "session_id": session_id,
        "tool_name": tool_name,
        "risk": risk,
        "action": action,
        "stage": stage,
        "outcome": outcome,
        "decided_by": decided_by,
    }
    clauses: list[str] = []
    params: list[Any] = []
    for key, col in _FILTER_COLUMNS.items():
        val = filters.get(key)
        if val:
            clauses.append(f"{col} = ?")
            params.append(val)
    if start:
        clauses.append("created_at >= ?")
        params.append(start)
    if end:
        clauses.append("created_at <= ?")
        params.append(end)

    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    total_row = Database().query_one(f"SELECT COUNT(*) AS c FROM permission_audit{where}", tuple(params))
    total = int(total_row["c"]) if total_row else 0
    rows = Database().query(
        f"SELECT * FROM permission_audit{where} ORDER BY id DESC LIMIT ? OFFSET ?",
        tuple(params) + (limit, offset),
    )
    items = [dict(r) for r in rows]
    return {"total": total, "items": items, "limit": limit, "offset": offset}


@router.get("/stats")
async def audit_stats(
    _admin: dict[str, Any] | None = Depends(require_admin),
) -> dict[str, Any]:
    """聚合统计：按 action / risk / decided_by 分布、Top 工具、近 24h 时间线。"""

    def _count_by(col: str) -> dict[str, int]:
        result: dict[str, int] = {}
        for r in Database().query(f"SELECT {col} AS k, COUNT(*) AS c FROM permission_audit GROUP BY {col}"):
            result[r["k"]] = int(r["c"])
        return result

    top_rows = Database().query(
        "SELECT tool_name AS tool_name, COUNT(*) AS c FROM permission_audit GROUP BY tool_name ORDER BY c DESC LIMIT 10"
    )
    top_tools = [{"tool_name": r["tool_name"], "count": int(r["c"])} for r in top_rows]

    tl_rows = Database().query(
        "SELECT substr(created_at,1,13) AS bucket, COUNT(*) AS c FROM permission_audit "
        "WHERE created_at >= datetime('now','-24 hours') GROUP BY bucket ORDER BY bucket"
    )
    timeline_24h = [{"bucket": r["bucket"], "count": int(r["c"])} for r in tl_rows]

    return {
        "by_action": _count_by("action"),
        "by_risk": _count_by("risk"),
        "by_decided_by": _count_by("decided_by"),
        "top_tools": top_tools,
        "timeline_24h": timeline_24h,
    }


@router.get("/{audit_id}")
async def audit_detail(
    audit_id: int,
    _admin: dict[str, Any] | None = Depends(require_admin),
) -> dict[str, Any]:
    """单条审计记录详情。"""
    row = Database().query_one("SELECT * FROM permission_audit WHERE id = ?", (audit_id,))
    if row is None:
        raise HTTPException(status_code=404, detail="审计记录不存在")
    return dict(row)
