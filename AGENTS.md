# andy-harness 项目架构规范

> **重要**：本文档面向 Agent 与开发者，描述 `andy-harness` 的源码组织、核心架构、技术栈与协作约定。它是 Agent 在后续会话中理解上下文、定位文件、执行修改的权威参考。
> 更详尽的功能说明、快速开始与截图请阅读 [`README.md`](README.md)。

## 设计原则

- **一切皆插件**：内核 `kernel/` 只负责装配、事件、服务注册与生命周期，零业务逻辑；业务功能以插件/模块形式挂载。
- **前后端解耦**：后端 `backend/` 暴露 REST / WebSocket / SSE；前端 `frontend/` 通过统一 API client 与 runtime 层调用。
- **数据与状态本地化**：默认 SQLite 单文件；认证、多用户、敏感凭据按需开启。
- **优雅降级**：所有可选能力（桌面操控、Docker 沙箱、飞书群聊、REA 原生反编译等）均遵循「缺失时不阻塞核心对话」原则。

---

## 技术栈

| 层级 | 技术 |
|------|------|
| 后端 runtime | Python 3.11+，FastAPI，Uvicorn，httpx |
| 后端类型/质量 | mypy strict，ruff，pytest + pytest-asyncio |
| 数据库 | SQLite（默认），通过 `Repository`/`Database` 抽象保留切换 Postgres 空间 |
| 前端框架 | Vue 3.5（Composition API），TypeScript 6.x，Vue Router 4，Pinia 4 |
| 前端构建 | Vite 8，vue-tsc，vitest（测试），eslint，prettier |
| 桌面壳 | Tauri v2（Rust + WebView），将前后端打包为单窗口应用 |
| 包管理 | 后端 `uv`，前端 `pnpm`，桌面 `pnpm` |
| 操作系统 | 仓库在 **Windows 10** 下开发与验证；跨平台风险尚未排除 |

---

## 目录结构概览

