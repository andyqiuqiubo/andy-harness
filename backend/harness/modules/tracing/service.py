"""运行轨迹服务 —— span 落库、按 trace 聚合、供前端回放。

一次 AgentLoop.run() 产生一个 trace（trace_id = run 的 id），其中包含：
- 1 个 run span（整轮耗时 / 迭代数 / 总 token）
- 若干 model span（每次模型调用，含 prompt/completion/total token 与耗时）
- 若干 tool span（每次工具调用，含耗时、输入输出摘要、成功/失败）

引擎通过事件总线（topic `trace.span`）发布 span；本服务订阅并落库。
"""

from __future__ import annotations

import json
import os
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from harness.infra.database import Database

# 预览文本落库上限（避免 span 表膨胀）
_PREVIEW_LIMIT = 500

# 默认保留上限（行数）：超过则清理最旧的 span，避免表无限增长（0 = 不限制）
DEFAULT_SPANS_MAX = 20000


def _now_iso() -> str:
    """UTC 时间戳（带显式偏移），前端据此转本地时间展示。"""
    return datetime.now(UTC).isoformat(timespec="seconds")


def spans_max() -> int:
    """span 保留条数上限（0 = 不限制）。"""
    raw = os.environ.get("HARNESS_SPANS_MAX")
    if raw:
        try:
            return max(0, int(raw))
        except ValueError:
            pass
    return DEFAULT_SPANS_MAX


@dataclass
class Span:
    """一个运行轨迹片段。"""

    id: str
    trace_id: str
    parent_id: str | None
    session_id: str
    name: str
    kind: str
    status: str
    duration_ms: int
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    input_preview: str
    output_preview: str
    error: str
    meta: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""

    @classmethod
    def from_row(cls, row: Any) -> Span:
        try:
            meta = json.loads(row["meta_json"] or "{}")
        except (json.JSONDecodeError, TypeError):
            meta = {}
        return cls(
            id=row["id"],
            trace_id=row["trace_id"],
            parent_id=row["parent_id"],
            session_id=row["session_id"],
            name=row["name"],
            kind=row["kind"],
            status=row["status"],
            duration_ms=row["duration_ms"],
            prompt_tokens=row["prompt_tokens"],
            completion_tokens=row["completion_tokens"],
            total_tokens=row["total_tokens"],
            input_preview=row["input_preview"],
            output_preview=row["output_preview"],
            error=row["error"],
            meta=meta,
            created_at=row["created_at"],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "trace_id": self.trace_id,
            "parent_id": self.parent_id,
            "session_id": self.session_id,
            "name": self.name,
            "kind": self.kind,
            "status": self.status,
            "duration_ms": self.duration_ms,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "input_preview": self.input_preview,
            "output_preview": self.output_preview,
            "error": self.error,
            "meta": self.meta,
            "created_at": self.created_at,
        }


@dataclass
class TraceSummary:
    """一次运行（trace）的汇总。"""

    trace_id: str
    session_id: str
    name: str
    started_at: str
    duration_ms: int
    span_count: int
    total_tokens: int
    error_count: int
    # 不含整轮 run span 的步骤数（= 模型调用 + 工具调用次数），
    # 与前端泳道图展示的行数一致，避免"显示 7 步却写 8"的困惑。
    step_count: int = 0

    @property
    def status(self) -> str:
        return "error" if self.error_count > 0 else "ok"

    def to_dict(self) -> dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "session_id": self.session_id,
            "name": self.name,
            "started_at": self.started_at,
            "duration_ms": self.duration_ms,
            "span_count": self.span_count,
            "step_count": self.step_count,
            "total_tokens": self.total_tokens,
            "error_count": self.error_count,
            "status": self.status,
        }


class SpanService(ABC):
    """运行轨迹服务接口。"""

    @abstractmethod
    def record(
        self,
        *,
        trace_id: str,
        name: str,
        kind: str = "span",
        session_id: str = "",
        parent_id: str | None = None,
        status: str = "ok",
        duration_ms: int = 0,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        total_tokens: int = 0,
        input_preview: str = "",
        output_preview: str = "",
        error: str = "",
        meta: dict[str, Any] | None = None,
    ) -> Span:
        """记录一个 span。"""

    @abstractmethod
    def list_spans(self, trace_id: str) -> list[Span]:
        """列出某条 trace 的所有 span（按记录顺序）。"""

    @abstractmethod
    def list_traces(
        self,
        session_id: str | None = None,
        user_id: str | None = None,
        limit: int = 50,
    ) -> list[TraceSummary]:
        """列出 trace 汇总（最近优先）。"""

    @abstractmethod
    def get_trace(self, trace_id: str) -> dict[str, Any] | None:
        """获取一条 trace 的汇总 + 全部 span。"""

    @abstractmethod
    def delete_trace(self, trace_id: str) -> int:
        """删除一条 trace，返回删除的 span 数。"""

    @abstractmethod
    def delete_by_session(self, session_id: str) -> int:
        """删除某会话的全部轨迹，返回删除的 span 数。"""


