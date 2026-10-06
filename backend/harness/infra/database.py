"""数据库初始化与连接管理。

SQLite + SQLModel/SQLAlchemy。
零依赖起步，保留切换 Postgres 的能力（Repository 抽象层）。
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
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
    archived INTEGER NOT NULL DEFAULT 0,
    user_id TEXT NOT NULL DEFAULT ''
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
    reasoning TEXT NOT NULL DEFAULT '',
    parent_id TEXT,
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
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    user_id TEXT NOT NULL DEFAULT ''
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

# 评测实验室（方案三/P1）：数据集与对比运行
_CREATE_EVAL_DATASETS = """
CREATE TABLE IF NOT EXISTS eval_datasets (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    cases_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

_CREATE_EVAL_RUNS = """
CREATE TABLE IF NOT EXISTS eval_runs (
    id TEXT PRIMARY KEY,
    dataset_id TEXT NOT NULL,
    matrix_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'pending',
    results_json TEXT NOT NULL DEFAULT '[]',
    error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at TEXT
);
"""

# 模板分享中心（方案五/P3）
_CREATE_TEMPLATES = """
CREATE TABLE IF NOT EXISTS templates (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    vars_schema_json TEXT NOT NULL DEFAULT '{}',
    payload_json TEXT NOT NULL DEFAULT '{}',
    source_session_id TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

# 工作流编排（方案二/P4）
_CREATE_WORKFLOWS = """
CREATE TABLE IF NOT EXISTS workflows (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    vars_json TEXT NOT NULL DEFAULT '[]',
    steps_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

_CREATE_WORKFLOW_RUNS = """
CREATE TABLE IF NOT EXISTS workflow_runs (
    id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL,
    vars_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'pending',
    steps_json TEXT NOT NULL DEFAULT '[]',
    error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at TEXT
);
"""

# 运行检查点（断点续跑）：记录每次 AgentLoop 运行的状态与迭代数，
# 服务重启 / 崩溃后可据此从会话已持久化的上下文续跑。
_CREATE_AGENT_RUNS = """
CREATE TABLE IF NOT EXISTS agent_runs (
    run_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'running',
    iteration INTEGER NOT NULL DEFAULT 0,
    snapshot_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

# 渠道会话映射（E1：多渠道 Channel）：每个 (channel, user) 对应一个会话，
# 使外部渠道的同一用户在多次 / 重启后仍续接同一对话。
_CREATE_CHANNEL_LINKS = """
CREATE TABLE IF NOT EXISTS channel_links (
    channel TEXT NOT NULL,
    external_user TEXT NOT NULL,
    session_id TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (channel, external_user)
);
"""

# 用户账户（E12：认证与多用户）：启用认证后按用户隔离会话与记忆。
_CREATE_USERS = """
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    salt TEXT NOT NULL,
    is_admin INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

# 权限审计日志（EPIC 首期切片：全链路审计，谁/何时/调了什么/如何裁决/最终是否执行）。
# 零组织模型依赖，沿用本项目「CREATE TABLE IF NOT EXISTS + 朴素列类型」约定，旧库自动升级。
_CREATE_PERMISSION_AUDIT = """
CREATE TABLE IF NOT EXISTS permission_audit (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at    TEXT    NOT NULL,   -- UTC ISO 带偏移
    user_id       TEXT,              -- 关联 users.id；未登录/系统调用记 'system'
    session_id    TEXT,              -- 关联 sessions.id
    agent_run_id  TEXT,              -- 关联 agent_runs.id（可选）
    tool_name     TEXT    NOT NULL,
    risk          TEXT    NOT NULL,  -- read / write / dangerous
    action        TEXT    NOT NULL,  -- 策略裁决：allow / confirm / deny
    stage         TEXT    NOT NULL,  -- decision（策略判定）/ resolved（人类最终裁决）
    outcome       TEXT,              -- decision 阶段=action；resolved 阶段=executed/rejected
    reason        TEXT,              -- 策略原因原文
    policy_mode   TEXT,              -- 决策时刻的全局策略快照
    decided_by    TEXT,              -- policy / override / human
    trace_id      TEXT               -- 关联 spans.trace_id
);
"""

_CREATE_INDEX_AUDIT = """
CREATE INDEX IF NOT EXISTS idx_audit_created   ON permission_audit(created_at);
CREATE INDEX IF NOT EXISTS idx_audit_user      ON permission_audit(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_audit_session   ON permission_audit(session_id);
CREATE INDEX IF NOT EXISTS idx_audit_tool      ON permission_audit(tool_name, created_at);
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
        # 单共享连接上的并发保护：WAL 允许读写分离，但同一连接仍不可被多线程同时访问。
        # 用可重入锁包裹所有数据库操作，避免多会话 / 后台任务并写时的连接损坏与交错。
        self._lock = threading.RLock()

        # 确保数据目录存在
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)

    @property
    def conn(self) -> sqlite3.Connection:
        """获取数据库连接（惰性创建，双重检查锁保证只建一次）。"""
        if self._conn is None:
            with self._lock:
                if self._conn is None:
                    self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
                    self._conn.row_factory = sqlite3.Row
                    self._conn.execute("PRAGMA foreign_keys = ON")
                    # WAL：读写分离，缓解并发写时的「database is locked」；
                    # busy_timeout：写冲突时等待而非立即报错。
                    self._conn.execute("PRAGMA journal_mode = WAL")
                    self._conn.execute("PRAGMA busy_timeout = 5000")
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
            + _CREATE_AGENT_RUNS
            + _CREATE_CHANNEL_LINKS
            + _CREATE_USERS
            + _CREATE_PERMISSION_AUDIT
            + _CREATE_EVAL_DATASETS
            + _CREATE_EVAL_RUNS
            + _CREATE_TEMPLATES
            + _CREATE_WORKFLOWS
            + _CREATE_WORKFLOW_RUNS
            + _CREATE_INDEX_AUDIT
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
            + "CREATE INDEX IF NOT EXISTS idx_agent_runs_session ON agent_runs(session_id, updated_at);"
        )
        self._conn.commit()
        # 旧库升级：messages 表可能缺少 attachments / reasoning 列
        cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(messages)")}
        if "attachments" not in cols:
            self._conn.execute("ALTER TABLE messages ADD COLUMN attachments TEXT NOT NULL DEFAULT '[]'")
        if "reasoning" not in cols:
            self._conn.execute("ALTER TABLE messages ADD COLUMN reasoning TEXT NOT NULL DEFAULT ''")
        # 回答版本：每条 assistant 答案的 parent_id 指向它所回答的 user 消息，
        # 同一 user 消息下的多条 assistant 即为「同一提问的多次重新回答」版本。
        if "parent_id" not in cols:
            self._conn.execute("ALTER TABLE messages ADD COLUMN parent_id TEXT")
        # 工作流画布图（方案二/P4 升级为图编排）：存储 nodes/edges 的 JSON
        wf_cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(workflows)")}
        if "graph_json" not in wf_cols:
            self._conn.execute("ALTER TABLE workflows ADD COLUMN graph_json TEXT NOT NULL DEFAULT '{}'")
        # E12：旧库补用户归属列（认证启用后按用户隔离）
        sess_cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(sessions)")}
        if "user_id" not in sess_cols:
            self._conn.execute("ALTER TABLE sessions ADD COLUMN user_id TEXT NOT NULL DEFAULT ''")
        mem_cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(memories)")}
        if "user_id" not in mem_cols:
            self._conn.execute("ALTER TABLE memories ADD COLUMN user_id TEXT NOT NULL DEFAULT ''")
        self._conn.commit()
        logger.info("数据库 schema 已初始化: %s", self.db_path)

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        """执行 SQL（非查询）。"""
        with self._lock:
            cursor = self.conn.execute(sql, params)
            self.conn.commit()
            return cursor

    def query(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        """查询并返回所有行。"""
        with self._lock:
            cursor = self.conn.execute(sql, params)
            return cursor.fetchall()

    def query_one(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
        """查询并返回第一行。"""
        with self._lock:
            cursor = self.conn.execute(sql, params)
            result = cursor.fetchone()
            return result if result is not None else None

    def close(self) -> None:
        """关闭连接。"""
        with self._lock:
            if self._conn:
                self._conn.close()
                self._conn = None
