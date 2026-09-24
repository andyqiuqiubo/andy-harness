# andy-harness 分阶段开发计划

> 本文档记录 andy-harness 项目的分阶段开发计划。P0–P9 已全部完成并通过验收，当前版本为 v0.1.0。每阶段包含目标、任务清单和完成标准（DoD）。

---

## Phase 0 — 仓库与工程脚手架 ✅

**目标**：monorepo 骨架可一键启动，工程规范与 CI 就位。

**任务清单**
- [x] 建立目录结构（backend / frontend / docs / examples），MIT LICENSE
- [x] 后端：uv + FastAPI 最小应用，`GET /api/health` 返回 `{"status":"ok"}`
- [x] 前端：Vite + Vue3 + TS + Pinia + Vue Router，首页显示项目名
- [x] 规范：ruff + mypy（后端）、eslint + prettier（前端）、pre-commit、.gitignore
- [x] GitHub Actions：lint + test + build；Makefile 一键启动前后端

**完成标准（DoD）**
1. 全新克隆后按 README 两条命令启动：后端 `/api/health` 返回 ok，前端页面正常显示 ✓
2. CI 绿灯；pre-commit 本地生效 ✓

---

## Phase 1 — 内核：事件总线 / 服务注册表 / 插件加载器 ✅

**目标**：最小内核可发现、加载、启停插件，三种通信机制可用。

**任务清单**
- [x] `EventBus`：async 发布/订阅、AMQP 风格通配符主题（`*` 单段、`#` 多段）、订阅注销
- [x] `ServiceRegistry`：按抽象接口注册/解析，支持 stack 回退策略（后注册覆盖先注册，停用后回退）
- [x] `HookManager`：有序管线，支持改写数据与短路，metadata 传播
- [x] **钩子处理器契约数据结构**：`HookContext`（hook_name / data / session_id / metadata）和 `HookResult`（data / short_circuit / error / metadata）
- [x] **`TokenCounter` 契约接口**：`count_tokens(text, model) -> int`，放入 `kernel/contracts/`
- [x] `plugin.json` schema + 校验（含 core_api 语义化版本检查、`"core": true` 标记支持）
- [x] `PluginLoader`：扫描 plugins 目录 → validate → load → activate/deactivate；核心插件不可被 deactivate
- [x] `PluginContext` 受限门面（plugin_id / logger / config / events / services）
- [x] 示例插件 `hello-plugin` + 单元测试

**完成标准（DoD）**
1. `pytest` 全绿，覆盖：加载/停用/服务调用/事件跨插件/钩子管线/核心插件保护 ✓
2. manifest 缺字段/版本不兼容时加载被拒绝并给出明确错误 ✓
3. `TokenCounter` 契约接口可被 mock 实现并通过 ServiceRegistry 调用 ✓

---

## Phase 2 — 模型抽象层 + DeepSeek / Qwen / Doubao 插件 ✅

**目标**：统一流式 chat 接口；内置三家 provider；支持自定义 OpenAI 兼容模型。

**任务清单**
- [x] `ModelProviderPlugin` 契约：`chat(stream)` / `list_models()` / `count_tokens()` / `health_check()`
- [x] `OpenAICompatibleProvider` 基类：流式解析、超时、指数退避重试、统一错误处理
- [x] 三个 provider 插件（薄子类；DeepSeek 处理 `reasoning_content` 思维链字段）
- [x] 各 provider 实现 `TokenCounter` 契约：tiktoken 估算（类级缓存编码器）
- [x] `ProviderRegistry`：统一管理内置与自定义 provider，`get_provider(id)` 获取实例（实例缓存）
- [x] API Key Fernet 加密存储（machine key 派生密钥，日志脱敏）
- [x] DeepSeek 模型：deepseek-v4-flash / deepseek-v4-pro / deepseek-chat / deepseek-reasoner
- [x] 环境变量 `DEEPSEEK_API_KEY` 回退支持
- [x] `health_check()` 使用 GET /models 端点验证（不消耗 token）

**完成标准（DoD）**
1. DeepSeek / Qwen / Doubao 三家真实调用全部流式跑通 ✓
2. 数据库中无明文密钥；错误密钥产生可读的错误信息 ✓
3. `services.get(TokenCounter).count_tokens("你好", "deepseek-chat")` 返回合理 token 数 ✓

---

## Phase 3 — 会话管理与上下文管理 ✅

**目标**：多轮对话可持久化；上下文装配策略化、可观测。

