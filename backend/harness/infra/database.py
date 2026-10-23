"""数据库初始化与连接管理。

SQLite + SQLModel/SQLAlchemy。
零依赖起步，保留切换 Postgres 的能力（Repository 抽象层）。
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("harness.db")

# 默认数据库路径（项目本地）
DEFAULT_DB_PATH = str(Path(__file__).parent.parent.parent / "data" / "harness.db")

# 应用主库单例（见 Database.__new__ 的说明）
_DB_SINGLETON_LOCK = threading.Lock()
_DB_SINGLETON: Database | None = None

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
    stopped INTEGER NOT NULL DEFAULT 0,
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

# ── 知识库系统（KB / 文档 / 分段 / 嵌入模型）──────────────────────────
# 分阶段存储：storage_stage 标记文档属 P1（文本/图文）还是 P2（音视频/特殊格式）。
# 全文检索走 SQLite FTS5 虚表（clean_fulltext 已做中文 bigram 预分词），
# 语义检索用 kb_segments.embedding（BLOB 存 float32 序列）做 numpy cosine。
_CREATE_KNOWLEDGE_BASES = """
CREATE TABLE IF NOT EXISTS knowledge_bases (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    embedding_model_id TEXT,
    index_type TEXT NOT NULL DEFAULT 'fulltext',   -- fulltext / vector / both
    default_retrieval_mode TEXT NOT NULL DEFAULT 'keyword',
    rerank_enabled INTEGER NOT NULL DEFAULT 0,
    storage_stage TEXT NOT NULL DEFAULT 'P1',
    need_reindex INTEGER NOT NULL DEFAULT 0,       -- 换模型后需重建索引
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

_CREATE_KB_DOCUMENTS = """
CREATE TABLE IF NOT EXISTS kb_documents (
    id TEXT PRIMARY KEY,
    kb_id TEXT NOT NULL,
    name TEXT NOT NULL,
    file_type TEXT NOT NULL DEFAULT '',
    origin_path TEXT NOT NULL DEFAULT '',
    storage_stage TEXT NOT NULL DEFAULT 'P1',
    status TEXT NOT NULL DEFAULT 'pending',        -- pending/processing/ready/empty/failed
    segment_count INTEGER NOT NULL DEFAULT 0,
    char_count INTEGER NOT NULL DEFAULT 0,
    asset_count INTEGER NOT NULL DEFAULT 0,
    segment_strategy TEXT NOT NULL DEFAULT 'auto', -- auto/S1/S2/S3/S4（手动覆盖）
    error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (kb_id) REFERENCES knowledge_bases(id) ON DELETE CASCADE
);
"""

_CREATE_KB_SEGMENTS = """
CREATE TABLE IF NOT EXISTS kb_segments (
    id TEXT PRIMARY KEY,
    doc_id TEXT NOT NULL,
    kb_id TEXT NOT NULL,
    content TEXT NOT NULL,                        -- 清洗后文本（子分段或普通分段）
    clean_fulltext TEXT NOT NULL DEFAULT '',      -- 参与全文索引（含图描述 / 父子时含父上下文）
    position INTEGER NOT NULL DEFAULT 0,
    parent_id TEXT,                               -- 父子分段的子分段指向父分段
    embedding BLOB,                               -- float32 序列；无向量索引时为 NULL
    metadata_json TEXT NOT NULL DEFAULT '{}',     -- 标题层级/资产引用/truncated 等
    hash TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (doc_id) REFERENCES kb_documents(id) ON DELETE CASCADE
);
"""

_CREATE_KB_EMBEDDING_MODELS = """
CREATE TABLE IF NOT EXISTS kb_embedding_models (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    provider TEXT NOT NULL DEFAULT 'openai',
    model_name TEXT NOT NULL,
    endpoint TEXT NOT NULL DEFAULT '',
    api_key_encrypted TEXT NOT NULL DEFAULT '',   -- 加密存储；返回时脱敏
    dimensions INTEGER NOT NULL DEFAULT 0,
    normalize INTEGER NOT NULL DEFAULT 1,
    batch_size INTEGER NOT NULL DEFAULT 32,
    rerank_enabled INTEGER NOT NULL DEFAULT 0,
    rerank_model TEXT NOT NULL DEFAULT '',
    rerank_endpoint TEXT NOT NULL DEFAULT '',
    rerank_api_key_encrypted TEXT NOT NULL DEFAULT '',
    rerank_top_n INTEGER NOT NULL DEFAULT 10,
    probe_ok INTEGER NOT NULL DEFAULT 0,          -- 保存时连通性探测结果
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

# FTS5 全文索引：contentless 由普通表承载（unindexed 列），
# 便于删除/重建分段时同步维护倒排索引（外部内容表）。
_CREATE_KB_SEGMENTS_FTS = """
CREATE VIRTUAL TABLE IF NOT EXISTS kb_segments_fts USING fts5(
    segment_id UNINDEXED,
    kb_id UNINDEXED,
    doc_id UNINDEXED,
    clean_fulltext,
    tokenize = 'unicode61'
);
"""

_CREATE_INDEX_KB_DOCS = """
CREATE INDEX IF NOT EXISTS idx_kb_docs_kb ON kb_documents(kb_id, created_at);
CREATE INDEX IF NOT EXISTS idx_kb_segments_doc ON kb_segments(doc_id, position);
CREATE INDEX IF NOT EXISTS idx_kb_segments_kb ON kb_segments(kb_id, position);
CREATE INDEX IF NOT EXISTS idx_kb_segments_hash ON kb_segments(kb_id, hash);
CREATE INDEX IF NOT EXISTS idx_kb_segments_parent ON kb_segments(parent_id);
"""
# 注意：FTS5 虚表（kb_segments_fts）不能再建普通索引（SQLite 报
# "virtual tables may not be indexed"）。kb_id 过滤走与 kb_segments 的 JOIN，
# 由 idx_kb_segments_kb 承担，无需（也不能）在虚表上另建索引。

_CREATE_INDEX_MESSAGES_SESSION = """
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, created_at);
"""

# 图像/视频生成任务（智谱 CogView-3-Flash / CogVideoX-Flash 等异步生成）。
# upstream_id 为上游异步任务号；status 为 SUBMITTING/PROCESSING/SUCCESS/FAIL/
# CANCELLED（CANCELLED=用户手动停止，见 media_gen.service.stop_task）；
# file_path 指向已落盘的产物（上游链接为临时链接，必须转存）。
_CREATE_MEDIA_TASKS = """
CREATE TABLE IF NOT EXISTS media_tasks (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL,                      -- image | video
    model TEXT NOT NULL,
    provider_id TEXT NOT NULL DEFAULT 'zhipu',
    prompt TEXT NOT NULL DEFAULT '',
    params_json TEXT NOT NULL DEFAULT '{}',
    upstream_id TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'PROCESSING',
    error TEXT NOT NULL DEFAULT '',
    result_url TEXT NOT NULL DEFAULT '',
    file_path TEXT NOT NULL DEFAULT '',
    mime TEXT NOT NULL DEFAULT '',
    size_bytes INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""
_CREATE_INDEX_MEDIA_TASKS = """
CREATE INDEX IF NOT EXISTS idx_media_tasks_session ON media_tasks(session_id, created_at);
"""
_CREATE_INDEX_SNAPSHOTS_SESSION = """
CREATE INDEX IF NOT EXISTS idx_snapshots_session ON context_snapshots(session_id, created_at);
"""


class Database:
    """SQLite 数据库连接管理器。

    **进程内单例**（仅限无参构造）：``Database()`` 语义是「应用主库」，全进程共享
    同一个实例；显式传入 ``db_path`` 则构造独立实例（测试用 ``:memory:`` 隔离）。

    为什么要做单例：

    1. REST 层多处直接用 ``Database()`` 取库（insights / permission_audit /
       templates_hub / workflows / evals_lab）。修复前每次调用都会新建一条 sqlite
       连接并跑一遍 :meth:`init_schema`（20 张表 + 10 个索引的 DDL），是纯粹的
       请求级浪费。
    2. 更严重的是：应用生命周期被重复触发时（测试里 ``TestClient`` 反复进出、
       未来可能的多次 lifespan），每次都会造出一个**新的** Database，而部分服务是
       "不存在才注册"，于是一次启动后不同服务会绑定不同实例，跨库写入直接触发
       ``FOREIGN KEY constraint failed``。单例让所有服务稳定指向同一个库。
    """

    def __new__(cls, db_path: str | None = None) -> Database:
        if db_path is not None:
            # 显式指定路径 → 独立实例（不进单例池）
            return super().__new__(cls)
        global _DB_SINGLETON
        if _DB_SINGLETON is not None:
            return _DB_SINGLETON
        with _DB_SINGLETON_LOCK:
            if _DB_SINGLETON is None:
                _DB_SINGLETON = super().__new__(cls)
            return _DB_SINGLETON

    def __init__(self, db_path: str | None = None) -> None:
        # 单例重复构造时不得重置连接/锁，否则正在进行的查询会被打断。
        # 用模块锁保证「检查 _initialized → 初始化 → 置位」整体原子。
        with _DB_SINGLETON_LOCK:
            if getattr(self, "_initialized", False):
                return
            # 支持环境变量覆盖（测试隔离用），db_path 显式传入优先级最高
            self.db_path = db_path or os.environ.get("HARNESS_DB_PATH") or DEFAULT_DB_PATH
            self._conn: sqlite3.Connection | None = None
            # 单共享连接上的并发保护：WAL 允许读写分离，但同一连接仍不可被多线程同时访问。
            # 用可重入锁包裹所有数据库操作，避免多会话 / 后台任务并写时的连接损坏与交错。
            self._lock = threading.RLock()

            # 确保数据目录存在
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
            self._initialized = True

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
                    self._conn.execute("PRAGMA busy_timeout = 10000")
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
            + _CREATE_KNOWLEDGE_BASES
            + _CREATE_KB_DOCUMENTS
            + _CREATE_KB_SEGMENTS
            + _CREATE_KB_EMBEDDING_MODELS
            + _CREATE_KB_SEGMENTS_FTS
            + _CREATE_INDEX_KB_DOCS
            + _CREATE_INDEX_AUDIT
            + _CREATE_INDEX_MESSAGES_SESSION
            + _CREATE_INDEX_SNAPSHOTS_SESSION
            + _CREATE_MEDIA_TASKS
            + _CREATE_INDEX_MEDIA_TASKS
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
        # 用户停止生成标记：被 stop 中断的 assistant 答案仍会持久化（保留已生成
        # 的部分内容），stopped=1 供前端在回答下方标注「用户已停止回答」。
        if "stopped" not in cols:
            self._conn.execute("ALTER TABLE messages ADD COLUMN stopped INTEGER NOT NULL DEFAULT 0")
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
        # SEC-31：定时任务补用户归属列（认证启用后按用户隔离，避免越权触发/改写他人任务）
        sched_cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(scheduled_tasks)")}
        if "user_id" not in sched_cols:
            self._conn.execute("ALTER TABLE scheduled_tasks ADD COLUMN user_id TEXT NOT NULL DEFAULT ''")
        self._conn.commit()
        logger.info("数据库 schema 已初始化: %s", self.db_path)

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        """执行 SQL（非查询）。

        `self._lock` 只保护进程内并发。多进程同时打开同一 SQLite 文件时
        （例如 uvicorn 8000 + 8020 + pytest），写事务仍会偶发
        `database is locked`。`PRAGMA busy_timeout` 只等一轮，长事务下不够，
        故在此做有限重试：失败时事务未提交，重试安全（INSERT/UPDATE 幂等）。
        """
        delay = 0.2
        last: sqlite3.OperationalError | None = None
        for attempt in range(4):
            with self._lock:
                try:
                    cursor = self.conn.execute(sql, params)
                    self.conn.commit()
                    return cursor
                except sqlite3.OperationalError as e:
                    msg = str(e).lower()
                    if "locked" not in msg and "busy" not in msg:
                        raise
                    last = e
            time.sleep(delay)
            delay *= 2
        assert last is not None
        raise last

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
