# andy-harness 架构设计文档

> andy-harness 是一个插件化的 Agent Harness（智能体底座）开源项目：提供对话 GUI、多模型接入（DeepSeek / Qwen / Doubao / 自定义）、会话管理、上下文管理、沙箱管理与系统设置。所有功能以插件形式构建，模块间完全解耦，面向开发者学习与二次开发。

---

## 0. 设计原则

1. **一切皆插件（Everything is a Plugin）**：内核极小且不含任何业务逻辑；连"会话管理、系统设置"这类内置能力也以插件实现。插件机制因此是真正的一等公民，而非事后补丁。
2. **契约优先（Contract First）**：模块之间只依赖 `kernel/contracts/` 中的抽象接口，绝不 import 其他模块的具体实现。
3. **双向解耦的通信**：同步调用走 **服务注册表**，异步通知走 **事件总线**，拦截改写走 **钩子管线**。任意模块被移除/替换，系统其余部分仍可运行（优雅降级）。
4. **前后端同构插件机制**：后端功能插件与前端 UI 插件使用同一套"清单 + 生命周期 + 事件"思想。
5. **渐进式复杂度**：本地跑通只需 SQLite + 子进程沙箱；Docker 沙箱、远程渠道等作为可插拔的后端实现逐步加入。

---

## 1. 技术选型

| 层 | 选型 | 理由 |
|---|---|---|
| 后端 | Python 3.11+ / FastAPI | AI 生态最丰富，插件动态加载天然友好，自带 OpenAPI 文档 |
| 实时通信 | WebSocket（对话流）+ REST（管理面） | 流式输出与 CRUD 分离，职责清晰 |
| 存储 | SQLite（原生 sqlite3）| 零依赖起步；Repository 抽象层保留切换 Postgres 的能力 |
| 前端 | Vue 3 + Vite + TypeScript + Pinia + Vue Router | 学习社区主流，组件化契合 UI 插件机制 |
| Markdown 渲染 | markdown-it + highlight.js | 支持代码高亮、表格、GFM 语法 |
| 密钥存储 | Fernet 对称加密（密钥派生自本地 machine key）| API Key 不落明文 |
| 沙箱 | 阶段一：子进程 + 工作区隔离；阶段二：Docker（可插拔后端） | 安全能力可演进 |
| 包管理 | uv（后端）/ pnpm（前端） | 快且适合 monorepo |

---

## 2. 总体分层

```
┌────────────────────────────────────────────────────────────┐
│ L1 接入层 Interface      REST API · WebSocket 网关 · CLI    │
├────────────────────────────────────────────────────────────┤
│ L2 应用服务层 App Services（全部是内置插件）                 │
│    会话管理 · 上下文管理 · 模型服务 · 沙箱管理 · 系统设置     │
├────────────────────────────────────────────────────────────┤
│ L3 核心引擎 Engine       Agent Loop · 工具编排 · Hook 管线  │
├────────────────────────────────────────────────────────────┤
│ L4 内核 Kernel（无业务）                                    │
│    插件加载器 · 注册表 · 事件总线 · 服务注册表 · 契约接口     │
├────────────────────────────────────────────────────────────┤
│ L5 基础设施 Infra        SQLite · 文件存储 · 日志 · 加密     │
└────────────────────────────────────────────────────────────┘
```

**依赖规则（强制）**：
- 只允许上层依赖下层，禁止反向依赖与同级横向 import。
- 跨模块协作只能通过 L4 内核提供的三样东西：**服务接口、事件、钩子**。
- L2 的每个内置插件只面向 `contracts` 编程，因此可以被第三方插件整体替换（例如把 SQLite 会话存储换成云端实现）。

---

## 3. 插件机制设计（核心）

### 3.1 插件类型

| 类型 | 契约接口 | 说明 | 例子 |
|---|---|---|---|
| `provider` | `ModelProviderPlugin` | 大模型接入 | provider-deepseek / provider-qwen / provider-doubao |
| `tool` | `ToolPlugin` | Agent 可调用工具 | tool-code-runner / tool-web-search / tool-web-fetch / tool-theme-switcher / tool-ip-lookup |
| `service` | `ServicePlugin` | 后台常驻服务（activate/deactivate） | session-manager / context-manager / sandbox-manager / jev-manager |
| `hook` | `HookPlugin` | 生命周期拦截器 | 内容审计、日志埋点 |
| `channel` | `ChannelPlugin` | 外部接入渠道 | CLI（内置 channel） / 飞书 / Telegram（后期） |
| `ui`（前端） | `UIPlugin` | 前端面板/路由/设置项扩展 | hello-plugin |

