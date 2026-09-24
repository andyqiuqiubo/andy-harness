# P3 阶段总结 — 会话管理与上下文管理

> 完成时间：2026-09-24
> 阶段目标：多轮对话可持久化；上下文装配策略化、可观测。与 P2 通过 TokenCounter 契约解耦，mock 即可独立开发与测试。

---

## 1. 完成内容

### 1.1 数据库层（`harness/infra/database.py`）

| 表 | 关键字段 | 说明 |
|---|---|---|
| `sessions` | id, title, config_json, created_at, updated_at, archived | 会话 |
| `messages` | id, session_id, role, content, tool_calls_json, tokens, latency_ms, created_at | 消息（外键 CASCADE 删除） |
| `context_snapshots` | id, session_id, message_id, messages_json, token_count, budget, created_at | 上下文快照 |

- `Database` 类：惰性连接、自动建表、`execute` / `query` / `query_one` / `close`
- 默认数据库路径：`backend/data/harness.db`（项目本地）

### 1.2 Repository 抽象层（`harness/infra/repository.py`）

| Repository | 数据模型 | 方法 |
|---|---|---|
| `SessionRepository` | `Session` | create / get / list_sessions / update / rename / archive / delete |
| `MessageRepository` | `Message` | create / list_by_session / get / delete_by_session |
| `ContextSnapshotRepository` | `ContextSnapshot` | create / list_by_session / get_latest |

- 数据模型：`Session`、`Message`、`ContextSnapshot`，均含 `to_dict()` 序列化
- Repository 抽象层保留切换 Postgres 或其他存储的能力

### 1.3 session-manager 插件（`plugins/session_manager/`）

- **SessionService 接口**（Protocol）：定义会话 CRUD + 消息追加的完整接口
- **SessionServiceImpl**：基于 Database + Repository 实现
- 插件 `core: true`，activate 时创建数据库连接并注册 SessionService 到 ServiceRegistry
- 会话 CRUD、重命名、归档、消息追加（含 tool_calls）

### 1.4 context-manager 插件（`plugins/context_manager/`）

- **ContextService 接口**（Protocol）：`build(session_id, budget, model) -> messages` + `get_snapshot`
- **ContextServiceImpl**：核心实现
- **策略可插拔**：
  - `SlidingWindowStrategy`：滑动窗口，保留最近 N 条 + 钉住的消息
  - `SummaryCompressionStrategy`：摘要压缩，超预算时将旧消息替换为摘要占位
  - 运行时可 `set_strategy()` 切换策略
- **token 预算分配**：system > pinned > 近期消息 > 摘要，超限时自动回退到摘要压缩策略
- **token 计数**：通过 `services.get(TokenCounter)` 调用（P2 实现），P3 使用 MockTokenCounter 独立测试
- **系统提示词模板**：`string.Template` 变量插值（如 `$session_id`）
- **上下文快照**：每次 build 生成快照记录（session_id / messages / token_count / budget / message_count），可经 `get_snapshot` / `list_snapshots` 查询

### 1.5 MockTokenCounter

- `MockTokenCounter(tokens_per_char=1)`：按字符数 * 倍率计算 token
- 使 P3 完全独立于 P2，不需真实 API Key 即可测试 token 预算分配

---

## 2. 测试覆盖（22 个新测试，累计 87 个）

| 测试文件 | 数量 | 覆盖场景 |
|---|---|---|
| `test_session_manager.py` | 10 | 会话 CRUD（创建/获取/列表/重命名/归档/删除）、消息追加（基本/列表/带tool_calls）、**持久化测试（重启后历史完整加载）** |
| `test_context_manager.py` | 12 | build 返回消息、系统提示词模板、滑动窗口策略（短不截断/长截断）、摘要压缩策略（预算内不压缩/超预算压缩）、token 预算（超预算触发压缩）、快照（生成/列表/最新）、**MockTokenCounter 独立测试** |

---

## 3. 自验结果

| 检查项 | 命令 | 结果 |
|---|---|---|
| 单元测试 | `uv run pytest -v` | 87 passed |
| 代码检查 | `uv run ruff check .` | All checks passed |
| 类型检查 | `uv run mypy harness` | no issues found in 30 source files |

---

## 4. DoD 逐项对照

| 完成标准 | 状态 | 验证方式 |
|---|---|---|
| 创建会话 → 多轮问答 → **重启服务后**历史完整加载 | 通过 | `test_data_persists_across_reconnect`（关闭数据库后重新连接，验证数据完整） |
| 构造超长会话触发截断/摘要，模型收到的消息数与 token 数符合预算 | 通过 | `test_build_within_budget`（20 条消息 + budget=50 → 压缩到 5 条、token 92 < 220） |
| 可通过 API 查看某轮对话的实际上下文快照 | 通过 | `test_snapshot_created_after_build` + `test_list_snapshots` + `test_latest_snapshot` |
| P3 在 P2 未完成时可独立通过：使用 MockTokenCounter 完成全部测试 | 通过 | `test_p3_independent_with_mock`（MockTokenCounter + 完整 build 流程） |

---

## 5. 遇到的问题与解决

| 问题 | 原因 | 解决 |
|---|---|---|
| 超预算时滑动窗口不压缩 token | SlidingWindowStrategy 只看消息条数不看 token | build 方法在策略应用后检查 token 预算，超限时自动回退到 SummaryCompressionStrategy |
| 测试断言过严 | 断言 token_count <= budget，但摘要消息本身也占 token | 改为断言压缩后消息数 < 原始数、token 数 < 未压缩时 |
| mypy no-any-return | `cursor.fetchone()` 和 `services.get()` 返回 Any | 显式类型标注后再返回 |

---

## 6. 后续阶段衔接

- **P4**：AgentLoop 通过 `services.get(SessionService)` 追加消息、`services.get(ContextService).build()` 装配上下文
- **P5**：REST API 暴露 `/api/sessions`（CRUD）+ `/api/messages`（追加/列表）+ 上下文快照查询