**任务清单**
- [x] SQLite 表：sessions / messages / context_snapshots；Repository 抽象层
- [x] `session-manager` 插件：会话 CRUD、重命名、归档、消息追加
- [x] `context-manager` 插件：`ContextService.build(session_id, budget)`，策略可插拔
  - [x] 滑动窗口策略（默认 20 条）
  - [x] 超额摘要压缩策略（首次压缩后仍超预算则回退到此策略）
  - [x] 关键消息钉住（pin）
  - [x] 系统提示词模板（变量插值）
- [x] token 计数通过 `services.get(TokenCounter)` 调用
- [x] token 预算分配：system > pinned > 近期消息 > 摘要
- [x] 上下文快照：每次 build 生成快照记录，可经 API 查询
- [x] **tool_call_id 全链路**：MessageRecord → Message → MessageRepository → SessionService → ContextManager
- [x] **孤立 tool 消息清理**：压缩前后两次过滤孤立的 tool 消息，避免 API 400/422 错误
- [x] **content=null 处理**：assistant 带 tool_calls 时 content 设为 null（API 规范）

**完成标准（DoD）**
1. 创建会话 → 多轮问答 → 重启服务后历史完整加载 ✓
2. 构造超长会话触发截断/摘要，token 数符合预算 ✓
3. 可通过 API 查看某轮对话的实际上下文快照 ✓
4. tool 消息包含 tool_call_id，不会导致 API 错误 ✓

---

## Phase 4 — Agent 引擎 ✅

**目标**：核心循环打通，支持工具调用与钩子拦截。

**任务清单**
- [x] `AgentLoop`：装配上下文 → 调模型 → 解析 tool_calls → 执行 → 回填，直至终答
- [x] `ToolPlugin` 契约（tool_name / description / parameters_schema / execute）+ 工具注册表
- [x] **钩子数据结构定义**：BuildContext / ModelRequest / ToolCallContext / MessageRecord
- [x] 钩子管线全量接入：pre/post_context_build、pre/post_model_call、pre/post_tool_call、pre_message_persist
- [x] 单轮最大工具迭代数保护（默认 10）、停止/中断
- [x] 模型失败重试与指数退避（默认 3 次，401 不可重试）
- [x] **流式 tool_calls 分片累积**：按 index 合并 name 和 arguments 片段
- [x] **带 tool_calls 的 assistant 消息持久化**：执行工具前先持久化（API 规范要求）
- [x] **失败轮次清理**：AgentLoop 异常中断时清理残留的未完成消息
- [x] **系统时间注入**：每次调模型时注入当前日期时间到 system 消息
- [x] **用户自定义 system_prompt 注入**：从会话级设置传递到 AgentLoopConfig
- [x] **工具定义传递给模型**：`tools` 和 `tool_choice` 参数传递给 provider.chat()
- [x] 内置示例工具：计算器、当前时间

**完成标准（DoD）**
1. 提问「现在几点？顺便算一下 123*456」→ agent 自动连续调用两个工具并给出终答 ✓
2. 工具异常被捕获并作为 tool 消息回填，不导致循环崩溃 ✓
3. hook 返回 `short_circuit=True` 时，Agent Loop 正确终止并返回错误信息 ✓
4. 流式 tool_calls 分片正确累积，工具参数完整 ✓
5. 失败轮次自动清理，下次对话上下文不错乱 ✓

---

## Phase 5 — 后端 API 层（REST + WebSocket） ✅

**目标**：把引擎能力完整暴露给前端。

**任务清单**
- [x] REST：sessions / messages / providers（含 `PATCH /{id}` 更新配置、`POST /{id}/test` 测试连接）/ models / settings / plugins
- [x] REST 保护核心插件：对 `core: true` 插件的 deactivate 请求返回 400 错误
- [x] **插件安装接口** `POST /api/plugins/install`：自动创建插件文件并加载激活
- [x] **插件卸载接口** `DELETE /api/plugins/{plugin_id}`：删除文件并注销
- [x] WS `/ws/chat`：发送消息 → 推流 `token_delta` / `tool_event` / `context_snapshot` / `error` / `done`
- [x] 流式 token 走直连 WS 推送（Agent Engine 持有 WS 连接引用），不经过 EventBus
- [x] EventBus 旁路广播 `model.request.delta`（容忍丢失）
- [x] **停止控制消息**：WS 发送 `{"type":"stop"}` 中断生成
- [x] **实时工具事件推送**：每个工具执行完成后立即推送 tool_event
- [x] **自动会话标题生成**：首次提问后用 AI 生成简短标题（temperature=0.3）
- [x] 统一错误格式 `{code, message, detail, trace_id}`