> **CLI 定位**：CLI 是内置 channel 插件，与 REST/WS 同属 L1 接入层，但以插件形式实现。它可以被停用或被第三方 channel 替换。

### 3.2 插件清单 `plugin.json`

```json
{
  "id": "provider-deepseek",
  "name": "DeepSeek Provider",
  "version": "0.1.0",
  "type": "provider",
  "entry": "plugins.provider_deepseek.main:DeepSeekProviderPlugin",
  "core_api": ">=0.1.0 <1.0.0",
  "permissions": ["network"],
  "description": "DeepSeek 大模型 provider 插件"
}
```

- `entry` 格式为 `module:Class`，PluginLoader 动态 import 并实例化。
- `core_api` 做语义化版本兼容校验，内核升级时可明确拒绝不兼容插件。
- `permissions` 声明式授权：网络、文件、密钥读取等。

### 3.3 生命周期

```
discover → validate → load → register → activate ⇄ deactivate → unload
```

- **validate**：校验 manifest schema、core_api 兼容性、依赖插件是否存在。
- **load/activate 分离**：安装不等于启用；停用插件不删数据。
- **优雅降级**：插件停用后，依赖它的调用返回标准 `ServiceUnavailable` 错误，事件订阅自动注销，ToolRegistry 自动清理该插件注册的工具，不拖垮系统。
- **核心插件保护**：标记为 `"core": true` 的内置插件（如 session-manager、context-manager）不可被停用或卸载，防止系统自锁。

### 3.4 PluginContext（注入每个插件的受限门面）

插件拿到的不是整个内核，而是一个受限上下文：

```python
class PluginContext:
    plugin_id: str                  # 插件唯一标识
    logger: Logger                  # 带插件 id 前缀
    config: dict                    # 插件配置（从 manifest config_schema 注入）
    events: EventBus                # publish / subscribe
    services: ServiceRegistry       # register / get(接口) —— 跨插件同步调用
```

### 3.5 三种通信机制的分工

| 机制 | 模式 | 适用场景 | 示例 |
|---|---|---|---|
| **事件总线 EventBus** | 异步 pub/sub，AMQP 风格通配符（`*` 单段、`#` 多段） | 状态广播、解耦通知 | `message.completed`、`model.request.delta` |
| **服务注册表 ServiceRegistry** | 同步接口调用（按契约解析实现），支持 stack 回退 | 需要返回值的请求-响应 | `services.get(ContextService).build(session_id)` |
| **钩子管线 HookManager** | 有序管线，可改写数据/短路，支持 metadata 传播 | 请求/响应拦截、审计 | `pre_model_call`、`pre_tool_call` |

**服务注册冲突策略**：当多个插件注册同一服务接口时，采用「后注册覆盖先注册」策略——最后 activate 的插件实现生效。停用后自动回退到上一个实现。

**EventBus 可靠性说明**：EventBus 本质是异步 fire-and-forget，适用于旁路通知（如前端状态刷新、审计日志）。**对话流式推送链路不依赖 EventBus**——Agent Engine 直接持有 WS 连接引用进行流式推送，保证 token 不丢帧。EventBus 仅作为旁路广播（如多客户端同步、日志记录），容忍丢失。

### 3.6 关键事件命名

命名规则：`<域>.<...实体路径>.<动作>`，其中 `<...实体路径>` 可含一级或多级子实体，以 `.` 分隔。域和动作各为单段。

- `session.created` / `session.archived`
- `message.created` / `message.completed`
- `model.request.started` / `model.request.delta` / `model.request.completed` / `model.request.failed`
- `tool.call.started` / `tool.call.completed` / `tool.call.failed`
- `sandbox.exec.started` / `sandbox.exec.completed` / `sandbox.exec.blocked`
- `plugin.activated` / `plugin.deactivated`
- `settings.changed`

### 3.7 钩子清单与契约（同步、可改写）

