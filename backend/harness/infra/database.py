"""数据库初始化与连接管理。

SQLite + SQLModel/SQLAlchemy。
零依赖起步，保留切换 Postgres 的能力（Repository 抽象层）。
"""

from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path
from typing import Any

logger = logging.getLogger("harness.db")

# 默认数据库路径（项目本地）
DEFAULT_DB_PATH = str(Path(__file__).parent.parent.parent / "data" / "harness.db")

# 建表 SQL
_CREATE_SESSIONS = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL DEFAULT 'New Session',
    config_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    archived INTEGER NOT NULL DEFAULT 0
);
"""

_CREATE_MESSAGES = """
CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL DEFAULT '',
    tool_calls_json TEXT,
    tokens INTEGER NOT NULL DEFAULT 0,
    latency_ms INTEGER,
    attachments TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
);
"""

_CREATE_ATTACHMENTS = """
CREATE TABLE IF NOT EXISTS attachments (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    filename TEXT NOT NULL,
    mime TEXT NOT NULL,
    size INTEGER NOT NULL DEFAULT 0,
    storage_path TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
);
"""

_CREATE_CONTEXT_SNAPSHOTS = """
CREATE TABLE IF NOT EXISTS context_snapshots (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    message_id TEXT,
    messages_json TEXT NOT NULL,
    token_count INTEGER NOT NULL DEFAULT 0,
    budget INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
);
"""

_CREATE_PROVIDERS = """
CREATE TABLE IF NOT EXISTS providers (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    base_url TEXT NOT NULL,
    api_key_encrypted TEXT,
    models_json TEXT NOT NULL DEFAULT '[]',
    extra_params_json TEXT NOT NULL DEFAULT '{}',
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

_CREATE_TODOS = """
CREATE TABLE IF NOT EXISTS todos (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    content TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    position INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
);
"""

_CREATE_ARTIFACTS = """
CREATE TABLE IF NOT EXISTS artifacts (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL DEFAULT '',
    tool_name TEXT NOT NULL DEFAULT '',
    path TEXT NOT NULL,
    size_bytes INTEGER NOT NULL DEFAULT 0,
    char_count INTEGER NOT NULL DEFAULT 0,
    summary TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

_CREATE_MEMORIES = """
CREATE TABLE IF NOT EXISTS memories (
    id TEXT PRIMARY KEY,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    scope TEXT NOT NULL DEFAULT 'global',
    session_id TEXT NOT NULL DEFAULT '',
    tags TEXT NOT NULL DEFAULT '',
    embedding TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

_CREATE_SPANS = """
CREATE TABLE IF NOT EXISTS spans (
    id TEXT PRIMARY KEY,
    trace_id TEXT NOT NULL,
    parent_id TEXT,
    session_id TEXT NOT NULL DEFAULT '',
    name TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'span',
    status TEXT NOT NULL DEFAULT 'ok',
    duration_ms INTEGER NOT NULL DEFAULT 0,
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens INTEGER NOT NULL DEFAULT 0,
    input_preview TEXT NOT NULL DEFAULT '',
    output_preview TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    meta_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

_CREATE_SCHEDULED_TASKS = """
CREATE TABLE IF NOT EXISTS scheduled_tasks (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1,
    schedule_type TEXT NOT NULL DEFAULT 'daily',
    schedule_json TEXT NOT NULL DEFAULT '{}',
    prompt TEXT NOT NULL DEFAULT '',
    mcp_servers_json TEXT NOT NULL DEFAULT '[]',
    skills_json TEXT NOT NULL DEFAULT '[]',
    tools_json TEXT NOT NULL DEFAULT '[]',
    provider_id TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    max_iterations INTEGER NOT NULL DEFAULT 8,
    timeout_seconds INTEGER NOT NULL DEFAULT 300,
    next_run_at TEXT,
    last_run_at TEXT,
    last_status TEXT NOT NULL DEFAULT '',
    last_error TEXT NOT NULL DEFAULT '',
    run_count INTEGER NOT NULL DEFAULT 0,
    fail_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

_CREATE_SCHEDULED_RUNS = """
CREATE TABLE IF NOT EXISTS scheduled_task_runs (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    started_at TEXT NOT NULL DEFAULT '',
    finished_at TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'running',
    duration_ms INTEGER NOT NULL DEFAULT 0,
    session_id TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    trigger TEXT NOT NULL DEFAULT 'schedule'
);
"""

_CREATE_INDEX_MESSAGES_SESSION = """
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, created_at);
"""

_CREATE_INDEX_SNAPSHOTS_SESSION = """
CREATE INDEX IF NOT EXISTS idx_snapshots_session ON context_snapshots(session_id, created_at);
"""


class Database:
    """SQLite 数据库连接管理器。"""

    def __init__(self, db_path: str | None = None) -> None:
        # 支持环境变量覆盖（测试隔离用），db_path 显式传入优先级最高
        self.db_path = db_path or os.environ.get("HARNESS_DB_PATH") or DEFAULT_DB_PATH
        self._conn: sqlite3.Connection | None = None

        # 确保数据目录存在
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)

    @property
    def conn(self) -> sqlite3.Connection:
        """获取数据库连接（惰性创建）。"""
        if self._conn is None:
            self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA foreign_keys = ON")
            self.init_schema()
        return self._conn

    def init_schema(self) -> None:
        """初始化数据库 schema。"""
        assert self._conn is not None
        self._conn.executescript(
            _CREATE_SESSIONS
            + _CREATE_MESSAGES
            + _CREATE_CONTEXT_SNAPSHOTS
            + _CREATE_PROVIDERS
            + _CREATE_TODOS
            + _CREATE_ARTIFACTS
            + _CREATE_MEMORIES
            + _CREATE_SPANS
            + _CREATE_SCHEDULED_TASKS
            + _CREATE_SCHEDULED_RUNS
            + _CREATE_ATTACHMENTS
            + _CREATE_INDEX_MESSAGES_SESSION
            + _CREATE_INDEX_SNAPSHOTS_SESSION
            + "CREATE INDEX IF NOT EXISTS idx_todos_session ON todos(session_id, position);"
            + "CREATE INDEX IF NOT EXISTS idx_artifacts_session ON artifacts(session_id, created_at);"
            + "CREATE INDEX IF NOT EXISTS idx_memories_scope ON memories(scope, updated_at);"
            + "CREATE INDEX IF NOT EXISTS idx_spans_trace ON spans(trace_id, id);"
            + "CREATE INDEX IF NOT EXISTS idx_spans_session ON spans(session_id, created_at);"
            + "CREATE INDEX IF NOT EXISTS idx_sched_tasks_next ON scheduled_tasks(enabled, next_run_at);"
            + "CREATE INDEX IF NOT EXISTS idx_sched_runs_task ON scheduled_task_runs(task_id, started_at);"
            + "CREATE INDEX IF NOT EXISTS idx_attachments_session ON attachments(session_id, created_at);"
        )
        self._conn.commit()
        # 旧库升级：messages 表可能缺少 attachments 列
        cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(messages)")}
        if "attachments" not in cols:
            self._conn.execute(
                "ALTER TABLE messages ADD COLUMN attachments TEXT NOT NULL DEFAULT '[]'"
            )
            self._conn.commit()
        logger.info("数据库 schema 已初始化: %s", self.db_path)

    def execute(
        self, sql: str, params: tuple[Any, ...] = ()
    ) -> sqlite3.Cursor:
        """执行 SQL（非查询）。"""
        cursor = self.conn.execute(sql, params)
        self.conn.commit()
        return cursor

    def query(
        self, sql: str, params: tuple[Any, ...] = ()
    ) -> list[sqlite3.Row]:
        """查询并返回所有行。"""
        cursor = self.conn.execute(sql, params)
        return cursor.fetchall()

    def query_one(
        self, sql: str, params: tuple[Any, ...] = ()
    ) -> sqlite3.Row | None:
        """查询并返回第一行。"""
        cursor = self.conn.execute(sql, params)
        result = cursor.fetchone()
        return result if result is not None else None

    def close(self) -> None:
        """关闭连接。"""
        if self._conn:
            self._conn.close()
            self._conn = None
