"""Insights —— 洞察看板数据服务（P0）。

纯聚合层：只对现有表（sessions / messages / spans / permission_audit）做 SQL 统计，
不建新业务表、不写回任何数据。所有 created_at 列的时间格式不统一
（messages=本地 ISO 带 T；spans=sqrt UTC 空格分隔；audit=UTC ISO 带偏移），
故按列分别构造比较用的时间下界（字符串比较在日期部分总是正确的）。

插件指标扩展点（方案四契约，供第三方插件贡献自定义指标卡）：

    from harness.modules.insights.service import register_metric_contributor

    class MyMetricContributor:
        metric_id = "feishu_docs_created"
        metric_name = "飞书文档创建数"
        def collect(self, since: str, until: str) -> dict:
            return {"value": 12, "unit": "篇"}

    register_metric_contributor(MyMetricContributor())

注册后 GET /api/insights/plugins 会自动包含其 collect() 结果；
未注册的插件零侵入（不出现卡片，也不影响其他指标）。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

logger = logging.getLogger("harness.insights")

# 支持的时间范围（天数）
ALLOWED_RANGES = (7, 30, 90)


def register_metric_contributor(contributor: Any) -> None:
    """注册一个插件指标贡献者（见模块 docstring 的契约）。"""
    if not hasattr(contributor, "collect") or not getattr(contributor, "metric_id", ""):
        raise ValueError("metric contributor 需要 metric_id 与 collect()")
    _CONTRIBUTORS.append(contributor)


def list_metric_contributors() -> list[Any]:
    return list(_CONTRIBUTORS)


def clear_metric_contributors() -> None:
    """清空注册表（测试用）。"""
    _CONTRIBUTORS.clear()


_CONTRIBUTORS: list[Any] = []


class InsightsServiceImpl:
    """洞察看板数据服务实现。"""

    def __init__(self, db: Any) -> None:
        self._db = db

    # ── 查询辅助 ────────────────────────────────────
    @staticmethod
    def _local_cutoff(days: int) -> str:
        """messages（本地 ISO 带 T）与 sessions 的比较下界。"""
        return (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")

    @staticmethod
    def _utc_cutoff(days: int) -> str:
        """spans（UTC 空格分隔）的比较下界。"""
        return (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")

    @staticmethod
    def _audit_cutoff(days: int) -> str:
        """permission_audit（UTC ISO 带偏移）的比较下界。"""
        return (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="seconds")

    @staticmethod
    def _day_keys(days: int) -> list[str]:
        """最近 N 天（含今天）的日期键列表（本地时区，升序）。"""
        today = datetime.now().date()
        return [(today - timedelta(days=i)).isoformat() for i in range(days - 1, -1, -1)]

    @staticmethod
    def _fill_days(rows: list[dict[str, Any]], day_keys: list[str], keys: list[str]) -> list[dict[str, Any]]:
        """把 SQL 按日聚合结果零填充到完整日期序列（图表需要连续横轴）。"""
        by_date = {r["date"]: r for r in rows}
        out: list[dict[str, Any]] = []
        for d in day_keys:
            # 显式标注 Any：`{"date": d}` 会被推断为 dict[str, str]，
            # 随后写入 int 统计值时类型不匹配。
            row: dict[str, Any] = {"date": d}
            for k in keys:
                row[k] = int(by_date.get(d, {}).get(k, 0) or 0)
            out.append(row)
        return out

    # ── 总览 ────────────────────────────────────────
    def overview(self, days: int = 7) -> dict[str, Any]:
        if days not in ALLOWED_RANGES:
            days = 7
        day_keys = self._day_keys(days)
        local_cut = self._local_cutoff(days)
        audit_cut = self._audit_cutoff(days)
        range_param = f"-{days} days"

        # 1. 消息量按日（只统计真实问答，排除 tool/system）
        msg_rows = [
            dict(r)
            for r in self._db.query(
                "SELECT substr(created_at, 1, 10) AS date, COUNT(*) AS count "
                "FROM messages WHERE role IN ('user','assistant') AND created_at >= ? "
                "GROUP BY date",
                (local_cut,),
            )
        ]
        messages_by_day = self._fill_days(msg_rows, day_keys, ["count"])

        # 2. 会话数按日
        sess_rows = [
            dict(r)
            for r in self._db.query(
                "SELECT substr(created_at, 1, 10) AS date, COUNT(*) AS count "
                "FROM sessions WHERE created_at >= ? GROUP BY date",
                (local_cut,),
            )
        ]
        sessions_by_day = self._fill_days(sess_rows, day_keys, ["count"])

        # 3. token 按日（spans kind=model，存储为 UTC）
        #    横轴 day_keys 是「本地日期」，故把 span 的 UTC 时间转成本地后再按日聚合，
        #    否则非 UTC 机器上 token/工具图表会与消息/会话横轴错位（甚至整列归零）。
        tok_rows = [
            dict(r)
            for r in self._db.query(
                "SELECT substr(datetime(created_at, 'localtime'), 1, 10) AS date, "
                "COALESCE(SUM(prompt_tokens),0) AS prompt, "
                "COALESCE(SUM(completion_tokens),0) AS completion, "
                "COALESCE(SUM(total_tokens),0) AS total "
                "FROM spans WHERE kind = 'model' "
                "AND datetime(created_at, 'localtime') >= datetime('now', 'localtime', ?) "
                "GROUP BY date",
                (range_param,),
            )
        ]
        tokens_by_day = self._fill_days(tok_rows, day_keys, ["prompt", "completion", "total"])

        # 4. 工具调用 Top10（spans kind=tool，UTC→本地过滤）
        top_tools = [
            dict(r)
            for r in self._db.query(
                "SELECT name, COUNT(*) AS calls, "
                "SUM(CASE WHEN status != 'ok' THEN 1 ELSE 0 END) AS errors, "
                "CAST(AVG(duration_ms) AS INTEGER) AS avg_ms "
                "FROM spans WHERE kind = 'tool' "
                "AND datetime(created_at, 'localtime') >= datetime('now', 'localtime', ?) "
                "GROUP BY name ORDER BY calls DESC LIMIT 10",
                (range_param,),
            )
        ]
        for t in top_tools:
            t["errors"] = int(t.get("errors") or 0)
            t["calls"] = int(t.get("calls") or 0)
            t["avg_ms"] = int(t.get("avg_ms") or 0)

        # 5. 汇总指标
        totals: dict[str, Any] = {}
        totals["messages"] = int(
            self._db.query_one(
                "SELECT COUNT(*) AS c FROM messages WHERE role IN ('user','assistant') AND created_at >= ?",
                (local_cut,),
            )["c"]
        )
        totals["sessions"] = int(
            self._db.query_one("SELECT COUNT(*) AS c FROM sessions WHERE created_at >= ?", (local_cut,))["c"]
        )
        tok_total = self._db.query_one(
            "SELECT COALESCE(SUM(total_tokens),0) AS t, COUNT(*) AS c, "
            "SUM(CASE WHEN status != 'ok' THEN 1 ELSE 0 END) AS err "
            "FROM spans WHERE kind = 'tool' "
            "AND datetime(created_at, 'localtime') >= datetime('now', 'localtime', ?)",
            (range_param,),
        )
        totals["tool_calls"] = int(tok_total["c"])
        totals["tool_errors"] = int(tok_total["err"] or 0)
        totals["tool_success_rate"] = (
            round(1 - totals["tool_errors"] / totals["tool_calls"], 4) if totals["tool_calls"] else None
        )
        model_tok = self._db.query_one(
            "SELECT COALESCE(SUM(total_tokens),0) AS t FROM spans "
            "WHERE kind = 'model' AND datetime(created_at, 'localtime') >= datetime('now', 'localtime', ?)",
            (range_param,),
        )
        totals["model_tokens_total"] = int(model_tok["t"])
        # tokens_total 与 model_tokens_total 取同一口径（仅 model span 携带 token），
        # 旧实现误把 tool span 的 total_tokens（恒为 0）当作总量。
        totals["tokens_total"] = int(model_tok["t"])
        totals["dangerous_ops"] = int(
            self._db.query_one(
                "SELECT COUNT(*) AS c FROM permission_audit WHERE created_at >= ? AND risk = 'dangerous'",
                (audit_cut,),
            )["c"]
        )
        totals["active_days"] = sum(1 for d in messages_by_day if d["count"] > 0)

        return {
            "range_days": days,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "totals": totals,
            "messages_by_day": messages_by_day,
            "sessions_by_day": sessions_by_day,
            "tokens_by_day": tokens_by_day,
            "top_tools": top_tools,
        }

    # ── 插件指标 ────────────────────────────────────
    def plugin_metrics(self, days: int = 30) -> list[dict[str, Any]]:
        """收集所有已注册指标贡献者的数据（单个失败不影响其他）。"""
        until = datetime.now().isoformat(timespec="seconds")
        since = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
        out: list[dict[str, Any]] = []
        for c in list_metric_contributors():
            try:
                data = c.collect(since=since, until=until)
            except Exception as e:  # noqa: BLE001
                logger.warning("指标贡献者 %s collect 失败: %s", getattr(c, "metric_id", "?"), e)
                continue
            out.append(
                {
                    "metric_id": getattr(c, "metric_id", ""),
                    "metric_name": getattr(c, "metric_name", getattr(c, "metric_id", "")),
                    "data": data or {},
                }
            )
        return out


def get_insights_service(db: Any) -> InsightsServiceImpl:
    return InsightsServiceImpl(db)