**钩子执行顺序**：

```
pre_context_build → post_context_build → pre_model_call → post_model_call → pre_tool_call → post_tool_call → pre_message_persist
```

**钩子处理器契约**：

```python
@dataclass
class HookContext:
    """钩子处理器接收的上下文，每个钩子点填充不同字段"""
    hook_name: str                        # 钩子点名称
    data: Any                             # 可改写的数据负载（如 messages、model_request）
    session_id: str                       # 当前会话
    metadata: dict                        # 附加元信息（provider 名、tool 名等）

@dataclass
class HookResult:
    """钩子处理器返回值"""
    data: Any | None = None               # 改写后的数据（None 表示不修改）
    short_circuit: bool = False           # 是否短路（跳过后续钩子与原始操作）
    error: str | None = None              # 错误信息（短路时可附带）
    metadata: dict | None = None          # 附加元信息（管线传播）
```

**各钩子点的 `HookContext.data` 具体类型**：
- `pre_context_build` / `post_context_build`：data 为 `BuildContext`（session_id, budget, model, messages）
- `pre_model_call` / `post_model_call`：data 为 `ModelRequest`（messages, model, params, response, tool_calls）
- `pre_tool_call` / `post_tool_call`：data 为 `ToolCallContext`（tool_name, args, result, error）
- `pre_message_persist`：data 为 `MessageRecord`（session_id, role, content, tool_calls, tool_call_id）

- 钩子按注册顺序依次执行，前一个钩子的输出 `data` 作为下一个钩子的输入。
- `short_circuit = True` 时立即终止管线，`error` 作为错误信息返回。

### 3.8 前端插件机制

前端采用与后端同构的插件思想，但实现基于 Vue 生态：

**前端插件清单 `ui-plugin.json`**：

```json
{
  "id": "hello",
  "name": "Hello Plugin",
  "version": "0.1.0",
  "type": "ui",
  "entry": "main:HelloPlugin",
  "contributes": {
    "views": [
      { "id": "hello", "route": "/hello", "title": "Hello", "component": "components/HelloView.vue" }
    ],
    "menu_items": [
      { "id": "hello-menu", "parent": "sidebar", "label": "Hello", "icon": "wave", "view_id": "hello" }
    ]
  }
}
```

**前端插件生命周期**：

```
discover → validate → load → register → activate ⇄ deactivate → unload
```

- **discover**：扫描 `frontend/src/plugins/` 目录下的 `ui-plugin.json`。
- **load**：动态 import entry 模块。
- **register**：将 `contributes` 中的 views / menu_items 注册到前端插件注册表。
- **activate**：路由注入（`router.addRoute()`）、菜单项挂载。
- **deactivate**：路由移除、菜单项卸载。

---

## 4. 核心模块详设

### 4.1 Agent 引擎（L3）

ReAct 风格循环：

```
用户输入 → session.append
  → hooks.pre_context_build
  → ContextService.build(token 预算内装配)
  → 注入系统时间提示 + 用户自定义 system_prompt
  → hooks.post_context_build
  → hooks.pre_model_call
  → Provider.chat(stream) ──增量──→ 直接写入 WS 连接（保证不丢帧）
  │                              └─→ EventBus 旁路广播 model.request.delta（容忍丢失）
  → 解析响应（content + tool_calls 流式分片按 index 累积）
  → 若返回 tool_calls：
        持久化带 tool_calls 的 assistant 消息（API 规范）
        hooks.pre_tool_call → ToolRegistry.execute → hooks.post_tool_call
        回填 tool 结果（带 tool_call_id）→ 回到调模型
  → hooks.pre_message_persist → message 落库 → message.completed
```

