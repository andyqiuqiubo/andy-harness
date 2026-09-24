"""数据库初始化与连接管理。

SQLite + SQLModel/SQLAlchemy。
零依赖起步，保留切换 Postgres 的能力（Repository 抽象层）。
"""

from __future__ import annotations

import logging
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

_CREATE_INDEX_MESSAGES_SESSION = """
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, created_at);
"""

_CREATE_INDEX_SNAPSHOTS_SESSION = """
CREATE INDEX IF NOT EXISTS idx_snapshots_session ON context_snapshots(session_id, created_at);
"""


class Database:
    """SQLite 数据库连接管理器。"""

    def __init__(self, db_path: str = DEFAULT_DB_PATH) -> None:
        self.db_path = db_path
        self._conn: sqlite3.Connection | None = None

        # 确保数据目录存在
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)

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
            + _CREATE_INDEX_MESSAGES_SESSION
            + _CREATE_INDEX_SNAPSHOTS_SESSION
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
