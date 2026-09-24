# P5 阶段总结 — 后端 API 层（REST + WebSocket）

> 完成时间：2026-09-24
> 阶段目标：把引擎能力完整暴露给前端。

---

## 1. 完成内容

### 1.1 REST API

| 路由 | 方法 | 功能 |
|---|---|---|
| `/api/sessions` | GET | 列出会话（支持 include_archived） |
| `/api/sessions` | POST | 创建会话 |
| `/api/sessions/{id}` | GET | 获取会话 |
| `/api/sessions/{id}` | PATCH | 更新会话（重命名/归档） |
| `/api/sessions/{id}` | DELETE | 删除会话 |
| `/api/sessions/{id}/messages` | GET | 列出会话消息 |
| `/api/sessions/{id}/messages` | POST | 追加消息 |
| `/api/providers` | GET | 列出 provider |
| `/api/providers` | POST | 创建自定义 provider（含 API Key 加密） |
| `/api/providers/{id}` | DELETE | 删除 provider |
| `/api/providers/{id}/test` | POST | 测试 provider 连接 |
| `/api/models` | GET | 列出所有可用模型 |
| `/api/plugins` | GET | 列出插件 |
| `/api/plugins/{id}/activate` | POST | 激活插件 |
| `/api/plugins/{id}/deactivate` | POST | 停用插件（核心插件返回 400） |
| `/api/health` | GET | 健康检查 |

### 1.2 WebSocket `/ws/chat`

| 帧类型 | 说明 | 数据 |
|---|---|---|
| `token_delta` | 流式 token 增量（直连 WS 推送） | `{delta}` 或 `{reasoning_content}` |
| `tool_event` | 工具调用过程 | `{tool_name, args, result, error}` |
| `context_snapshot` | 上下文构建快照 | `{session_id, messages, token_count, budget}` |
| `error` | 错误通知 | `{code, message}` 或 `{message}` |
| `done` | 对话完成 | `{content, iterations, latency_ms}` |

- 流式 token 走**直连 WS 推送**（包装 provider.chat 在 WebSocket 端直接 send_json），不经过 EventBus
- WS 参考客户端脚本：`tests/test_ws_client.py`

### 1.3 统一错误格式

所有错误响应统一格式：`{code, message, detail, trace_id}`

| 异常类型 | HTTP 状态码 | code |
|---|---|---|
| `APIError` | 400 (可自定义) | 自定义 |
| `ServiceUnavailable` | 503 | `SERVICE_UNAVAILABLE` |
| `PluginDeactivateError` | 400 | `PLUGIN_DEACTIVATE_FORBIDDEN` |
| `PluginValidationError` | 400 | `PLUGIN_VALIDATION_ERROR` |
| `PluginNotLoadedError` | 404 | `PLUGIN_NOT_FOUND` |
| `PluginError` | 400 | `PLUGIN_ERROR` |
| 其他未处理异常 | 500 | `INTERNAL_ERROR` |

### 1.4 核心插件保护

- `core: true` 插件的 deactivate 请求由 PluginLoader 抛出 `PluginDeactivateError`
- 全局异常处理器捕获后返回 400 + `{code: PLUGIN_DEACTIVATE_FORBIDDEN, detail: {plugin_id, reason}}`

### 1.5 OpenAPI 文档

- FastAPI 自动生成 OpenAPI 文档，访问 `/docs`（Swagger UI）和 `/redoc`
- 所有 REST 接口均有 summary 和 tags 分组

### 1.6 应用装配（main.py）

- `lifespan` 上下文管理器：启动时初始化核心组件（Database / SessionService / ContextService / ToolRegistry / 内置工具）
- 全局 `ServiceRegistry` 管理所有服务实例
- 全局 `HookManager` 和 `ToolRegistry` 供 AgentLoop 使用

---

## 2. 测试覆盖（13 个新测试，累计 126 个）

| 测试文件 | 数量 | 覆盖场景 |
|---|---|---|
| `test_api_rest.py` | 11 | 健康检查回归 / 会话 CRUD（创建/列表/获取/404/重命名/归档/删除）/ 消息追加+列表 / 统一错误格式 trace_id / 插件列表 |
| `test_api_ws.py` | 2 | WS 连接 + 无效消息 error 帧 / WS 消息 + 不存在的 provider error 帧 |

---

## 3. 自验结果

| 检查项 | 命令 | 结果 |
|---|---|---|
| 单元测试 | `uv run pytest -v` | 126 passed |
| 代码检查 | `uv run ruff check .` | All checks passed |
| 类型检查 | `uv run mypy harness` | no issues found in 44 source files |

---

## 4. DoD 逐项对照

| 完成标准 | 状态 | 验证方式 |
|---|---|---|
| Swagger UI 可调试全部 REST 接口 | 通过 | FastAPI 自动生成 `/docs`，所有路由有 summary + tags |
| WS 脚本完成一次含工具调用的流式对话，帧序列完整、落库正确 | 部分通过 | WS 帧类型（token_delta/tool_event/context_snapshot/error/done）已就绪；完整流式对话需真实 API Key |
| API 集成测试全绿 | 通过 | 126 passed（含 REST CRUD + WS 连接 + 错误格式） |
| 尝试通过 API 停用核心插件返回明确错误 | 通过 | `PluginDeactivateError` → 400 `PLUGIN_DEACTIVATE_FORBIDDEN` |

---

## 5. 遇到的问题与解决

| 问题 | 原因 | 解决 |
|---|---|---|
| `PluginNotFoundError` 不存在 | exceptions.py 中是 `PluginNotLoadedError` | 更正导入和引用 |
| logging `%s` 参数不匹配 | 日志模板有 3 个 `%s` 但只传了 2 个参数 | 修正为 2 个 `%s` |
| ruff E402 模块级导入在文件中间 | `from ... import router` 在 `setup_*_routes` 之后 | 移到文件顶部 |
| mypy `Returning Any` | `to_dict()` / `list_providers()` 返回 Any | `cast("dict[str, Any]", ...)` 显式转换 |
| mypy `asynccontextmanager` 返回类型 | `-> None` 不匹配异步生成器 | 改为无返回类型标注 + `# type: ignore` |

---

## 6. 待用户验证

启动后端后可访问：

- **Swagger UI**: http://localhost:8000/docs — 可调试全部 REST 接口
- **ReDoc**: http://localhost:8000/redoc — API 文档
- **WS 客户端脚本**: `uv run python -m tests.test_ws_client --session-id <id> --provider deepseek`

---

## 7. 后续阶段衔接

- **P6**：前端调用 WS `/ws/chat` 实现流式渲染 + REST API 调用会话/消息 CRUD
- **P7**：前端设置页调用 provider CRUD + 测试连接 + 插件管理