**关键实现细节**：
- **流式 tool_calls 分片累积**：DeepSeek/OpenAI API 将一个 tool_call 的 `arguments` 拆成多个 SSE 块发送，必须按 `index` 合并 name 和 arguments 片段，否则参数永远不完整。
- **tool_call_id 全链路**：DeepSeek API 要求 tool 消息必须包含 `tool_call_id`，且前一条 assistant 消息必须包含对应的 `tool_calls`。MessageRecord → Message → MessageRepository → SessionService → ContextManager 全链路传递。
- **孤立 tool 消息清理**：压缩策略可能把 assistant 的 tool_calls 消息丢弃但保留 tool 消息，ContextManager 在压缩前后两次清理孤立的 tool 消息，避免 API 400 错误。
- **失败轮次清理**：AgentLoop 异常中断时，自动从会话末尾删除残留的未完成消息（带 tool_calls 的 assistant 消息和 tool 消息），防止下次对话上下文错乱。
- **content=null 处理**：assistant 消息带 tool_calls 但无实际内容时，content 设为 null（符合 OpenAI API 规范），避免 422 错误。
- **模型调用重试**：带指数退避的重试机制（默认 3 次），401 不可重试，429/网络错误可重试。
- **停止生成**：支持运行中停止，WS 发送 stop 控制消息，AgentLoop 检测 `_stopped` 标志中断循环。

### 4.2 模型服务

三家厂商均提供 **OpenAI 兼容端点**，因此抽象为：`OpenAICompatibleProvider` 基类 + 各厂商薄子类：

| Provider | base_url | 模型 |
|---|---|---|
| DeepSeek | `https://api.deepseek.com` | deepseek-v4-flash / deepseek-v4-pro / deepseek-chat / deepseek-reasoner |
| Qwen | `https://dashscope.aliyuncs.com/compatible-mode/v1` | qwen-max / qwen-plus / qwen-turbo / qwen-long |
| Doubao | `https://ark.cn-beijing.volces.com/api/v3` | doubao-pro-4k / doubao-pro-32k / doubao-lite-4k |

- **DeepSeek 特有**：`reasoning_content` 字段单独处理（思维链），前端逐步展示推理过程，完成后折叠。
- **统一能力**：`chat(stream)` / `list_models()` / `count_tokens()` / `health_check()`（GET /models 端点验证，不消耗 token）。
- **TokenCounter**：使用 tiktoken 估算（类级缓存编码器），`ProviderTokenCounter` 组合式委托到正确 provider。
- **API Key 管理**：Fernet 加密入库，`ProviderRegistry` 支持 `api_key`（明文）和 `api_key_encrypted`（密文）两种来源，运行时自动解密。环境变量 `DEEPSEEK_API_KEY` 作为回退。
- **ProviderRegistry**：统一管理内置与自定义 provider，通过 `get_provider(id)` 获取实例（不感知配置来源差异）。实例缓存，配置更新时清除缓存。`get_provider` 默认拒绝已停用的 provider，测试连接场景可用 `include_disabled=True` 豁免。
- **Provider 启用/停用状态**：`list_providers()` 返回 `enabled` 字段；`ProviderUpdate` 支持更新 `enabled`；停用后 provider 的模型从 `/api/models` 隐藏、Token 计数跳过、WS 聊天拒绝调用。内置 provider 的密钥和启用状态通过 `_config_overrides` 机制持久化到数据库，重启后自动合并。

### 4.3 会话管理（session-manager，service 插件）

- 会话 CRUD、重命名、归档、消息追加。
- 消息持久化：角色（user/assistant/system/tool）、tool_calls、tool_call_id、token 用量、耗时。
- **自动生成标题**：首次提问后，AgentLoop 完成时用 AI 生成不超过 20 字的简短总结标题（temperature=0.3），通过 WS done 帧后前端自动刷新侧边栏。
- **会话级设置**：model、temperature、system_prompt 通过 WS 消息传递到 AgentLoopConfig。

### 4.4 上下文管理（context-manager，service 插件）

- **策略可插拔**：滑动窗口（默认 20 条）/ 超额摘要压缩 / 关键消息钉住（pin）/ 系统提示词模板。
- token 预算分配：system > pinned > 近期消息 > 摘要，超限触发压缩策略，首次压缩后仍超预算则回退到摘要压缩策略。
- token 计数通过 `services.get(TokenCounter)` 调用 provider 实现。
- **消息格式化**：转换为 `{role, content}` 格式，保留 tool_call_id 和 tool_calls；assistant 带 tool_calls 时 content 设为 null。
- **孤立消息过滤**：构建上下文时过滤缺少 tool_call_id 的 tool 消息（旧数据兼容），并在压缩前后两次清理孤立 tool 消息。
- **快照持久化**：每次 build 生成快照记录存入 `context_snapshots` 表，前端可查看"本轮实际上下文快照"。