class SpanServiceImpl(SpanService):
    """基于 SQLite 的运行轨迹实现。"""

    def __init__(self, db: Database) -> None:
        self._db = db

    def record(
        self,
        *,
        trace_id: str,
        name: str,
        kind: str = "span",
        session_id: str = "",
        parent_id: str | None = None,
        status: str = "ok",
        duration_ms: int = 0,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        total_tokens: int = 0,
        input_preview: str = "",
        output_preview: str = "",
        error: str = "",
        meta: dict[str, Any] | None = None,
    ) -> Span:
        sid = f"spn_{uuid.uuid4().hex[:12]}"
        self._db.execute(
            "INSERT INTO spans "
            "(id, trace_id, parent_id, session_id, name, kind, status, duration_ms, "
            " prompt_tokens, completion_tokens, total_tokens, input_preview, "
            " output_preview, error, meta_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                sid,
                trace_id or "unknown",
                parent_id,
                session_id or "",
                name or "span",
                kind or "span",
                status or "ok",
                int(duration_ms or 0),
                int(prompt_tokens or 0),
                int(completion_tokens or 0),
                int(total_tokens or 0),
                (input_preview or "")[:_PREVIEW_LIMIT],
                (output_preview or "")[:_PREVIEW_LIMIT],
                (error or "")[:1000],
                json.dumps(meta or {}, ensure_ascii=False, default=str),
                _now_iso(),
            ),
        )
        row = self._db.query_one("SELECT * FROM spans WHERE id = ?", (sid,))
        assert row is not None
        self._prune()
        return Span.from_row(row)

    def _prune(self) -> None:
        """按保留上限清理最旧的 span，避免表无限增长。"""
        maxn = spans_max()
        if maxn <= 0:
            return
        self._db.execute(
            "DELETE FROM spans WHERE rowid IN (SELECT rowid FROM spans ORDER BY rowid DESC LIMIT -1 OFFSET ?)",
            (maxn,),
        )

    def list_spans(self, trace_id: str) -> list[Span]:
        rows = self._db.query("SELECT * FROM spans WHERE trace_id = ? ORDER BY rowid ASC", (trace_id,))
        return [Span.from_row(r) for r in rows]

    def list_traces(
        self,
        session_id: str | None = None,
        user_id: str | None = None,
        limit: int = 50,
    ) -> list[TraceSummary]:
        params: list[Any] = []
        joins = ""
        where = ""
        if user_id:
            # E12：仅返回归属当前用户的会话下的 trace（两跳：spans.session_id → sessions.user_id）。
            joins = "JOIN sessions s ON p.session_id = s.id"
            where = "WHERE s.user_id = ?"
            params.append(user_id)
        elif session_id:
            where = "WHERE p.session_id = ?"
            params.append(session_id)
        sql = (
            "SELECT p.trace_id, "
            "  MAX(p.session_id) AS session_id, "
            "  MAX(CASE WHEN p.kind = 'run' THEN p.name END) AS name, "
            "  MIN(p.created_at) AS started_at, "
            "  MAX(p.duration_ms) AS duration_ms, "
            "  COUNT(*) AS span_count, "
            "  SUM(CASE WHEN p.kind = 'run' THEN 0 ELSE 1 END) AS step_count, "
            "  SUM(p.total_tokens) AS total_tokens, "
            "  SUM(CASE WHEN p.status = 'error' THEN 1 ELSE 0 END) AS error_count "
            f"FROM spans p {joins} {where} "
            "GROUP BY p.trace_id "
            "ORDER BY started_at DESC, p.trace_id DESC LIMIT ?"
        )
        params.append(max(1, limit))
        result: list[TraceSummary] = []
        for r in self._db.query(sql, tuple(params)):
            result.append(
                TraceSummary(
                    trace_id=r["trace_id"],
                    session_id=r["session_id"] or "",
                    name=r["name"] or "trace",
                    started_at=r["started_at"] or "",
                    duration_ms=int(r["duration_ms"] or 0),
                    span_count=int(r["span_count"] or 0),
                    total_tokens=int(r["total_tokens"] or 0),
                    error_count=int(r["error_count"] or 0),
                    step_count=int(r["step_count"] or 0),
                )
            )
        return result

    def get_trace(self, trace_id: str) -> dict[str, Any] | None:
        spans = self.list_spans(trace_id)
        if not spans:
            return None
        # 只针对该 trace 聚合（避免全表扫描）
        row = self._db.query_one(
            "SELECT MAX(session_id) AS session_id, "
            "  MAX(CASE WHEN kind = 'run' THEN name END) AS name, "
            "  MIN(created_at) AS started_at, "
            "  MAX(duration_ms) AS duration_ms, "
            "  COUNT(*) AS span_count, "
            "  SUM(CASE WHEN kind = 'run' THEN 0 ELSE 1 END) AS step_count, "
            "  SUM(total_tokens) AS total_tokens, "
            "  SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END) AS error_count "
            "FROM spans WHERE trace_id = ?",
            (trace_id,),
        )
        summary: dict[str, Any] | None = None
        if row is not None:
            summary = TraceSummary(
                trace_id=trace_id,
                session_id=row["session_id"] or "",
                name=row["name"] or "trace",
                started_at=row["started_at"] or "",
                duration_ms=int(row["duration_ms"] or 0),
                span_count=int(row["span_count"] or 0),
                total_tokens=int(row["total_tokens"] or 0),
                error_count=int(row["error_count"] or 0),
                step_count=int(row["step_count"] or 0),
            ).to_dict()
        return {
            "trace_id": trace_id,
            "summary": summary,
            "spans": [s.to_dict() for s in spans],
        }

    def delete_trace(self, trace_id: str) -> int:
        rows = self._db.query("SELECT COUNT(*) AS n FROM spans WHERE trace_id = ?", (trace_id,))
        count = int(rows[0]["n"]) if rows else 0
        if count:
            self._db.execute("DELETE FROM spans WHERE trace_id = ?", (trace_id,))
        return count

    def delete_by_session(self, session_id: str) -> int:
        cursor = self._db.execute("DELETE FROM spans WHERE session_id = ?", (session_id,))
        return int(cursor.rowcount or 0)