**完成标准（DoD）**
1. Swagger UI 可调试全部 REST 接口 ✓
2. WS 完成一次含工具调用的流式对话，帧序列完整、落库正确 ✓
3. 尝试通过 API 停用核心插件返回明确错误 ✓
4. 插件安装接口可在线安装新插件并立即可用 ✓

---

## Phase 6 — 前端 MVP：对话界面与前端插件内核 ✅

**目标**：浏览器内完成完整聊天体验；前端插件机制可用。

**任务清单**
- [x] **前端插件内核**：
  - [x] `ui-plugin.json` 清单格式与校验（contributes: views / menu_items）
  - [x] 前端 `PluginLoader`：扫描 → validate → load → register → activate/deactivate
  - [x] `UIPluginContext` 门面（logger / config / events / api / router / stores）
  - [x] `EventBusBridge`：WS 事件 → 前端事件总线
  - [x] 路由注入：`router.addRoute()` / `router.removeRoute()`
- [x] Chat 页面：会话侧栏（新建/切换/重命名/归档/删除/清空全部）、消息列表
- [x] **Markdown + 代码高亮渲染**：markdown-it + highlight.js，流式内容也支持 Markdown 渲染
- [x] 流式渲染 + 停止生成按钮 + 工具调用过程可视化（折叠卡片）
- [x] **思维链展示**：DeepSeek `reasoning_content` 逐步展示，完成后折叠
- [x] WS 客户端：断线重连、单连接管理（避免多连接导致 token 重复）
- [x] 模型选择器（按 provider 分组）
- [x] 设置按钮（header 右上角齿轮图标，跳转设置页面）
- [x] **会话标题截断显示**：超过 20 字显示 `...`，hover 显示完整标题
- [x] **自动刷新会话标题**：done 帧后从后端获取 AI 生成的标题更新侧边栏
- [x] **AI 主题切换监听**：监听 tool_event 中 `theme_switcher` 的 `__THEME__:xxx` 指令自动切换
- [x] 示例前端插件：hello 视图

**完成标准（DoD）**
1. 新建会话 → 提问 → 流式渲染 → 触发工具调用可见 → 切换会话历史正确 → 刷新页面后数据仍在 ✓
2. 中断生成、WS 断线重连均有明确 UI 反馈 ✓
3. 示例前端插件的视图出现在侧栏，停用后消失 ✓
4. 思维链逐步展示，完成后折叠 ✓
5. 流式内容 Markdown 正确渲染 ✓

---

## Phase 7 — 设置中心与插件管理界面 ✅

**目标**：不改代码、纯 GUI 完成模型与插件配置。

**任务清单**
- [x] 设置页：provider 列表（启停/编辑/删除）、新增自定义模型表单 + 「测试连接」按钮
- [x] 会话级设置：模型、温度、系统提示词覆盖（通过 WS 消息传递到 AgentLoop）
- [x] 插件页：列表、启停开关、配置表单（按 config_schema 自动渲染）、权限说明
- [x] **插件安装表单**：填写 plugin_id/name/entry/description/plugin_code 即可安装
- [x] **插件卸载按钮**
- [x] 核心插件保护：`core: true` 插件的停用/卸载按钮灰显并提示不可操作
- [x] **多主题系统**：5 套主题（matrix / ocean / sunset / dark / light），可视化选择卡片 + 色块预览
- [x] **插件列表滚动条**：max-height + 自定义滚动条样式
- [x] 通用设置：主题、语言（中/英）

**完成标准（DoD）**
1. 纯 UI 操作新增一个自定义 provider，测试连接成功并完成一次聊天 ✓
2. 停用工具插件后 agent 不再调用该工具；重新启用后恢复 ✓
3. 主题切换即时生效并持久化 ✓
4. 核心插件的停用按钮不可点击 ✓
5. 纯 UI 安装新插件并立即可用，无需任何代码改动 ✓

---

## Phase 8 — 沙箱管理 ✅

**目标**：安全的代码执行能力，后端实现可插拔。

**任务清单**
- [x] `SandboxService` 接口 + `LocalSubprocessBackend`：会话级工作区隔离、超时、输出截断
- [x] **平台适配的内存/CPU 限制**：Linux/macOS 使用 `resource` 模块；Windows 使用 `psutil` + subprocess 超时
- [x] 危险命令黑名单 + 工作区外路径访问拦截；默认禁网
- [x] **进程树终止**：超时或取消时杀死整个进程树
- [x] **预装包白名单**：`requirements-sandbox.txt` 配置（numpy、matplotlib、pandas 等）
- [x] `tool-code-runner` 插件：执行 Python / Shell，返回 stdout/stderr/exit_code
- [x] 执行事件 `sandbox.exec.*` 推送前端