### 4.5 沙箱管理（sandbox-manager，service 插件）

- 每个会话独立工作区目录；代码执行经 `SandboxBackend` 接口：
  - `LocalSubprocessBackend`：子进程 + 超时 + 输出截断 + 内存/CPU 限制 + 危险命令黑名单 + 进程树终止。
    - **平台适配**：Linux/macOS 使用 `resource` 模块做内存/CPU 限制；Windows 使用 `psutil` 做进程级内存限制 + subprocess 超时控制。
    - **预装包白名单**：`requirements-sandbox.txt` 配置（numpy、matplotlib、pandas 等）。
  - `DockerBackend`（后期）：容器级隔离、网络开关、镜像白名单。
- `tool-code-runner` 工具插件只面向 `SandboxService` 编程，不感知后端实现。

### 4.6 系统设置（settings，前端 store）

- **多主题系统**：5 套主题（matrix 黑客帝国 / ocean 深海蓝 / sunset 日落紫红 / dark 中性深灰 / light 亮色），通过 CSS 变量 `html[data-theme='xxx']` 切换，localStorage 持久化。
- **AI 主题切换**：`tool-theme-switcher` 工具插件让 AI 可调用主题切换，前端监听 tool_event 中的 `__THEME__:xxx` 指令自动应用。
- **会话级设置**：模型、温度、系统提示词覆盖（通过 WS 消息传递到 AgentLoop）。
- **国际化**：中文/英文双语支持。

### 4.7 插件管理器（plugin-manager，REST API）

- 插件列表、启停、配置读写、权限说明。
- **安装接口** `POST /api/plugins/install`：用户在 UI 填写 plugin_id/name/entry/description/plugin_code，后端自动创建插件目录和文件，加载并激活插件，无需任何项目代码改动。
- **卸载接口** `DELETE /api/plugins/{plugin_id}`：删除插件文件并注销。
- **配置接口** `GET|PATCH /api/plugins/{plugin_id}/config`：读取和更新插件配置，按 `config_schema` 自动渲染表单。
- **核心插件保护**：`core: true` 插件的停用/卸载按钮灰显并提示不可操作，API 层也拒绝此类请求。

### 4.8 Jev 结构化决策（jev-manager，service 插件）

集成 TypeSafe AI Jev 结构化决策模型，支持三种原语：

| 原语 | 类型 | 输入 | 输出 |
|---|---|---|---|
| Choice | 选择型 | question + options[] | selected + probabilities + confidence |
| Score | 评分型 | question + scale[] | score + probabilities + confidence |
| Noul | 是非型 | proposition | probability (0~1) |

- **JevManager** 封装 `/jev/decide` API，三种原语可混合并行提问，独立计算、同时返回。
- **服务注册**：激活时将 `JevManager` 实例注册到 `ServiceRegistry`，工具插件通过 `services.get(JevManager)` 调用。
- **配置**：`api_key`（必填，secret）+ `base_url`（默认 `https://api.typesafe.ai/v1`），通过设置页面插件管理配置。
- **优雅降级**：未配置 API Key 时插件仍可激活（记录警告），调用 Jev 工具时返回友好错误提示。
- **环境变量**：`JEV_API_KEY` 作为回退。

---

## 5. 模块间依赖与通信关系

```
前端 GUI ──REST/WS──▶ API 层 ──▶ Agent 引擎
                                   │
        ┌──────────────┬───────────┼─────────────┬────────────┐
        ▼              ▼           ▼             ▼            ▼
   ContextService  Provider插件  Tool插件    SandboxService  Settings
   (context-mgr)   (model-mgr)   (注册表)    (sandbox-mgr)   (前端 store)
        └──────────────┴──── 全部经由内核 EventBus / ServiceRegistry / Hooks ────┘
```

- **对话主链路**：`GUI → WS → AgentLoop → ContextService → Provider → (Tools/Sandbox) → 落库 → 事件 → WS → GUI`。
- **模块替换示例**：把 `session-manager` 停用并 activate 一个第三方"云端会话插件"，Agent 引擎零改动——它只认识 `SessionService` 接口。

---

## 6. 数据模型（SQLite）

