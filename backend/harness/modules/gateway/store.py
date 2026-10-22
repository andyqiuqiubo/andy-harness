"""gateway 数据访问 —— gw_* 表、用量批量写、审计、配置版本。"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from typing import Any, Protocol

logger = logging.getLogger("harness.gateway.store")

SCHEMA_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS gw_tenants (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'active',
        allowed_models_json TEXT NOT NULL DEFAULT '["*"]',
        allowed_channels_json TEXT NOT NULL DEFAULT '["*"]',
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS gw_keys (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL,
        name TEXT NOT NULL DEFAULT '',
        prefix TEXT NOT NULL,
        key_hash TEXT NOT NULL,
        scopes_json TEXT NOT NULL DEFAULT '["chat","models"]',
        rpm INTEGER NOT NULL DEFAULT 0,
        tpm INTEGER NOT NULL DEFAULT 0,
        expires_at TEXT,
        revoked_at TEXT,
        last_used_at TEXT,
        created_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_gw_keys_prefix ON gw_keys(prefix)",
    """
    CREATE TABLE IF NOT EXISTS gw_usage (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        tenant_id TEXT NOT NULL DEFAULT '',
        key_id TEXT NOT NULL DEFAULT '',
        channel_id TEXT NOT NULL DEFAULT '',
        credential_id TEXT NOT NULL DEFAULT '',
        model TEXT NOT NULL DEFAULT '',
        status INTEGER NOT NULL DEFAULT 0,
        prompt_tokens INTEGER NOT NULL DEFAULT 0,
        completion_tokens INTEGER NOT NULL DEFAULT 0,
        total_tokens INTEGER NOT NULL DEFAULT 0,
        duration_ms INTEGER NOT NULL DEFAULT 0,
        ttfb_ms INTEGER NOT NULL DEFAULT 0,
        attempt INTEGER NOT NULL DEFAULT 1,
        trace_id TEXT NOT NULL DEFAULT '',
        error_code TEXT NOT NULL DEFAULT ''
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_gw_usage_ts ON gw_usage(ts)",
    "CREATE INDEX IF NOT EXISTS idx_gw_usage_tenant_ts ON gw_usage(tenant_id, ts)",
    "CREATE INDEX IF NOT EXISTS idx_gw_usage_trace ON gw_usage(trace_id)",
    """
    CREATE TABLE IF NOT EXISTS gw_audit (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        actor TEXT NOT NULL DEFAULT '',
        action TEXT NOT NULL DEFAULT '',
        target_type TEXT NOT NULL DEFAULT '',
        target_id TEXT NOT NULL DEFAULT '',
        detail_json TEXT NOT NULL DEFAULT '{}',
        trace_id TEXT NOT NULL DEFAULT ''
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_gw_audit_ts ON gw_audit(ts)",
    """
    CREATE TABLE IF NOT EXISTS gw_config_versions (
        version INTEGER PRIMARY KEY,
        payload_json TEXT NOT NULL,
        checksum TEXT NOT NULL,
        actor TEXT NOT NULL DEFAULT '',
        source TEXT NOT NULL DEFAULT 'api',
        applied_at TEXT NOT NULL
    )
    """,
]


class SqlDB(Protocol):
    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> Any: ...
    def query(self, sql: str, params: tuple[Any, ...] = ()) -> list[Any]: ...
    def query_one(self, sql: str, params: tuple[Any, ...] = ()) -> Any | None: ...


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class GatewayStore:
    def __init__(self, db: SqlDB) -> None:
        self.db = db

    def ensure_schema(self) -> None:
        for sql in SCHEMA_STATEMENTS:
            self.db.execute(sql)

    # ---- 用量 ---------------------------------------------------------- #
    def record_usage(self, row: dict[str, Any]) -> None:
        self.db.execute(
            "INSERT INTO gw_usage (ts, tenant_id, key_id, channel_id, credential_id, model, status, "
            " prompt_tokens, completion_tokens, total_tokens, duration_ms, ttfb_ms, attempt, trace_id, error_code) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row.get("ts") or now_iso(),
                row.get("tenant_id", ""),
                row.get("key_id", ""),
                row.get("channel_id", ""),
                row.get("credential_id", ""),
                row.get("model", ""),
                int(row.get("status", 0)),
                int(row.get("prompt_tokens", 0)),
                int(row.get("completion_tokens", 0)),
                int(row.get("total_tokens", 0)),
                int(row.get("duration_ms", 0)),
                int(row.get("ttfb_ms", 0)),
                int(row.get("attempt", 1)),
                row.get("trace_id", ""),
                row.get("error_code", ""),
            ),
        )

    def query_usage(self, tenant_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 1000))
        if tenant_id:
            rows = self.db.query(
                "SELECT * FROM gw_usage WHERE tenant_id = ? ORDER BY id DESC LIMIT ?", (tenant_id, limit)
            )
        else:
            rows = self.db.query("SELECT * FROM gw_usage ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]

    def prune_usage(self, retention_days: int) -> int:
        """清理超过保留期的用量流水，返回删除行数。"""
        if retention_days <= 0:
            return 0
        cursor = self.db.execute(
            "DELETE FROM gw_usage WHERE ts < datetime('now', ?)",
            (f"-{int(retention_days)} days",),
        )
        return int(getattr(cursor, "rowcount", 0) or 0)

    # ---- 审计 ---------------------------------------------------------- #
    def audit(
        self,
        action: str,
        *,
        actor: str = "",
        target_type: str = "",
        target_id: str = "",
        detail: dict[str, Any] | None = None,
        trace_id: str = "",
    ) -> None:
        try:
            self.db.execute(
                "INSERT INTO gw_audit (ts, actor, action, target_type, target_id, detail_json, trace_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    now_iso(),
                    actor,
                    action,
                    target_type,
                    target_id,
                    json.dumps(detail or {}, ensure_ascii=False),
                    trace_id,
                ),
            )
        except Exception as e:  # noqa: BLE001 —— 审计失败不能阻断请求
            logger.warning("gateway 审计写入失败: %s", e)

    def query_audit(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.db.query("SELECT * FROM gw_audit ORDER BY id DESC LIMIT ?", (max(1, min(limit, 1000)),))
        return [dict(r) for r in rows]

    # ---- 配置版本 ------------------------------------------------------ #
    def save_config_version(
        self,
        *,
        version: int,
        payload: dict[str, Any],
        checksum: str,
        actor: str,
        source: str,
    ) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO gw_config_versions (version, payload_json, checksum, actor, source, applied_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (version, json.dumps(payload, ensure_ascii=False), checksum, actor, source, now_iso()),
        )

    def list_config_versions(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT version, checksum, actor, source, applied_at FROM gw_config_versions ORDER BY version DESC LIMIT ?",
            (max(1, min(limit, 100)),),
        )
        return [dict(r) for r in rows]

    def has_config_checksum(self, checksum: str) -> bool:
        row = self.db.query_one("SELECT 1 AS hit FROM gw_config_versions WHERE checksum = ? LIMIT 1", (checksum,))
        return row is not None

    def get_config_version(self, version: int) -> dict[str, Any] | None:
        row = self.db.query_one("SELECT payload_json FROM gw_config_versions WHERE version = ?", (version,))
        if row is None:
            return None
        try:
            value = json.loads(row["payload_json"])
        except (json.JSONDecodeError, TypeError):
            return None
        return value if isinstance(value, dict) else None

    def latest_config_version(self) -> int:
        row = self.db.query_one("SELECT MAX(version) AS v FROM gw_config_versions")
        try:
            return int(row["v"] or 0) if row is not None else 0
        except (TypeError, ValueError, KeyError):
            return 0


class UsageWriter:
    """用量批量落库：50 条或 500ms 触发，避免单连接 SQLite 写竞争。"""

    def __init__(self, store: GatewayStore, *, batch_size: int = 50, interval: float = 0.5) -> None:
        self.store = store
        self.batch_size = batch_size
        self.interval = interval
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=10000)
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    def start(self) -> None:
        if self._task is None:
            self._stop.clear()
            self._task = asyncio.create_task(self._loop(), name="gateway-usage-writer")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except TimeoutError:
                self._task.cancel()
            self._task = None
        self._drain()

    def enqueue(self, row: dict[str, Any]) -> None:
        try:
            self._queue.put_nowait(row)
        except asyncio.QueueFull:
            logger.warning("gateway 用量队列已满，丢弃一条记录")

    async def _loop(self) -> None:
        batch: list[dict[str, Any]] = []
        while not self._stop.is_set():
            try:
                item = await asyncio.wait_for(self._queue.get(), timeout=self.interval)
                batch.append(item)
                while len(batch) < self.batch_size:
                    try:
                        batch.append(self._queue.get_nowait())
                    except asyncio.QueueEmpty:
                        break
            except TimeoutError:
                pass
            if batch:
                self._flush(batch)
                batch = []
        if batch:
            self._flush(batch)

    def _flush(self, batch: list[dict[str, Any]]) -> None:
        for row in batch:
            try:
                self.store.record_usage(row)
            except Exception as e:  # noqa: BLE001
                logger.warning("gateway 用量写入失败: %s", e)

    def _drain(self) -> None:
        while True:
            try:
                row = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            try:
                self.store.record_usage(row)
            except Exception as e:  # noqa: BLE001
                logger.warning("gateway 用量落盘失败: %s", e)
