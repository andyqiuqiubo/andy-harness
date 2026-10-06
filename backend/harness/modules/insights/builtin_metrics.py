"""内置指标贡献者 —— 把已有业务表接到 Insights 插件指标扩展点上。

背景：`GET /api/insights/plugins` 遍历 `list_metric_contributors()`，但生产代码
从未调用过 `register_metric_contributor`，该接口恒返回空数组，Insights 页的
插件指标区永远是空的（扩展点定义了却没接线）。

这里注册一组只读聚合贡献者，覆盖定时任务 / 评测 / 工件 / 工作流 / 记忆五类
内置能力，使扩展点真正产出数据；同时给第三方插件提供可直接参照的示例。

时间过滤说明：各表 `created_at` 格式不统一（本地 ISO 带 T、UTC 空格分隔等），
统一取前 10 位（日期部分）做字符串比较，避免时区与分隔符差异导致的漏统计。
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("harness.insights.builtin_metrics")


def _date_bounds(since: str, until: str) -> tuple[str, str]:
    """把 ISO 时间窗收敛为 `YYYY-MM-DD` 字符串边界。"""
    return (since or "")[:10], (until or "")[:10]


_DB_CACHE: Any = None


def _db() -> Any:
    """惰性取数据库实例并缓存，避免每次采集都新建连接。"""
    global _DB_CACHE
    if _DB_CACHE is None:
        from harness.infra.database import Database

        _DB_CACHE = Database()
    return _DB_CACHE


def _scalar(sql: str, params: tuple[Any, ...] = ()) -> int:
    try:
        row = _db().query_one(sql, params)
    except Exception as e:  # noqa: BLE001 —— 表不存在（插件未启用）时按 0 处理
        logger.debug("内置指标查询失败（表可能不存在）: %s", e)
        return 0
    if row is None:
        return 0
    try:
        return int(row[0] or 0)
    except (TypeError, ValueError, IndexError):
        return 0


class _BaseContributor:
    """贡献者基类：统一处理异常，单个指标失败不影响其他卡片。"""

    metric_id = ""
    metric_name = ""

    def _collect(self, since: str, until: str) -> dict[str, Any]:  # pragma: no cover - 子类实现
        raise NotImplementedError

    def collect(self, since: str, until: str) -> dict[str, Any]:
        try:
            return self._collect(since, until)
        except Exception as e:  # noqa: BLE001
            logger.warning("内置指标 %s 采集失败: %s", self.metric_id, e)
            return {}


class ScheduledTaskContributor(_BaseContributor):
    """定时任务：启用任务数 + 近期执行成功/失败数。"""

    metric_id = "scheduled_tasks"
    metric_name = "定时任务"

    def _collect(self, since: str, until: str) -> dict[str, Any]:
        lo, hi = _date_bounds(since, until)
        enabled = _scalar("SELECT COUNT(*) FROM scheduled_tasks WHERE enabled = 1")
        ok = _scalar(
            "SELECT COUNT(*) FROM scheduled_task_runs "
            "WHERE status = 'ok' AND substr(created_at, 1, 10) BETWEEN ? AND ?",
            (lo, hi),
        )
        failed = _scalar(
            "SELECT COUNT(*) FROM scheduled_task_runs "
            "WHERE status NOT IN ('ok', 'skipped', 'running') "
            "AND substr(created_at, 1, 10) BETWEEN ? AND ?",
            (lo, hi),
        )
        return {
            "value": enabled,
            "unit": "个启用任务",
            "detail": {"enabled_tasks": enabled, "runs_ok": ok, "runs_failed": failed},
        }


class EvalContributor(_BaseContributor):
    """评测实验室：数据集数 + 近期运行数。"""

    metric_id = "evals"
    metric_name = "评测实验室"

    def _collect(self, since: str, until: str) -> dict[str, Any]:
        lo, hi = _date_bounds(since, until)
        datasets = _scalar("SELECT COUNT(*) FROM eval_datasets")
        runs = _scalar(
            "SELECT COUNT(*) FROM eval_runs WHERE substr(created_at, 1, 10) BETWEEN ? AND ?",
            (lo, hi),
        )
        return {
            "value": datasets,
            "unit": "个数据集",
            "detail": {"datasets": datasets, "runs": runs},
        }


class ArtifactContributor(_BaseContributor):
    """工件库：累计工件数与占用体积。"""

    metric_id = "artifacts"
    metric_name = "工件库"

    def _collect(self, since: str, until: str) -> dict[str, Any]:
        lo, hi = _date_bounds(since, until)
        total = _scalar("SELECT COUNT(*) FROM artifacts")
        recent = _scalar(
            "SELECT COUNT(*) FROM artifacts WHERE substr(created_at, 1, 10) BETWEEN ? AND ?",
            (lo, hi),
        )
        size = _scalar("SELECT COALESCE(SUM(size_bytes), 0) FROM artifacts")
        return {
            "value": total,
            "unit": "个工件",
            "detail": {
                "total": total,
                "recent": recent,
                "size_bytes": size,
                "size_mb": round(size / 1024 / 1024, 2),
            },
        }


class WorkflowContributor(_BaseContributor):
    """工作流：工作流数量与近期运行成功/失败。"""

    metric_id = "workflows"
    metric_name = "工作流"

    def _collect(self, since: str, until: str) -> dict[str, Any]:
        lo, hi = _date_bounds(since, until)
        total = _scalar("SELECT COUNT(*) FROM workflows")
        ok = _scalar(
            "SELECT COUNT(*) FROM workflow_runs WHERE status = 'success' AND substr(created_at, 1, 10) BETWEEN ? AND ?",
            (lo, hi),
        )
        failed = _scalar(
            "SELECT COUNT(*) FROM workflow_runs WHERE status = 'error' AND substr(created_at, 1, 10) BETWEEN ? AND ?",
            (lo, hi),
        )
        return {
            "value": total,
            "unit": "条工作流",
            "detail": {"workflows": total, "runs_ok": ok, "runs_failed": failed},
        }


class MemoryContributor(_BaseContributor):
    """长期记忆：记忆条数与近期新增。"""

    metric_id = "memories"
    metric_name = "长期记忆"

    def _collect(self, since: str, until: str) -> dict[str, Any]:
        lo, hi = _date_bounds(since, until)
        total = _scalar("SELECT COUNT(*) FROM memories")
        recent = _scalar(
            "SELECT COUNT(*) FROM memories WHERE substr(created_at, 1, 10) BETWEEN ? AND ?",
            (lo, hi),
        )
        return {
            "value": total,
            "unit": "条记忆",
            "detail": {"total": total, "recent": recent},
        }


def register_builtin_contributors() -> int:
    """注册全部内置指标贡献者（幂等：重复调用不会重复注册）。

    Returns:
        本次新注册的贡献者数量。
    """
    from harness.modules.insights.service import (
        list_metric_contributors,
        register_metric_contributor,
    )

    existing = {getattr(c, "metric_id", "") for c in list_metric_contributors()}
    added = 0
    for cls in (
        ScheduledTaskContributor,
        EvalContributor,
        ArtifactContributor,
        WorkflowContributor,
        MemoryContributor,
    ):
        if cls.metric_id in existing:
            continue
        try:
            register_metric_contributor(cls())
            added += 1
        except ValueError as e:
            logger.warning("注册内置指标 %s 失败: %s", cls.metric_id, e)
    if added:
        logger.info("已注册 %d 个内置 Insights 指标贡献者", added)
    return added