```
H:/jsd-andyharness/
├── AGENTS.md                    # 本文件：项目结构与协作规范
├── README.md                    # 项目完整说明、功能清单、快速开始
├── DISCLAIMER.md                # 免责声明（AI 操作、Token 费用、安全风险）
├── LICENSE                      # MIT 许可证
├── Makefile                     # 前后端安装 / 启动 / 测试 / 检查 / 清理
├── mcp.example.json             # MCP 服务端配置模板
├── docker-compose.yml           # 可选 Docker 沙箱后端（Firecracker/PentAGI 不可用时降级）
├── start-all.bat                # 浏览器模式一键启动（后端 8000 + Vite 5173）
├── start-desktop.bat            # Tauri 桌面壳启动
├── stop-all.bat                 # 停止浏览器模式服务
├── stop-desktop.bat             # 停止桌面壳
├── start-services.ps1           # 服务启动辅助脚本
├── .github/                     # GitHub 模板与工作流
├── .pre-commit-config.yaml      # 提交前检查配置
├── backend/                     # Python 后端
│   ├── pyproject.toml           # 依赖、ruff、mypy、pytest 配置
│   ├── harness/                 # 后端源码包
│   │   ├── main.py              # FastAPI 入口，只装配、无业务逻辑
│   │   ├── cli/                 # CLI 入口（chat 等）
│   │   ├── kernel/              # 插件内核：服务、事件、钩子、加载器、契约
│   │   ├── engine/              # AgentLoop、工具注册、权限门、上下文压缩、checkpoint
│   │   ├── infra/               # 数据库、网络护栏、安全、原子写、子进程、仓库抽象
│   │   ├── api/                 # REST / WebSocket / 中间件 / 错误处理
│   │   ├── modules/             # 业务模块（见下文核心模块）
│   │   └── eval/                # 离线评测框架（CLI 可直接当 CI 门槛）
│   ├── tests/                   # 后端测试集（约 80+ 测试文件）
│   └── data/                    # 运行时数据库（gitignore）
├── frontend/                    # Vue3 + TypeScript 前端
│   ├── package.json             # 依赖与脚本
│   ├── tsconfig*.json           # TypeScript solution 与 app 配置
│   ├── vite.config.ts           # Vite 配置
│   ├── src/
│   │   ├── main.ts              # 入口：Pinia + Router + 插件加载 + 后端 runtime 初始化
│   │   ├── App.vue              # 根组件：认证锁屏、路由视图、插件悬浮层
│   │   ├── router/index.ts      # 路由表（pages 映射到 views）
│   │   ├── api/                 # HTTP client、token、runtime 地址解析
│   │   ├── views/               # 页面组件（Chat/Settings/Workflows/...）
│   │   ├── components/          # 可复用组件（SessionSidebar/MessageItem/...）
│   │   ├── stores/              # Pinia 状态（chat/settings/auth/providers/...）
│   │   ├── composables/         # 组合式函数（语言/涟漪效果/...）
│   │   ├── utils/               # 工具函数（含单元测试）
│   │   ├── i18n/                # 中文 / 英文国际化
│   │   ├── core/                # 插件上下文与加载器（前端 UI 插件）
│   │   ├── plugins/             # 内置前端插件示例（hello / pet）
│   │   └── style.css            # 全局 CSS 变量与主题
│   └── dist/                    # 构建产物（gitignore）
├── desktop/                     # Tauri 桌面壳
│   ├── package.json
│   ├── src-tauri/               # Rust 源码、Cargo、Tauri 配置、图标
│   └── launcher/                # launch-backend.py：拉起并回收后端进程
├── mcp_servers/                 # 本地 vendored MCP 服务
│   ├── rea/                     # REA 原生反编译 MCP server（源码集成，Node）
│   └── frida_mcp/               # 自研 Frida 动态插桩 MCP server（Python）
├── rev_eng/                     # 工程强逆向运行时产物与样本
│   ├── runs/                    # 流水线运行记录（gitignore）
│   ├── samples/                 # 测试样本
│   └── tools/                   # 重建工具脚本
├── docs/                        # 用户手册、营销/报告文档、媒体资产
│   ├── andy-harness-v1.0.0用户使用手册.md
│   ├── manual/screenshots/      # 界面截图
│   ├── marketing/               # 公众号推文等营销文案
│   ├── reports/                 # 代码审查/实施总结等报告
│   └── media/                   # 宣传视频、GIF 等媒体资产
├── tests/                       # 顶层测试集合
│   └── e2e/                     # 端到端测试与脚本
├── .tools/                      # 隔离工具链（gitignore，项目外不依赖）
│   ├── tcc/                     # TCC 编译器（重建 exe 用）
│   ├── ghidra_12.1.4_PUBLIC/    # REA 反编译依赖
│   ├── jdk-21/                  # 项目内 JDK
│   └── java/                    # CFR 反编译器 jar 等
├── .workbuddy/                  # IDE 记忆目录（gitignore）
└── logs/                        # 运行日志（gitignore）
```

> **说明**：前端采用「页面 + 组件 + 状态 + 工具函数」的经典分层，**不是 Feature-Sliced Design**。截图示例中的 FSD 目录（`app/pages/features/entities/shared`）不适用于本项目，请勿按 FSD 假设去推断文件位置。

---

## 后端架构分层

### 1. `kernel/` — 插件内核（纯装配，无业务）

| 模块 | 职责 |
|------|------|
| `services.py` | `ServiceRegistry`：按接口/名称注册实现，栈式后注册覆盖，deactivate 自动回退 |
| `eventbus.py` | `EventBus`：进程内发布订阅 |
| `hooks.py` | `HookManager`：pre/post 钩子管线 |
| `loader.py` | `PluginLoader`：插件发现 → 校验 → 加载 → activate/deactivate → unload |
| `contracts/` | 插件/服务/工具/Provider/Channel 等基础契约与 `BasePlugin` |
| `context.py` | `PluginContext`：插件运行时可访问的内核能力集合 |

### 2. `engine/` — 对话与工具执行引擎