| 表 | 关键字段 | 说明 |
|---|---|---|
| `sessions` | id, title, config_json, created_at, updated_at, archived | 会话 |
| `messages` | id, session_id, role, content, tool_calls_json, tokens, latency_ms, created_at | 消息（tool_calls_json 同时存储 tool_calls 和 tool_call_id） |
| `providers` | id, name, type, base_url, api_key_encrypted, models_json, extra_params_json, enabled | 自定义 provider |
| `settings` | key, value_json, scope(global/session) | 设置项 |
| `plugins` | id, version, enabled, config_json, core | 插件配置（含内置 provider 的 api_key/base_url） |
| `context_snapshots` | id, session_id, message_id, messages_json, token_count, budget, created_at | 上下文快照 |

---

## 7. 实际目录结构（monorepo）

```
andy-harness/
├── README.md  LICENSE  Makefile
├── 01-architecture.md       # 架构设计文档
├── 02-development-plan.md  # 分阶段开发计划
├── docs/                    # 各阶段总结 + 密钥文件
├── backend/
│   ├── pyproject.toml       # uv + hatchling，依赖：fastapi/uvicorn/httpx/cryptography/tiktoken
│   ├── requirements-sandbox.txt  # 沙箱预装包白名单
│   ├── data/harness.db      # SQLite 数据库
│   ├── harness/
│   │   ├── main.py          # FastAPI 入口（装配 + 插件加载 + 优雅关机）
│   │   ├── kernel/          # L4 内核：无业务
│   │   │   ├── loader.py    # 发现/校验/加载/激活/停用
│   │   │   ├── eventbus.py  # 事件总线（async + AMQP 风格通配符）
│   │   │   ├── services.py  # 服务注册表（stack 回退 + 线程安全）
│   │   │   ├── hooks.py     # 钩子管线
│   │   │   ├── context.py   # PluginContext
│   │   │   ├── exceptions.py
│   │   │   └── contracts/   # 全部抽象接口
│   │   │       ├── base.py        # PluginType / PluginManifest / BasePlugin
│   │   │       ├── provider.py    # ModelProviderPlugin
│   │   │       ├── tool.py        # ToolPlugin（继承 ABC，无生命周期）
│   │   │       ├── service.py     # ServicePlugin
│   │   │       ├── hook.py        # HookPlugin / HookContext / HookResult
│   │   │       ├── channel.py     # ChannelPlugin
│   │   │       └── token_counter.py  # TokenCounter
│   │   ├── engine/          # L3：Agent Loop / 工具编排 / 钩子
│   │   │   ├── agent_loop.py      # 核心循环（重试/工具/清理/系统提示）
│   │   │   ├── tool_registry.py   # 工具注册表
│   │   │   ├── builtin_tools.py   # 内置工具（计算器/时间）
│   │   │   └── hook_types.py      # 钩子数据结构
│   │   ├── modules/         # L2：内置插件
│   │   │   ├── session_manager/   # 会话管理（CRUD + tool_call_id）
│   │   │   ├── context_manager/   # 上下文管理（策略 + 孤立消息清理）
│   │   │   ├── model_manager/     # 模型服务（ProviderRegistry + OpenAI兼容基类）
│   │   │   └── sandbox_manager/   # 沙箱管理（子进程 + 资源限制）
│   │   ├── api/             # L1：REST + WS
│   │   │   ├── rest/        # sessions/messages/providers/models/settings/plugins
│   │   │   └── ws/chat.py   # WebSocket 对话网关（流式/工具事件/停止/自动标题）
│   │   ├── cli/             # CLI channel
│   │   └── infra/           # L5：SQLite / 加密 / Repository
│   ├── plugins/             # 扩展插件（自动扫描加载）
│   │   ├── provider_deepseek/  provider_qwen/  provider_doubao/
│   │   ├── session_manager/  context_manager/  jev_manager/
│   │   ├── tool_code_runner/  tool_web_search/  tool_web_fetch/
│   │   ├── tool_theme_switcher/  tool_ip_lookup/
│   │   └── hello_plugin/
│   └── tests/               # pytest 测试（20+ 测试文件）
├── frontend/
│   ├── package.json         # vue3/vite/pinia/vue-router/markdown-it/highlight.js
│   └── src/
│       ├── main.ts  App.vue  style.css  # 入口 + 全局样式（多主题 CSS 变量）
│       ├── api/             # REST + WS 客户端（断线重连 + 单连接管理）
│       ├── components/      # MarkdownRenderer / MatrixRain / MessageItem / SessionSidebar / ModelSelector
│       ├── composables/     # useLanguage / useRipple
│       ├── core/            # 前端插件内核（loader/context/types/event-bus-bridge）
│       ├── i18n/            # 中文/英文
│       ├── plugins/         # 前端 UI 插件
│       ├── router/          # Vue Router
│       ├── stores/          # chat / providers / plugins / settings
│       └── views/           # ChatView / SettingsView / HomeView
└── .github/workflows/       # CI
```