**完成标准（DoD）**
1. 对话中让 agent「写一段 python 画正弦曲线」→ 沙箱执行成功 ✓
2. 测试证明：超时进程被终止、访问工作区外路径被拒、黑名单命令被拦截 ✓
3. 连续执行后宿主机无残留进程/临时文件泄漏 ✓

---

## Phase 9 — 开源打磨与发布 ✅

**目标**：社区就绪（Contributor 10 分钟跑起来）。

**任务清单**
- [x] 架构文档（01-architecture.md）：反映当前实际代码实现
- [x] 分阶段开发计划（02-development-plan.md）：记录已完成的 P0–P8
- [x] **插件开发指南**（docs/plugin-dev-guide.md）：后端工具/Provider 插件 + 前端 UI 插件 + 在线安装示例 + 调试技巧
- [x] 示例：后端工具插件（tool-web-search / tool-web-fetch / tool-theme-switcher / tool-ip-lookup）+ 前端 UI 插件（hello）
- [x] docker-compose 一键部署（含后端/前端 Dockerfile）
- [x] 版本号 v0.1.0、CHANGELOG.md、Roadmap
- [x] README 打磨：徽章、功能特性、快速开始、使用指南、项目结构、FAQ
- [x] issue / PR 模板、贡献指南（CONTRIBUTING.md）、行为准则
- [x] 测试覆盖率：141 个测试全部通过（内核 / API / 插件 / 沙箱 / 会话 / 上下文等）
- [x] CI 发布工作流（.github/workflows/ci.yml）

**完成标准（DoD）**
1. 找一台干净机器按 README 操作，10 分钟内跑通对话 ✓
2. 按插件开发指南从零写出一个新工具插件（后端）并运行成功 ✓（在线安装示例）
3. 按插件开发指南从零写出一个新 UI 插件（前端）并运行成功 ✓（hello 插件）
4. v0.1.0 发布：CI 绿、141 个测试全通过、文档链接全部有效 ✓

---

## 附：阶段依赖图

```
P0 脚手架 ✅
 └─ P1 内核（含 TokenCounter / HookContext 契约）✅
     ├─ P2 模型层（实现 TokenCounter）✅ ──────────────┐
     └─ P3 会话/上下文（mock TokenCounter，可并行）✅ ──┤
                                                        ▼
                                          P4 Agent 引擎 ✅ ── P5 API 层 ✅ ── P6 前端 MVP ✅ ── P7 设置/插件 UI ✅
                                                                       │
                                                            P8 沙箱 ✅（依赖 P4 工具机制）
                                                                       │
                                                                       └─ P9 开源发布 ✅
```

---

## 附：已实现的工具插件清单

| 插件 ID | 类型 | 说明 |
|---|---|---|
| `provider_deepseek` | provider | DeepSeek 大模型（v4-flash/v4-pro/chat/reasoner），含 reasoning_content 思维链 |
| `provider_qwen` | provider | 通义千问大模型 |
| `provider_doubao` | provider | 豆包大模型 |
| `session_manager` | service (core) | 会话 CRUD、消息持久化、tool_call_id |
| `context_manager` | service (core) | 上下文装配、压缩策略、孤立消息清理、快照 |
| `tool_code_runner` | tool | Python/Shell 代码执行（沙箱） |
| `tool_web_search` | tool | 百度搜索引擎搜索 |
| `tool_web_fetch` | tool | 网页正文内容抓取 |
| `tool_theme_switcher` | tool | AI 可调用切换前端主题 |
| `tool_ip_lookup` | tool | IP 地址地理位置查询 |
| `hello_plugin` | service | 示例插件（事件 + 服务 + 钩子） |

---

## 附：已实现的前端特性

1. **Matrix 数字雨首页**：Canvas 0/1 瀑布雨，鼠标弯曲效果
2. **多主题系统**：matrix(黑绿) / ocean(深蓝) / sunset(紫红) / dark(深灰) / light(亮色)
3. **思维链展示**：逐步流式展示，完成后折叠
4. **Markdown 渲染**：消息和流式内容均支持代码高亮、表格、GFM
5. **微交互**：涟漪效果、右键菜单、长按
6. **会话管理**：新建/重命名/归档/删除/清空全部
7. **自动会话标题**：AI 生成简短总结标题
8. **插件在线安装**：UI 表单填写即可安装
9. **国际化**：中文/英文
10. **断线重连**：WS 自动重连，单连接管理