| 模块 | 职责 |
|------|------|
| `agent_loop.py` | 核心对话循环：上下文装配 → 模型调用 → tool_calls 解析 → 执行 → 回填 |
| `tool_registry.py` | 工具注册表，支持插件动态注册/注销 |
| `permission_gate.py` | 工具 read/write/dangerous 分级与人工确认 |
| `runtime.py` | 运行时上下文与变量解析 |
| `builtin_tools.py` | 内置工具（task/subagent/memory/read_artifact/...） |
| `checkpoint.py` | 断点续跑持久化 |
| `safe_math.py` | LLM 数值计算安全护栏 |

### 3. `infra/` — 基础设施

| 模块 | 职责 |
|------|------|
| `database.py` | SQLite 初始化、连接、Schema 创建 |
| `repository.py` | 通用 Repository 封装 |
| `net_guard.py` | SSRF 防护（私有地址、非 HTTP 协议、回环等）与带上限的安全 fetch |
| `security.py` / `crypto.py` | 密钥加密、签名、敏感信息脱敏 |
| `subproc.py` | 子进程管理辅助 |
| `atomic_write.py` | 原子写文件 |

### 4. `api/` — HTTP / WebSocket 接口

- `rest/`：各业务域 REST router，每个 router 通常配 `setup_*_routes()` 注册。
- `ws/chat.py`：WebSocket 实时对话流。
- `middleware/auth.py`：可选 Bearer token 认证中间件。
- `errors.py`：统一错误响应与 SEC 脱敏要求。
- `deps.py`：`require_admin` 等依赖。

### 5. `modules/` — 业务模块（部分关键）

| 模块 | 职责 |
|------|------|
| `auth_manager/` | 可选认证：PBKDF2 口令哈希、HMAC 签名 token、用户隔离 |
| `session_manager/` | 会话与消息 CRUD、导出/导入/分叉、搜索 |
| `memory_manager/` | 长期记忆、语义向量检索、自动摘要 |
| `mcp_client/` | MCP stdio / SSE / streamable-http 客户端，工具注册 |
| `model_manager/` | Provider 注册（OpenAI 兼容）、模型列表、token 计数 |
| `sandbox_manager/` | Python/Shell 代码沙箱（本地或 Docker 后端） |
| `scheduler/` | 定时任务（每天/每周/间隔/一次性） |
| `workflows/` | 工作流编排引擎（Dify 风格画布） |
| `knowledge_base/` | 私有知识库：分段、清洗、索引、检索、技能化 |
| `evals_lab/` | 模型/提示词 A/B 评测 |
| `insights/` | 会话/消息/Token/工具调用洞察看板 |
| `reverse_engineering/` | 工程强逆向七阶段流水线 |
| `evidence_v2/` | 哈希链防篡改证据包 |
| `templates_hub/` | 模板分享中心 |
| `devkit/` | 插件开发者工作台 |
| `skill_manager/` / `package_installer/` | Skills 与市场安装 |
| `channel_manager/` | Webhook / Telegram 多渠道接入 |

---

## 前端架构

### 1. 入口与启动顺序

`main.ts` 的启动顺序：

1. 创建 Vue app + Pinia + Vue Router
2. `initBackendRuntime()`：解析后端地址（浏览器直接返回，Tauri 向壳查询）
3. `useAuthStore().init()`：认证状态初始化
4. `app.mount('#app')`
5. 后台加载并激活 UI 插件，再同步后端插件状态

### 2. 状态管理（Pinia）

| Store | 职责 |
|-------|------|
| `auth.ts` | 认证启用状态、token、登录/登出 |
| `chat.ts` | 当前会话、消息流、输入状态、发送/重试 |
| `settings.ts` | 主题、语言、后端持久化设置 |
| `providers.ts` | 模型 Provider 列表与配置 |
| `plugins.ts` / `plugin-loader.ts` | 插件列表与前端 UI 插件激活 |
| `skills.ts` | Skills 启用状态 |
| `permissions.ts` | 工具权限策略 |
| `todos.ts` | 当前会话任务清单 |

### 3. 页面视图

| 路由 | 视图 |
|------|------|
| `/` | `HomeView`（Matrix 数字雨首页） |
| `/chat` | `ChatView` |
| `/settings` | `SettingsView` |
| `/insights` | `InsightsView` |
| `/evals` | `EvalLabView` |
| `/templates` | `TemplateHubView` |
| `/workflows` / `/workflows/new` / `/workflows/:id` | `WorkflowListView` / `WorkflowView` |
| `/devkit` | `DevkitView` |
| `/knowledge` | `KnowledgeBaseView` |
| `/reverse` | `ReverseEngineeringView` |
| `/oauth/oortcloud/callback` | `OortcloudCallback` |