---

## 8. API 概览

- REST：`/api/sessions`、`/api/sessions/{id}/messages`、`/api/providers`（含 `PATCH /{id}` 更新配置和启用状态、`POST /{id}/test` 测试连接，返回 `enabled` 字段和具体错误信息）、`/api/models`（仅返回已启用 provider 的模型）、`/api/settings`、`/api/plugins`（含 `POST /install` 安装、`DELETE /{id}` 卸载、`POST /{id}/activate`、`POST /{id}/deactivate`、`GET|PATCH /{id}/config`）
- WS：`/ws/chat` —— 帧类型见下表
- 统一错误格式：`{code, message, detail, trace_id}`

**WS 帧类型与后端事件映射**：

| WS 帧类型 | 说明 |
|---|---|
| `token_delta` | 流式 token 增量（含 `delta` 正文和 `reasoning_content` 思维链） |
| `tool_event` | 工具调用过程（实时推送 tool_name/args/result/error） |
| `context_snapshot` | 上下文构建快照（token_count/budget/messages） |
| `error` | 错误通知（含 code/message/detail/trace_id） |
| `done` | 对话完成（含 content/iterations/latency_ms/short_circuited） |
| `stop_ack` | 停止确认 |

**WS 控制消息**：发送 `{"type": "stop"}` 可中断正在进行的生成。

---

## 9. 安全要点

- **API Key 加密存储**：使用 Fernet 对称加密，密钥派生自本地 machine key（Windows: `winreg` MachineGuid；Linux: `/etc/machine-id`；macOS: `IOPlatformUUID`）。
- **沙箱安全**：默认无网络、限制内存/CPU 时间、工作区外路径拒绝访问、危险命令黑名单、进程树终止。
- **插件权限**：插件权限声明在 `plugin.json` 的 `permissions` 字段。

---

## 10. 已实现的特色功能

1. **Matrix 数字雨首页**：Canvas 实现的 0/1 瀑布雨，鼠标滑过区域数字弯曲，科幻黑绿主题。
2. **思维链展示**：DeepSeek `reasoning_content` 逐步流式展示，完成后自动折叠，下方显示正文回答。
3. **Markdown 渲染**：聊天消息和流式内容均使用 markdown-it 渲染，支持代码高亮、表格、GFM。
4. **多主题切换**：5 套 CSS 变量主题，设置页面可视化选择，AI 也可通过 theme-switcher 工具切换。
5. **插件在线安装**：用户在 UI 填写表单即可安装新插件，无需任何代码改动。
6. **自动会话标题**：首次提问后 AI 自动生成简短总结标题。
7. **微交互**：涟漪效果、右键菜单、长按等。
8. **会话管理**：新建/重命名/归档/删除/清空全部。
9. **Jev 结构化决策**：集成 TypeSafe AI Jev 模型，支持 Choice/Score/Noul 三种原语，AI 可自动调用进行结构化判断。
10. **Provider 状态管理**：启用/停用状态联动，启用前自动测试连接，停用后模型和工具自动隔离。

---

## 11. 演进路线（架构预留）

- **v0.1**（当前）：核心功能完整（对话、多模型、会话/上下文、沙箱、设置、插件管理、多主题）。
- **v0.2**：会话分支与导出、Docker 沙箱后端、插件从 zip/git 安装。
- **v0.3+**：桌面壳（Tauri）→ 多 Agent 协作 → 远程 channel（飞书/Telegram）→ 插件市场。

以上全部通过新增插件/渠道实现，内核无需改动。
