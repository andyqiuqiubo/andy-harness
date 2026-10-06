"""REST API —— 洞察看板（只读聚合）。

设计要点：
- 与 ``permission_audit.py`` 同构：管理面接口挂 ``Depends(require_admin)``，
  认证未启用（单用户）时自动放行；``Database()`` 单例由 HARNESS_DB_PATH 决定。
- 纯聚合：只读现有表（sessions/messages/spans/permission_audit），零写入。
- ``range`` 仅接受 7d / 30d / 90d，其余回退 7d。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Query

from harness.api.deps import require_admin
from harness.infra.database import Database
from harness.modules.insights.service import ALLOWED_RANGES, InsightsServiceImpl

logger = logging.getLogger("harness.api.insights")

router = APIRouter(prefix="/api/insights", tags=["insights"])


def _parse_range(range_str: str) -> int:
    """解析 '7d' / '30d' / '90d'；非法值回退 7 天。"""
    s = (range_str or "").strip().lower()
    m = s[:-1] if s.endswith("d") else s
    try:
        days = int(m)
    except ValueError:
        return 7
    return days if days in ALLOWED_RANGES else 7


@router.get("/overview", summary="洞察总览（按日聚合 + 汇总指标）")
async def get_overview(
    range: str = Query(default="7d", description="时间范围：7d / 30d / 90d"),
    _admin: dict[str, Any] | None = Depends(require_admin),
) -> dict[str, Any]:
    """最近 N 天的使用洞察：消息/会话/Token 按日序列、工具 Top10 与汇总指标。"""
    return InsightsServiceImpl(Database()).overview(_parse_range(range))


@router.get("/plugins", summary="插件贡献的自定义指标卡")
async def get_plugin_metrics(
    range: str = Query(default="30d", description="时间范围：7d / 30d / 90d"),
    _admin: dict[str, Any] | None = Depends(require_admin),
) -> list[dict[str, Any]]:
    """收集所有已注册指标贡献者（MetricContributor）的数据；未注册则返回空列表。"""
    return InsightsServiceImpl(Database()).plugin_metrics(_parse_range(range))