### 4. UI 插件机制

- `core/plugin-loader.ts` + `stores/plugin-loader.ts` 负责加载 `frontend/src/plugins/*`。
- 每个 UI 插件包含 `ui-plugin.json` 与至少一个 Vue 组件。
- `PluginOverlayHost` 提供全局悬浮层。

---

## 核心模块依赖关系

```
main.py
├── Database (infra)          ─────┐
├── ServiceRegistry (kernel)  ─────┼── 被 modules 注册使用
├── EventBus (kernel)         ─────┤
├── HookManager (kernel)      ─────┤
├── PluginLoader (kernel)     ─────┤
├── ToolRegistry (engine)      ─────┤
└── REST / WS routers (api)    ─────┘
       │
       ▼
   modules/* 业务实现
       │
       ▼
   engine/agent_loop (调用 ToolRegistry / Provider / Memory / 子代理)
```

**关键流动**（以一次对话为例）：

1. 前端 `POST /api/sessions/{id}/runs`。
2. `runs.py` → `orchestrator.run()`。
3. `AgentLoop.run()` 构建上下文，调用 Provider chat（stream）。
4. 若模型返回 `tool_calls`，`AgentLoop` 经 `permission_gate` 校验后调用 `ToolRegistry`。
5. 工具执行结果回填，循环直到终答或达到 `max_tool_iterations`。
6. 过程中通过 WebSocket / SSE 向前端推送流式 token、工具卡片、trace。

---

## 开发规范与约定

### 后端

1. **Python 版本**：>= 3.11；mypy 配置 `python_version = 3.12` 且 `strict = true`。
2. **代码风格**：ruff（E/F/I/N/W/UP），`line-length = 120`；import 顺序由 ruff 自动处理。
3. **类型注解**：公共接口必须完整标注；避免 `Any`，必要时加注释说明。
4. **异常处理**：
   - 业务错误统一抛 `harness.api.errors.APIError` 或内核 `exceptions.*`。
   - REST 错误响应必须经脱敏处理，**禁止把原始异常/堆栈/密钥直接返回**。
   - 使用日志 `logger.exception` 记录堆栈，响应中只暴露类型名/用户可读信息。
5. **资源与并发**：
   - 异步代码禁止长时间同步阻塞；DNS/文件 IO 等应走 `asyncio.to_thread`。
   - 子进程环境必须白名单化，不得透传宿主机 `os.environ`（参考 `sandbox_manager`）。
   - 后台 `asyncio.Task` 必须持有引用（用 `_bg_tasks` 集合 + `add_done_callback(discard)` 回收）。
6. **安全**：
   - 外部 URL 必须经 `net_guard.assert_url_safe` 校验。
   - 文件路径先 `Path.resolve()` 再用 `is_relative_to()` 防止穿越。
   - 密钥用 `crypto` 加密存储，日志中经 `redact_secrets` 脱敏。
7. **测试**：
   - 新功能必须带测试；测试文件放 `backend/tests/`。
   - 测试之间应隔离；避免依赖全局单例状态顺序。

### 前端

1. **框架**：Vue 3 Composition API；`<script setup lang="ts">`。
2. **状态**：Pinia；脚本中 `t.value('key')`（i18n `t` 是 `ComputedRef`）。
3. **构建门禁**：
   - 真正的类型检查是 `pnpm build`（`vue-tsc -b && vite build`）。
   - `tsconfig.json` 是 solution 文件（`files: [] + references`），单独跑 `vue-tsc --noEmit -p tsconfig.json` 不会检查应用代码。
4. **风格**：紧凑 UI、最小内边距、无装饰图标/颜色、居中文本、轻快交互、避免拖沓动效。
5. **多语言**：新字符串同时写入 `frontend/src/i18n/zh.ts` 与 `frontend/src/i18n/en.ts`。
6. **UI 改动后**：必须 `pnpm build` 通过。

### 通用

1. **README/CHANGELOG**：每次新增功能按现有风格写入 `README.md` 与 `CHANGELOG.md`（若存在）；README 醒目位置链接 `DISCLAIMER.md`。
2. **工作日志**：每次项目修改后追加到 `.workbuddy/memory/YYYY-MM-DD.md`。
3. **依赖隔离**：后端用 `uv`，前端用 `pnpm`；大文件/框架优先落到项目目录（`F:`/`H:` 盘），避免 `C:` 空间不足。

---

## 关键文件速查

| 用途 | 文件 |
|------|------|
| 后端入口 | `backend/harness/main.py` |
| 后端配置 | `backend/pyproject.toml` |
| 数据库 Schema | `backend/harness/infra/database.py` |
| 服务注册 | `backend/harness/kernel/services.py` |
| 插件加载器 | `backend/harness/kernel/loader.py` |
| 对话循环 | `backend/harness/engine/agent_loop.py` |
| REST 错误/脱敏 | `backend/harness/api/errors.py` |
| SSRF 护栏 | `backend/harness/infra/net_guard.py` |
| 认证中间件 | `backend/harness/api/middleware/auth.py` |
| 前端入口 | `frontend/src/main.ts` |
| 前端路由 | `frontend/src/router/index.ts` |
| API client / runtime | `frontend/src/api/client.ts`、`frontend/src/api/runtime.ts` |
| Tauri 壳 | `desktop/src-tauri/tauri.conf.json`、`desktop/launcher/launch-backend.py` |
| 一键启动（浏览器） | `start-all.bat` |
| 一键启动（桌面） | `start-desktop.bat` |
| 项目说明 | `README.md` |

---

## 后续维护与扩展注意事项

### 新增业务模块

1. 在 `backend/harness/modules/` 新建包，提供业务实现。
2. 在 `backend/harness/api/rest/` 新增 router，并在 `main.py` 的 `create_app` 中注册。
3. 若模块是插件化能力，按 `kernel/contracts/base.py` 的 `BasePlugin` 实现并接入 `PluginLoader`。
4. 补充 `backend/tests/test_xxx.py` 覆盖主要路径。

### 新增前端页面

1. 在 `frontend/src/views/` 新增 `.vue`。
2. 在 `frontend/src/router/index.ts` 注册路由。
3. 若需要新状态，在 `frontend/src/stores/` 新建 Pinia store。
4. 若需要调用新 REST 接口，扩展 `frontend/src/api/client.ts`。
5. 更新 `frontend/src/i18n/zh.ts` 与 `frontend/src/i18n/en.ts`。

### 修改数据库 Schema

- `infra/database.py` 中追加 `CREATE TABLE IF NOT EXISTS` 语句，应用会在首次启动时自动升级。
- 若涉及既有表结构迁移，需考虑老数据兼容；必要时写一次性迁移脚本。

### 安全红线

- **禁止**在 API 响应中直接返回未脱敏的异常/密钥/堆栈。
- **禁止**在子进程中透传完整 `os.environ`。
- **禁止**未校验的 URL 直接进入 `urlopen` / `httpx` 请求。
- **禁止**未校验的文件名直接拼接到磁盘路径。

### 性能与稳定

- 大输出使用 `artifact_store` 落盘，不要全部塞进响应。
- SSE / WebSocket 推送保持非阻塞；后台任务持有引用防止 GC。
- 逆向/评测/工作流等长耗时操作必须实时 emit 进度事件。

### 已知限制

- 桌面壳、渠道管理、断点续跑、Computer Use 等部分功能**后端已实现但前端暂无入口**。
- 飞书 Channel 入站需要额外安装 `lark-oapi`（`--extra feishu`）。
- REA 原生反编译依赖项目内 Ghidra + JDK，Firecracker/PentAGI 在本机不可用时自动降级。
- 当前仅在 Windows 10 + DeepSeek `deepseek-v4-flash` 下做了深度验证。

---

*本文档与 `README.md` 互补：AGENTS.md 面向源码结构与开发协作，README.md 面向功能说明与使用指南。修改项目结构或新增约定时，请同步更新本文档。*
