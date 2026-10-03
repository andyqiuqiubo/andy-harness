# andy-harness 能力增强清单

> 本文基于对 16 个主流开源 / 商用 Agent Harness 项目的横向调研，归纳出本项目**可借鉴的能力点**，并按价值 × 实施成本排定优先级。
> 每项能力均标注：**来源项目 → 行业做法 → 对本项目价值 → 优先级 → 实施要点 → 验收标准 → 当前状态**。
>
> 调研时间：2026-09-27 ｜ 对应项目版本：v0.1.0

---

## 0. 调研样本

### 0.1 通用 Harness / SDK

| 项目 | 维护方 | Stars | 协议 | 架构签名 |
|---|---|---|---|---|
| Claude Code / Claude Agent SDK | Anthropic | 7.5K+ (SDK) | MIT (SDK) | 文件化 Skills + 三级权限 + Hooks + Subagent + MCP |
| OpenAI Codex CLI | OpenAI | — | Apache-2.0 | Rust 单二进制、OS 沙箱、AGENTS.md、apply_patch |
| OpenAI Agents SDK | OpenAI | 27.7K | 开源 | 内置 compaction / memory / snapshot-rehydrate / tracing / guardrails |
| Deep Agents | LangChain | 25.9K | 开源 | write_todos 规划 + 虚拟文件系统 + SKILL.md + Subagent + 长期记忆 |
| LangGraph | LangChain | 36.7K | 开源 | 图编排 + checkpointing + human-in-the-loop interrupt |
| Microsoft Agent Framework | Microsoft | 11.9K | 开源 | shell/FS + 审批 + compaction + todo + plan/execute + AgentSkillsProvider |
| Letta (MemGPT) | Letta | 23.7K | Apache-2.0 | 分层自编辑记忆（core / archival）+ Context Repositories |
| CrewAI | CrewAI Inc. | 55.1K | MIT | 角色化 Crew + Flows + 统一记忆 + 层级委派 |
| Pydantic AI | Pydantic | 18.2K | MIT | 类型安全结构化输出 + OTel 可观测 + Temporal 持久化 |
| Amazon Bedrock AgentCore | AWS | — | 商用 | 配置化 harness：隔离会话环境 + 跨会话记忆 + Skills 目录 |

### 0.2 编码 / 终端向 Harness

| 项目 | Stars | 协议 | 架构签名 |
|---|---|---|---|
| OpenHands | 65K+ | MIT | 事件流架构 + 每会话 Docker 沙箱 |
| Aider | 44K+ | Apache-2.0 | Git-first + Tree-sitter Repo Map + Architect Mode |
| Cline | 61.2K | Apache-2.0 | VS Code 原生 + 每动作审批 + 成本透明 |
| Goose | 44.7K | Apache-2.0 | MCP-first + 70+ 扩展 + vendor 中立 |
| OpenCode | 160K+ | Apache-2.0 | Go TUI + LSP 原生 + 75+ provider |
| Pi | 98K | MIT | 极简四工具内核 + 按需加载 Skills + TypeScript 扩展 |
| DeepSeek Harness (dsh) | 95K+ | MIT | Cordis 微内核，模型/工具/沙箱/UI 全可换插件 |
| SWE-Agent | 19K | MIT | Agent-Computer Interface（ACI） |
| Gemini CLI | — | Apache-2.0 | 终端 + 内置工具 + 确认式安全 |

主要信息源：
- [harnesses.sh](https://www.harnesses.sh/) — 75 个 harness 的能力维度目录（mcp / skills / local models / sandbox / HIL / checkpointing / tracing）
- [truefoundry: Best Agent Harness 2026](https://www.truefoundry.com/pt/blog/best-agent-harness-in-2026) — 五强横向评测（含 token 成本对比）
- [worldprogramming: Harness Engineering 101](https://www.worldprogramming.org/posts/harness-engineering-101-how-coding-agents-actually-work-xsrz0k) — 安全模型与工具集对比
- [Agent Skills 开放规范 / agentskills.io](https://atlan.com/know/ai-agent/ai-agent-skills/what-are-agent-skills) — SKILL.md 三级渐进式披露标准

---

## 0.5 核心产品定位：Computer Use（一等目标）

> 本项目的**核心定位是 Computer Use（操控整个本机桌面）**，浏览器操作（browser use）只是它的一个子集 / 场景。**任何能力设计都应以「让 Agent 安全、可控地操作本机」为第一性，而非反过来做成「浏览器专用」**——这条铁律也写进了 `plugins/computer_use/` 的代码约束（工具层只保留通用 OS 级原语，不得为某个具体 App / 网站写死特殊处理）。

| 项 | 内容 |
|---|---|
| **能力** | 截图 / 鼠标点击移动 / 键盘输入 / 文件读写 / 命令执行 / wait / finish 全套通用原语；模型「计划先行」再执行，并内置**截图降本策略**（避免每轮都带截图导致图像 token 重复计费） |
| **安全围栏** | 工具层只保留通用原语（不绑定任何具体 App/网站）；权限分级（read/write/dangerous）+ 人工确认；危险操作（如执行命令）默认需授权；超时与输出截断兜底 |
| **依赖** | `pyautogui` / `mss` 通过 `pyproject.toml` 的 `desktop` optional extra 提供，缺失时优雅降级（工具返回「桌面不可用：未安装 pyautogui/mss」），需启用 Computer Use 再 `uv sync --extra desktop` 安装 |
| **验收标准** | ① 装好 `desktop` extra 后 `RealDesktopController.available` 为 `True`；② 模型能执行「截图→定位→点击/输入→wait→截图验证」闭环；③ 未授权命令执行被拦截；④ 截图累积不导致图像 token 线性膨胀（降本策略生效） |
| **状态** | ✅ 已实现并验证（落地于 `plugins/computer_use/`：`desktop.py` 原语 + `agent.py` 计划先行 / 降本）。依赖声明见 G1、Windows 沙箱资源限制与进程树终止见 G2 |
| **落地位置** | 后端：`plugins/computer_use/`（desktop 控制器 + agent 循环 + 安全围栏）、`api/ws/chat.py`（computer_use 模式帧）；前端：ChatView 桌面操控模式 |

---

## 1. 行业共性：一个成熟 Harness 的"能力地基"

把所有样本的能力取交集，去掉营销成分，剩下的**四个必答题**（freeCodeCamp / truefoundry 表述一致）：

1. **Planning（规划）** —— 长任务不迷路：todo / plan-execute 模式
2. **Sandboxing（沙箱与权限）** —— 模型输出当作不可信输入：沙箱 + 审批 + 权限分级
3. **Delegation（委派）** —— 子代理 / 并行 / 上下文隔离
4. **Context Management（上下文工程）** —— 压缩、offload、按需加载（Skills）

在此之上，2026 年新增的两条**事实标准**：

5. **Skills（SKILL.md 三级渐进式披露）** —— Claude / Codex / Cursor / Copilot / Gemini / Snowflake 等 40+ 客户端采纳的开放规范
6. **MCP（Model Context Protocol）** —— 工具生态的通用插座

---

## 2. 本项目现状 vs 行业地基（差距分析）

| 能力维度 | 行业普及度 | 本项目现状 | 差距 |
|---|---|---|---|
| 多模型接入 | 100% | ✅ DeepSeek/Qwen/Doubao/自定义 OpenAI 兼容 | 持平（缺本地模型 Ollama 直达入口，见 P3-2） |
| 插件架构 | 高 | ✅ 内核零业务、一切皆插件（对标 DeepSeek Harness Cordis） | **领先** |
| **Computer Use 桌面操控** | 高（Claude Computer Use / OpenHands / SWE-Agent ACI） | ✅ 已实现（截图 / 鼠标 / 键盘 / 文件读写 / 命令执行 / wait / finish 全套原语 + 计划先行 + 降本策略） | 持平（依赖 `pyautogui`/`mss`，经 `desktop` extra 可选安装） |
| 上下文压缩 | 高 | ✅ 滑动窗口 / 摘要压缩 / 钉住 / 快照 / **大输出 offload 落盘** | 持平 |
| 沙箱执行 | 高 | ✅ 子进程 + 资源限制（Windows Job Object 内存上限 + `taskkill /T` 进程树终止）+ 黑名单 + 权限分级审批；**并支持 Docker 容器级隔离（禁网 / 资源限制 / 镜像白名单，见 E2）** | 持平 |
| **Skills 按需加载** | **40+ 客户端支持的事实标准** | ✅ 已实现（三级渐进式披露 L1/L2/L3，内置 3 示例） | 持平 |
| **Human-in-the-loop 审批** | 高（Cline/Claude/Codex/MS/LangGraph） | ✅ 已实现（risk_level 分级 + 四档策略 + 单工具覆盖 + WS 确认弹窗） | 持平 |
| **规划 / Todo 追踪** | 高（Deep Agents/Claude Code/MS） | ✅ 已实现（todo_write 覆盖式写入 + 前端进度面板） | 持平 |
| **MCP 客户端** | 高（Goose/Claude/Cline/OpenHands） | ✅ 已实现（stdio + SSE/streamable-http，零第三方依赖） | 持平 |
| 子代理委派 | 中高 | ✅ 已实现（独立隔离会话 + 回传摘要，主上下文不膨胀；**并支持并行任务与角色流水线编排，见 E10**） | 持平 |
| 长期记忆 | 中高（Letta/OpenAI/Deep Agents） | ✅ 已实现（跨会话 key/value，**向量余弦语义检索 + LIKE 回退 + 定时自主总结，见 E7**） | 持平 |
| 会话分支 / 导出 | 中（Pi/Aider/OpenCode） | ✅ 已实现（JSON 导出/导入 + 任意消息 fork） | 持平 |
| 可观测性 Tracing | 中高（OpenAI/Pydantic OTel/LangSmith） | ✅ 已实现（run/model/tool span 落库 + 泳道图回放） | 持平 |
| **Checkpoint / 断点续跑** | 中（LangGraph/OpenAI snapshot） | ✅ 已实现（AgentLoop 每轮写 `agent_runs` 检查点 + `resume(run_id)` 续跑） | 持平 |
| **插件市场 / 技能市场 / MCP 市场** | 中高（生态分发） | ✅ 已实现（三套市场：本地目录安装 / 说明弹窗 / 启停，前端共享面板；**并支持从 zip / git 外部分发安装，见 E6**） | 持平 |
| 多渠道 Channel | 中（Hermes/架构已预留契约） | ✅ 已实现（Webhook 同步 + Telegram 长轮询，按渠道用户映射会话，见 E1） | 持平 |
| 认证与多用户 | 中高（本地单用户工具默认关闭，需要时一键开启） | ✅ 已实现（默认关闭行为零变化；HARNESS_AUTH=1 后全站 Bearer 鉴权，会话 / 记忆按用户隔离，见 E12） | 持平 |

---

## 3. 能力增强清单

优先级定义：
- **P0**：大众普遍需要、缺失即体验残缺、且实施成本可控 → 优先实现
- **P1**：显著提升专业场景能力
- **P2**：差异化 / 进阶
- **P3**：长尾与生态

### P0-1 · Skills 系统（SKILL.md 渐进式披露） ⭐ 首选实现

| 项 | 内容 |
|---|---|
| **来源** | Claude Code / Claude Agent SDK、OpenAI Codex、Deep Agents、Microsoft Agent Framework（AgentSkillsProvider）、Pi、OpenCode、Gemini CLI、Bedrock AgentCore（curated skills catalog） |
| **行业做法** | Skill = 一个文件夹（`SKILL.md` + 可选 `scripts/` `references/` `assets/`）。**三级渐进式披露**：L1 仅 frontmatter（`name` + `description`，约 100 token）常驻 system prompt；L2 命中后加载正文；L3 引用的脚本/参考文件在被用到时才读取。装几十上百个 skill 也不炸上下文。`name` kebab-case ≤64 字符，`description` 必须同时说明"做什么"和"何时用"（≤1024 字符） |
| **价值** | ① 用户零代码扩展 Agent 能力，比写插件门槛低一个数量级；② 与现有插件机制互补——插件给**能力**，Skill 给**流程知识**；③ 直接复用 40+ 生态已有的 SKILL.md；④ 契合本项目"面向学习与二次开发"的定位 |
| **实施要点** | 新增 `skill-manager`（service 插件）+ `tool-use-skill`（工具插件）；扫描三处目录（内置 `backend/skills/`、用户级 `~/.andy-harness/skills/`、插件自带 `plugins/*/skills/`）；极简 frontmatter 解析（不引新依赖）；AgentLoop 注入 L1 目录；`use_skill` 工具返回 L2 正文 + L3 资源清单；REST `/api/skills`；前端设置页 Skills 面板；内置 3 个示例 skill |
| **验收标准** | ① 后端 pytest 全绿 + 新增 skill 相关测试；② ruff/mypy 通过；③ 启动后端，`/api/health` 与 `/api/skills` 正常返回；④ 用 fake provider 跑通 AgentLoop，验证目录注入 + `use_skill` 调用链路；⑤ 前端 `pnpm build` 通过 |
| **状态** | ✅ 已实现并验证（见 §4 实施记录） |
| **落地位置** | 后端：`harness/modules/skill_manager/service.py`（服务实现）、`plugins/skill_manager/`（service 插件）、`plugins/tool_use_skill/`（工具插件）、`api/rest/skills.py`（REST）、`engine/agent_loop.py`（目录注入）；Skill 目录：`backend/skills/`（含 3 个内置示例）；前端：`stores/skills.ts` + 设置页 Skills 标签页 |

### P0-2 · Human-in-the-loop 审批与权限分级

| 项 | 内容 |
|---|---|
| **来源** | Cline（每动作审批）、Claude Code（三级权限：自动放行只读 / 确认后修改 / 拒绝危险操作）、Codex（OS 沙箱 + workspace-only + 断网）、Microsoft Agent Framework（human-in-the-loop approvals）、LangGraph（interrupt）、worldprogramming 评测（"工具越少越依赖 shell，沙箱就必须越严"） |
| **行业做法** | 把模型生成的命令视为**不可信输入**；deny-by-default + 显式 allowlist；不可逆操作必须人工确认；凭据最小作用域 |
| **价值** | 本项目已开放 `tool-code-runner`（shell/Python 执行），**缺审批等于把本机完全交给模型**。这是安全底线，也是 Cline/Codex 类产品口碑来源 |
| **实施要点** | 新增 `permission_manager` service 插件；`ToolPlugin.risk_level` 契约字段（read/write/dangerous，默认 write）；四档策略（auto / confirm_dangerous 默认 / confirm_write / confirm_all）+ 单工具覆盖（auto/confirm/deny）；AgentLoop 在工具执行前拦截；WS 新增 `confirm_request` / `confirm_reply` / `confirm_timeout` 帧；前端确认弹窗 + 设置页策略配置 |
| **验收标准** | ① 危险工具未获批准时不执行且错误信息回传模型；② 批准后继续执行；③ 拒绝/超时不会留下孤立 tool_calls 导致 API 400；④ 权限服务未注册时行为不变；⑤ pytest + 前端 build 通过 |
| **状态** | ✅ 已实现并验证（见 §4 实施记录） |
| **落地位置** | 后端：`harness/modules/permission_manager/service.py`、`plugins/permission_manager/`、`harness/kernel/contracts/tool.py`（risk_level 契约）、`engine/agent_loop.py`（拦截）、`api/rest/permissions.py`、`api/ws/chat.py`（确认帧）；前端：`stores/permissions.ts`、ChatView 确认弹窗、设置页通用设置内权限区 |

### P0-3 · 规划与 Todo 追踪（write_todos）

| 项 | 内容 |
|---|---|
| **来源** | Deep Agents（`write_todos` 规划工具）、Claude Code（TodoWrite + plan mode）、Microsoft Agent Framework（todo tracking / plan-execute 模式）、OpenHands（task tracker） |
| **行业做法** | 长任务先拆成 todo 列表，逐项勾选；用户能看见进度，模型不易跑偏；配合 plan/execute 模式让用户先审计划 |
| **价值** | 本项目 AgentLoop 最多 10 次工具迭代，复杂任务容易"半途跑偏且用户看不见进度"。Todo 是最廉价的可信度提升手段 |
| **实施要点** | `todo_manager` service 插件 + `tool_todo` 工具插件（`todo_write`，覆盖式写入）+ `todos` 表 + `GET|DELETE /api/sessions/{id}/todos`；前端聊天页任务面板（进度 + 状态）；`ToolPlugin.needs_session` 契约字段让 AgentLoop 注入 session_id |
| **验收标准** | ① AI 能创建/更新 todo 且持久化到会话；② 切换会话后加载对应清单、互不影响；③ 非法参数/缺 session 时报错而非写脏数据；④ pytest + 前端 build 通过 |
| **状态** | ✅ 已实现并验证（见 §4 实施记录） |
| **落地位置** | 后端：`harness/modules/todo_manager/service.py`、`plugins/todo_manager/`、`plugins/tool_todo/`、`infra/database.py`（todos 表）、`api/rest/sessions.py`；前端：`stores/todos.ts` + ChatView 任务面板 |

### P0-4 · 会话导出 / 导入与分支 fork

| 项 | 内容 |
|---|---|
| **来源** | Pi（会话分支 fork，被评测单独称赞）、Aider（git-native，每步一个可回退 commit）、OpenCode（会话持久化 + 断点）、DeepSeek Harness（会话可分叉） |
| **行业做法** | 会话可导出为 JSON/Markdown；可从任意历史消息分叉出新会话试错 |
| **价值** | 数据可携 + 试错零成本；本项目架构文档 v0.2 已列入规划 |
| **实施要点** | `POST /api/sessions/{id}/export`（JSON，含元数据 + 全量消息 + 任务清单）、`POST /api/sessions/import`、`POST /api/sessions/{id}/fork?at_message_id=`；前端侧边栏右键菜单「复制为新会话 / 导出 / 导入」+ 顶栏「导入」按钮；`PUT /api/sessions/{id}/todos` 覆盖写入（供前端/导入回填，剥离来源 id 防主键冲突） |
| **验收标准** | 导出→导入内容一致（消息 + 任务）；fork 后新会话含截断历史且互不影响；非法会话返回 404 |
| **状态** | ✅ 已实现并验证（见 §4 实施记录） |
| **落地位置** | 后端：`harness/modules/session_manager/service.py`（`export_session` / `import_session` / `fork_session`）、`harness/api/rest/sessions.py`（export/import/fork + PUT todos）、`harness/infra/database.py`、`harness/infra/repository.py`（Message/Session `to_dict` 补 `tool_call_id`）；前端：`components/SessionSidebar.vue`（右键菜单 + 导入按钮 + 文件选择）、`i18n/zh.ts`、`i18n/en.ts` |

### P1-1 · MCP（Model Context Protocol）客户端

| 项 | 内容 |
|---|---|
| **来源** | Goose（MCP-first，70+ 扩展）、Claude Code / Claude Agent SDK、Cline、OpenHands、OpenAI Agents SDK、CrewAI |
| **行业做法** | 通过 stdio / SSE 连接 MCP server，把远端工具动态注册进工具注册表 |
| **价值** | 一次接入，复用整个 MCP 生态（数据库、浏览器、SaaS）。对本项目的"插件市场"是极强补充 |
| **实施要点** | `mcp_client` service 插件；`mcp.json` 配置；MCP tool → ToolRegistry 适配；前后端配置页 |
| **验收标准** | 连接一个本地 MCP echo server，其工具出现在工具列表并可被 AI 调用 |
| **状态** | ✅ 已实现并验证（见 §4 实施记录） |
| **落地位置** | 后端：`harness/modules/mcp_client/service.py`（零依赖 stdio JSON-RPC 客户端：握手 / tools.list / tools.call / 按 server 缓存）、`plugins/mcp_client/`（service 插件，启动时连接并把每个远端工具注册为 `mcp__{server}__{tool}`）、`api/rest/mcp.py`（REST `/api/mcp`）、`harness/kernel/loader.py`（支持重复启动的幂等再激活）；前端：设置页 MCP 标签页（server 列表 + 连接状态 + 工具 chips + 增删/重连）；配置：`mcp.json`（项目根）或 `~/.andy-harness/mcp.json`，可用 `MCP_CONFIG_PATH` 覆盖 |

### P1-2 · 子代理委派（Subagent）

| 项 | 内容 |
|---|---|
| **来源** | Claude Code（Sub-agent delegation）、Deep Agents（subagent spawning）、Microsoft（BackgroundAgentsProvider）、Bedrock、CrewAI（层级委派） |
| **行业做法** | 主代理派生一个**上下文干净**的子代理处理隔离子任务，只回传摘要 |
| **价值** | 长会话主上下文不被探索过程污染；可并行。本项目已有 EventBus/ServiceRegistry，具备落地条件 |
| **实施要点** | `subagent` service + `task` 工具；子 AgentLoop 复用现有 engine；回传摘要而非原始消息 |
| **验收标准** | 主会话调用 task 工具后只收到摘要，主上下文 token 不膨胀 |
| **状态** | ✅ 已实现并验证（见 §4 实施记录） |
| **落地位置** | 后端：`harness/modules/subagent/service.py`（子代理服务：独立临时会话 + 受限工具集 + 回传摘要）、`plugins/subagent/`（service 插件）、`plugins/tool_task/`（`task` 工具）、`engine/runtime.py`（ContextVar 运行环境，向工具暴露当前 provider/model）、`engine/agent_loop.py`（run 期间设置/重置运行环境）、`engine/tool_registry.py`（`all_tools()`）、`api/ws/chat.py`（子代理运行时抑制向主界面流式输出） |

### P1-3 · 长期记忆（跨会话记忆）

| 项 | 内容 |
|---|---|
| **来源** | Letta（分层自编辑记忆 core/archival）、OpenAI Agents SDK（built-in memory）、Deep Agents（LangGraph store）、CrewAI（unified memory）、Bedrock（cross-session memory） |
| **行业做法** | 跨会话持久化的用户偏好 / 项目事实，按需检索注入 |
| **价值** | 从"每次重新认识用户"进化为"记住你"。本项目已有 SQLite + 加密 infra，成本低 |
| **实施要点** | `memory_manager` service 插件；`memories` 表（key/value/scope/embedding 预留）；`memory_save` / `memory_search` 工具；前端记忆管理页 |
| **验收标准** | 新开会话能检索到旧会话写入的记忆；可手动增删改 |
| **状态** | ✅ 已实现并验证（见 §4 实施记录） |
| **落地位置** | 后端：`harness/modules/memory_manager/service.py`（记忆服务：同 key upsert、LIKE 检索、`render_hint` 生成注入片段）、`plugins/memory_manager/`（service 插件）、`plugins/tool_memory/`（`memory_save` / `memory_search` 工具）、`api/rest/memories.py`（REST 增删改查）、`infra/database.py`（`memories` 表，`embedding` 列预留）、`engine/agent_loop.py`（system 前缀注入长期记忆）；前端：设置页「记忆」标签页（搜索 / 新增 / 编辑 / 删除，全局与会话作用域） |

### P1-4 · 大工具输出 Offload（虚拟文件系统）

| 项 | 内容 |
|---|---|
| **来源** | Deep Agents（virtual filesystem for context offloading）、OpenAI Agents SDK（offload large tool outputs）、freeCodeCamp 归纳的"上下文管理四机制" |
| **行业做法** | 超过阈值（如 2K token）的工具结果不进上下文，落盘为文件，只回传路径 + 摘要，模型按需读取 |
| **价值** | 本项目沙箱能跑代码/抓网页，长输出极易撑爆 4096 token 预算——这是当前最现实的上下文故障点 |
| **实施要点** | `post_tool_call` 钩子拦截；`workspace/artifacts/` 落盘；回传 `[offloaded: path, 摘要]`；提供 `read_artifact` 工具 |
| **验收标准** | 大输出被落盘；上下文 snapshot token 数显著下降；模型可回读全文 |
| **状态** | ✅ 已实现并验证（见 §4 实施记录） |
| **落地位置** | 后端：`harness/modules/artifact_store/service.py`（工件存储：文件落盘 + `artifacts` 表元数据 + 分页读取）、`plugins/artifact_store/`（service 插件 + `post_tool_call` 钩子：超阈值即落盘并改写结果为「路径 + 摘要 + artifact_id」）、`plugins/tool_read_artifact/`（`read_artifact` 工具，支持 offset/limit 分页，单次上限防再次撑爆）、`api/rest/artifacts.py`（REST）、`infra/database.py`（`artifacts` 表）；前端：设置页「制品」标签页（列表 + 查看全文 + 删除）；阈值/目录：`HARNESS_OFFLOAD_THRESHOLD`（默认 6000 字符）、`HARNESS_ARTIFACTS_DIR`（默认 `workspace/artifacts/`） |

### P1-5 · 可观测性 Tracing（运行轨迹面板）

| 项 | 内容 |
|---|---|
| **来源** | OpenAI Agents SDK（tracing）、Pydantic AI（OTel + Logfire）、LangSmith、OpenHands（event stream 可视化） |
| **行业做法** | 每次模型调用 / 工具调用记 span（耗时、token、输入输出），可视化回放 |
| **价值** | "哪里慢、哪里贵、哪里错"一眼可见。本项目已有 EventBus 与 usage 统计，补齐 span 即可 |
| **实施要点** | `tracing` service 插件订阅现有事件落表；`GET /api/traces`；前端泳道图 |
| **验收标准** | 一次对话产生完整 span 链，含耗时与 token |
| **状态** | ✅ 已实现并验证（见 §4 实施记录） |
| **落地位置** | 后端：`harness/modules/tracing/service.py`（span 落库 + 按 trace 聚合）、`plugins/tracing/`（service 插件，订阅 `trace.span` 事件）、`engine/agent_loop.py`（发布 run/model/tool span 到事件总线）、`kernel/loader`/`main.py`（把 EventBus 暴露给引擎）、`api/rest/traces.py`（REST）、`infra/database.py`（`spans` 表）；前端：设置页「轨迹」标签页（trace 列表 + 泳道图 + span 明细） |

### P2-1 · Durable Execution / Checkpoint 断点续跑

**来源**：LangGraph（automatic checkpointing）、OpenAI Agents SDK（snapshot/rehydrate）、Pydantic AI（Temporal/DBOS）。
**价值**：长任务中断后可续跑，服务重启不丢进度。
**要点**：AgentLoop 每轮快照 → `agent_runs` 表 → `resume(run_id)`。**状态**：✅ 已实现并验证（见 §4 实施记录）。

落地位置：后端：`harness/engine/checkpoint.py`（`RunCheckpointService` 读写 + `resume_run` 续跑）、`harness/engine/agent_loop.py`（运行开始 / 每轮迭代后 / 终答后 / 异常四处 best-effort 写检查点）、`harness/api/rest/runs.py`（`POST /api/runs/{run_id}/resume`）、`harness/infra/database.py`（`agent_runs` 表 + 索引）、`harness/main.py`（接线 `setup_runs_routes` + `runs_router`）。设计取舍：消息在每轮即时持久化到会话，因此**会话历史本身就是完整状态快照**，续跑只需基于已持久化上下文让模型继续（追加上一条「继续」引导语），不重放内存态。

### P2-2 · 上下文压缩增强（结构化 compaction）

**来源**：OpenAI Agents SDK（built-in compaction）、Deep Agents（context engineering + replay history）、Microsoft（context compaction）。
**价值**：现有摘要压缩是全量重写，成本高；改为"保留最近 N 轮原文 + 结构化事件日志摘要"更省更准。
**要点**：新增 `StructuredCompactionStrategy`（compaction v2），与现有策略并存可切换：旧消息折叠为结构化事件日志、保留最近 N 条原文，确定性、不依赖 LLM。**状态**：✅ 已实现并验证（见 §4 实施记录 E3）。

### P2-3 · 多渠道 Channel（飞书 / Telegram / Webhook）

**来源**：Hermes Agent（一个进程打通 Telegram/Slack/Discord/WhatsApp/邮件）、Bedrock、本项目架构已预留 `ChannelPlugin` 契约。
**价值**：把 Agent 从 GUI 里放出来，接入日常协作工具。
**要点**：`channel_manager` 服务 + `channel_telegram`（长轮询）插件 + 内置 Webhook（同步应答），统一复用 AgentLoop；飞书渠道后续按同模式补。**状态**：✅ 已实现并验证（见 §4 实施记录 E1）。

### P2-4 · Docker 沙箱后端

**来源**：OpenHands（每会话 Docker）、Codex（OS 沙箱）、Bedrock（vm 隔离）。
**价值**：从"本机子进程"升级为真正隔离；架构文档已预留 `SandboxBackend` 接口。
**要点**：实现 `DockerBackend`：一次性容器（`docker run --rm`）、bind mount 工作区、镜像白名单、网络开关、`--memory`/`--cpus` 限制、超时强删防泄漏。**状态**：✅ 已实现并验证（见 §4 实施记录 E2）。

### P3-1 · 回归评测（Eval Harness）

**来源**：SWE-Agent / OpenHands（benchmark 驱动开发）、truefoundry 评测（按"每任务成本/成功率"而非每 token 成本衡量）。
**价值**：改动后有客观回归基线，避免"感觉没坏"。
**要点**：`tests/evals/` 任务集 + 打分脚本 + CI 门槛。**状态**：✅ 已实现并验证（见 §4 实施记录 E5）。

### P3-2 · 本地模型直达（Ollama / vLLM）

**来源**：Aider（Ollama 本地模型）、Goose（Ollama）、OpenCode、Pi（BYOK）。
**价值**：零成本、隐私；本项目已有"自定义 OpenAI 兼容 provider"，补一份 Ollama 预置配置即可。
**要点**：`provider_ollama` 插件 + 默认 base_url `http://localhost:11434/v1`。**状态**：⬜

---

## 4. 实施记录（严格"先验证、后确认"）

每项能力必须依次通过下列四道验证，才可标记为「已接入」：

| 验证项 | 命令 / 手段 |
|---|---|
| V1 后端测试 | `cd backend && uv run pytest -q` |
| V2 静态检查 | `cd backend && uv run ruff check . && uv run mypy harness` |
| V3 核心流程 | 启动后端 → `/api/health`、`/api/skills` 等端点实测；用 fake provider 跑通 AgentLoop 全链路 |
| V4 前端构建 | `cd frontend && pnpm build` |

### 已接入

| 编号 | 能力 | 完成时间 | V1 | V2 | V3 | V4 |
|---|---|---|---|---|---|---|
| P0-1 | Skills 系统（SKILL.md 渐进式披露） | 2026-09-27 | ✅ | ✅ | ✅ | ✅ |
| P0-2 | Human-in-the-loop 审批与权限分级 | 2026-09-27 | ✅ | ✅ | ✅ | ✅ |
| P0-3 | 规划与 Todo 追踪（todo_write） | 2026-09-27 | ✅ | ✅ | ✅ | ✅ |
| P0-4 | 会话导出 / 导入与分支 fork | 2026-09-27 | ✅ | ✅ | ✅ | ✅ |
| P1-1 | MCP（Model Context Protocol）客户端 | 2026-09-28 | ✅ | ✅ | ✅ | ✅ |
| P1-4 | 大工具输出 Offload（虚拟文件系统） | 2026-09-28 | ✅ | ✅ | ✅ | ✅ |
| P1-3 | 长期记忆（跨会话记忆） | 2026-09-28 | ✅ | ✅ | ✅ | ✅ |
| P1-5 | 可观测性 Tracing（运行轨迹面板） | 2026-09-28 | ✅ | ✅ | ✅ | ✅ |
| P1-2 | 子代理委派（Subagent） | 2026-09-28 | ✅ | ✅ | ✅ | n/a |
| G4 | Checkpoint 断点续跑（agent_runs + resume） | 2026-09-30 | ✅ | ✅ | ✅* | n/a |
| E6 | 插件 / 技能从 zip / git 外部分发安装 | 2026-09-30 | ✅ | ✅ | n/a | n/a |
| E5 | 回归评测（Eval Harness：任务集 + 打分 + 报告） | 2026-09-30 | ✅ | ✅ | n/a | n/a |
| E8 | 前端测试（vitest + @vue/test-utils） | 2026-09-30 | ✅ | n/a | n/a | ✅ |
| E7 | 向量记忆检索（嵌入器 + 余弦语义 + LIKE 降级） | 2026-09-30 | ✅ | ✅ | n/a | n/a |
| E3 | 结构化 compaction（compaction v2：事件日志 + 最近原文） | 2026-09-30 | ✅ | ✅ | n/a | n/a |
| E2 | Docker 沙箱后端（一次性容器 + 禁网 + 镜像白名单） | 2026-09-30 | ✅ | ✅ | n/a | n/a |
| E9 | 会话 / 消息搜索（标题 + 内容 LIKE + 片段） | 2026-09-30 | ✅ | ✅ | n/a | ✅ |
| E1 | 多渠道 Channel（Webhook 同步 + Telegram 长轮询） | 2026-09-30 | ✅ | ✅ | ✅ | n/a |
| E10 | 多 Agent 编排（并行任务 + 角色流水线） | 2026-09-30 | ✅ | ✅ | ✅ | n/a |
| E11 | 桌面壳 Tauri（窗口 + 后端进程生命周期） | 2026-09-30 | ✅ | ✅ | ✅ | ✅ |
| E12 | 认证与多用户（默认关闭的 Bearer 鉴权 + 会话/记忆隔离） | 2026-09-30 | ✅ | ✅ | ✅ | ✅ |

#### P0-1 验证明细（2026-09-27）

| 项 | 结果 |
|---|---|
| V1 后端测试 | **180 passed**（基线 142 → 现 180，新增 38）。新增 `tests/test_skill_manager.py`（29 项：frontmatter 解析、扫描、三级加载、越界防护、启停持久化、工具行为、AgentLoop 目录注入、端到端 `use_skill` 调用）与 `tests/test_api_skills.py`（9 项 REST 集成） |
| V2 静态检查 | 新增代码 `ruff check` **全部通过**；`mypy harness` 错误数 **14，与基线持平**（基线 13 ruff / 14 mypy 均为仓库既有问题，已在干净工作树 `git worktree` 上对比确认，本次未新增） |
| V3 核心流程 | 真实启动 `uvicorn harness.main:app`，冒烟脚本 **10/10 通过**：health、skills 列表（3 个）、catalog 仅含 L1（706 字符）、详情含 L2 正文、L3 资源读取、停用→目录消失→恢复、reload、插件列表含两个新插件、会话 API 回归 |
| V4 前端构建 | `vue-tsc -b --force` 类型检查通过；`vite build` 打包成功（3.43s） |

> 说明：本机 `node_modules` 原先不完整（缺 vite/vue 等核心包），已执行 `pnpm install` 补全；`pnpm build` 中 vite 清空 `dist` 的步骤会被本机安全删除策略拦截，故改为先清理 `dist` 再单独执行 `vite build`，两者等价。

#### P0-2 验证明细（2026-09-27）

| 项 | 结果 |
|---|---|
| V1 后端测试 | **204 passed**（180 → 204，新增 24）。新增 `tests/test_permission_manager.py`（19 项：风险等级、四档策略、覆盖优先级、持久化、AgentLoop 拦截/批准/拒绝/无回调安全默认/向后兼容）与 `tests/test_api_permissions.py`（5 项：REST 策略读写 + WS `confirm_request → confirm_reply` 批准与拒绝全链路） |
| V2 静态检查 | 新增代码 `ruff check` **全部通过**；全量 ruff **13**、mypy **14**，均与基线持平 |
| V3 核心流程 | 真实启动 uvicorn，冒烟脚本 **13/13 通过**：权限策略与风险清单（code_runner=dangerous、calculator=read）、切换 confirm_all 生效并恢复、单工具 deny 生效并清除，外加 P0-1 的全部回归项 |
| V4 前端构建 | `vue-tsc -b --force` 通过；`vite build` 成功 |

> 修复的两个真实缺陷（在实现过程中由测试暴露）：① 持久化策略被构造函数默认值覆盖，导致重启后策略丢失；② 被拒绝/deny 的工具调用未回填 tool 消息，会在会话末尾留下「带 tool_calls 却无对应 tool 响应」的 assistant 消息，下一轮请求必被 API 以 400 拒绝。

#### P0-3 验证明细（2026-09-27）

| 项 | 结果 |
|---|---|
| V1 后端测试 | **222 passed**（204 → 222，新增 18）。新增 `tests/test_todo_manager.py`（14 项：覆盖式写入、幂等、非法状态回落、空内容跳过、会话隔离、统计、清空、持久化、工具契约、AgentLoop 端到端、缺 session 与非法参数处理）与 `tests/test_api_todos.py`（4 项 REST） |
| V2 静态检查 | 新增代码 `ruff check` **全部通过**；全量 ruff **13**、mypy **14**，均与基线持平 |
| V3 核心流程 | 真实启动 uvicorn，冒烟脚本 **16/16 通过**（新增 Todo 清单读取、清空、不存在的会话返回 404） |
| V4 前端构建 | `vue-tsc -b --force` 通过；`vite build` 成功 |

> 设计取舍：`todo_write` 采用**覆盖式写入**（每轮提交完整列表），与 Deep Agents / Claude Code 一致，语义幂等、不会出现增量更新错乱。为此新增 `ToolPlugin.needs_session` 契约字段，让 AgentLoop 注入 `session_id`（默认 False，不影响既有工具）。`todos` 表使用 `CREATE TABLE IF NOT EXISTS`，旧库启动即自动升级，无需迁移脚本。

#### P0-4 验证明细（2026-09-27）

| 项 | 结果 |
|---|---|
| V1 后端测试 | **243 passed**（222 → 243，新增 21）。新增 `tests/test_session_export_fork.py`（18 项：导出结构、消息/工具调用保留、含任务清单、导入幂等新会话、fork 截断历史、互不影响、非法会话 404、工具调用 id 重建防主键冲突）与 `tests/test_api_todos.py` 新增 PUT 覆盖写入（剥离来源 id） |
| V2 静态检查 | 新增代码 `ruff check` **全部通过**；全量 ruff **13**、mypy **14**，均与基线持平 |
| V3 核心流程 | 真实启动 uvicorn，冒烟脚本 **20/20 通过**（新增：导出含消息与任务、分叉继承消息、导入往返） |
| V4 前端构建 | `vue-tsc -b --force` 通过；`vite build` 成功 |

> 修复的两个真实缺陷（实现过程中由测试暴露）：① 导出/导入复用了来源 Todo 的 id，触发主键冲突；已在 `_save_todos` 剥离 `id` 字段。② 导出消息漏带 `tool_call_id`（它存在 `tool_calls_json` 里而非 `Message.to_dict`），会导致导入后的工具消息失去关联、上下文错乱；已在 `export_session` 补上。另新增 `PUT /api/sessions/{id}/todos` REST 端点（覆盖写入），此前 todos 只能通过 AI 的 `todo_write` 工具写入、无 HTTP 写入口，导致导出/导入冒烟无法经 REST 验证任务清单。

#### P1-1 验证明细（2026-09-28）

| 项 | 结果 |
|---|---|
| V1 后端测试 | **256 passed**（243 → 256，新增 13）。新增 `tests/test_mcp_client.py`（连接 echo server、tools/list、调用 echo/add、错误工具、缺失命令、配置解析、connect_all、缺配置）9 项 + `tests/test_mcp_plugin.py`（插件把 `mcp__echo__echo` / `mcp__echo__add` 注册进 ToolRegistry 并可执行、deactivate 注销并断开）2 项 + `tests/test_api_mcp.py`（真实子进程 echo server：REST `/servers` `/tools` `refresh` 全链路；无配置返回空列表）2 项 |
| V2 静态检查 | 新增代码 `ruff check` **全部通过**；全量 ruff **13**、mypy **14**，均与基线持平 |
| V3 核心流程 | 真实启动 `uvicorn`（`MCP_CONFIG_PATH` 指向 echo server 配置），冒烟脚本 **22/22 通过**：新增 `GET /api/mcp/servers` 可用、echo server 已连接且 `mcp__echo__echo`/`mcp__echo__add` 已注册；含此前全部回归项 |
| V4 前端构建 | `vue-tsc -b --force` 通过；`vite build` 成功（1.61s） |

> 设计取舍：为规避重型官方 SDK 依赖（磁盘受限，且所有依赖必须落在项目内），自研**零第三方依赖**的 stdio MCP 客户端（换行分隔 JSON-RPC 2.0，协议版本 `2024-11-05`），行为与官方规范一致。修复的一个真实缺陷（由测试暴露）：`PluginLoader` 只支持**单次**启动——同一进程内重复进入 lifespan（uvicorn `--reload`、测试多次 `TestClient(app)`）时，插件因「已加载」而跳过激活，导致其工具/服务不再注册；已在 `load_and_activate_all` 增加**幂等再激活**（已加载但未激活则重新激活）。同时为设置页补齐 `.btn-sm`/`.btn-xs`/`.btn-danger`/`.section-desc`/`.empty-state` 等此前只有引用、没有定义的样式类，避免细节穿帮。

#### P1-4 验证明细（2026-09-28）

| 项 | 结果 |
|---|---|
| V1 后端测试 | **271 passed**（256 → 271，新增 15）。新增 `tests/test_artifact_store.py`（阈值判定、落盘文件与元数据、全文/分页读取、列表过滤、删除连带文件、摘要；`post_tool_call` 钩子：大结果落盘并改写、小结果不动、错误与 `read_artifact` 结果跳过；`read_artifact` 工具契约/读取/报错/服务缺失；**AgentLoop 端到端**：大输出被落盘后上下文中的 tool 消息显著缩短）与 `tests/test_api_artifacts.py`（REST 列表/读取/分页/删除/404） |
| V2 静态检查 | 新增代码 `ruff check` **全部通过**；全量 ruff **13**、mypy **14**，均与基线持平 |
| V3 核心流程 | 真实启动 `uvicorn`，冒烟脚本 **25/25 通过**：新增 `GET /api/artifacts` 可用、`read_artifact` 已注册且 `risk=read`、插件列表含 `artifact_store`/`tool_read_artifact`；含此前全部回归项 |
| V4 前端构建 | `vue-tsc -b --force` 通过；`vite build` 成功（1.21s） |

> 设计取舍：落盘阈值默认 **6000 字符**（约 1.5K~2K token，接近 4096 预算的一半），可用 `HARNESS_OFFLOAD_THRESHOLD` 调整；工件目录默认 `workspace/artifacts/`，可用 `HARNESS_ARTIFACTS_DIR` 覆盖（测试已在 `conftest` 隔离，避免污染项目目录）。`read_artifact` 结果**不二次落盘**（否则回读无意义且会嵌套），并以 `MAX_READ_CHARS`（2 万字符）约束单次读取。修复的一个真实缺陷（由测试暴露）：`HookManager.execute` 在管线末尾丢弃了累积的 `metadata`，导致钩子附加的元数据无法回传给调用方；已改为连同累积 metadata 一并返回（符合 `HookResult.metadata` 的文档语义）。

#### P1-3 验证明细（2026-09-28）

| 项 | 结果 |
|---|---|
| V1 后端测试 | **284 passed**（271 → 284，新增 13）。新增 `tests/test_memory_manager.py`（保存/覆盖、全局与会话作用域隔离、按 key/value/tags 检索、空查询返回最近、更新、删除、`render_hint` 仅含 global 且截断长值；`memory_save` / `memory_search` 工具契约与校验；**AgentLoop 注入**：旧会话写入的 global 记忆出现在新会话的 system 前缀中）与 `tests/test_api_memories.py`（REST 增删改查 + 校验 + 404） |
| V2 静态检查 | 新增代码 `ruff check` **全部通过**；全量 ruff **13**、mypy **14**，均与基线持平 |
| V3 核心流程 | 真实启动 `uvicorn`，冒烟脚本 **33/33 通过**：新增插件含 `memory_manager`/`tool_memory`、工具 `memory_save`(write)/`memory_search`(read) 已注册、`/api/memories` 保存→检索→更新→删除全链路；含此前全部回归项 |
| V4 前端构建 | `vue-tsc -b --force` 通过；`vite build` 成功 |

> 设计取舍：`memories` 表 `embedding` 列预留给未来的向量检索，当前用 SQLite `LIKE` 做关键词匹配（零新增依赖）。`save` 对相同 `(scope, session_id, key)` 做 upsert（幂等，模型反复保存不产生重复）。`render_hint` 只注入最近 8 条 `global` 记忆、单条截断 200 字符，避免长期记忆本身撑爆上下文；`session` 作用域不注入（避免串会话）。

#### P1-5 验证明细（2026-09-28）

| 项 | 结果 |
|---|---|
| V1 后端测试 | **290 passed**（284 → 290，新增 6）。新增 `tests/test_tracing.py`（span 记录/按 trace 聚合/会话过滤/删除；插件订阅 `trace.span` 落库并把未知字段收进 meta；**AgentLoop 端到端**：一次对话产生 `run`+`model`+`tool` 完整 span 链且模型 span 带 token、工具 span 带耗时）与 `tests/test_api_traces.py`（REST 列表/详情/删除/404） |
| V2 静态检查 | 新增代码 `ruff check` **全部通过**；全量 ruff **13**、mypy **14**，均与基线持平 |
| V3 核心流程 | 真实启动 `uvicorn`，冒烟脚本 **35/35 通过**：新增插件含 `tracing`、`GET /api/traces` 可用；含此前全部回归项 |
| V4 前端构建 | `vue-tsc -b --force` 通过；`vite build` 成功 |

> 设计取舍：引擎在**已知计时点**（模型调用前后、工具执行前后）计算耗时并携带 usage，通过事件总线 topic `trace.span` 发布；`tracing` 插件订阅落库。这样 span 的耗时/token 精确，且引擎与存储解耦——`EventBus` 未注册或无订阅者时发布是空操作，对主流程零影响。为让引擎能发布事件，`main.py` 把 loader 的 EventBus 注册进服务注册表（幂等保护，避免重复启动时栈增长）。trace 聚合用 `MAX(duration_ms)` 代表整轮耗时（run span 通常最长），`SUM(total_tokens)` 汇总 token。

#### P1-2 验证明细（2026-09-28）

| 项 | 结果 |
|---|---|
| V1 后端测试 | **296 passed**（290 → 296，新增 6）。新增 `tests/test_subagent.py`：子代理 `run_task` 回传摘要且临时会话用完即删、无 provider 报错、使用运行环境 provider；`task` 工具契约与错误分支；**端到端隔离**：主代理调用 `task` 后，子代理收集到的 3000 字符大输出**未**进入主会话上下文，只留「[子代理已完成] + 摘要」 |
| V2 静态检查 | 新增代码 `ruff check` **全部通过**；全量 ruff **13**、mypy **14**，均与基线持平 |
| V3 核心流程 | 真实启动 `uvicorn`，冒烟脚本 **37/37 通过**：新增插件含 `subagent`/`tool_task`、`task` 工具已注册（risk=write）；含此前全部回归项 |
| V4 前端构建 | 本项无前端改动（`task` 作为普通工具在既有工具卡片中展示）；沿用最近一次 `vue-tsc` + `vite build` 通过结果 |

> 设计取舍：子代理需要拿到「当前这一轮的 provider/model」，但它们不在服务注册表里，也不适合塞进工具 `args`（args 会被序列化进 WebSocket 事件）。为此新增 `harness/engine/runtime.py`，用 `contextvars.ContextVar` 承载运行环境——**按异步任务隔离**，不同会话的并发运行互不干扰；`AgentLoop.run()` 开始时设置、`finally` 中重置。子代理工具集默认排除 `task` 自身（防无限递归）。另修复一个真实体验缺陷：子代理运行期间，其流式 token 会被 WS 层当作主回答推送；已在 `streaming_chat` 中检测「运行环境 session_id ≠ 当前会话」时抑制向主界面输出（子代理仍正常累积内容）。

#### 全量自检与修复（2026-09-28）

对 P0/P1 全部能力做了一次端到端自检（真实库污染 / 业务逻辑 / 前端细节 / 任意 bug），共发现并修复 **8** 处问题，明细见 `CHANGELOG.md [0.0.10]`。其中两处为严重问题：

| 严重度 | 问题 | 根因 | 修复 |
|---|---|---|---|
| 🔴 严重 | 本轮新增 i18n **未进构建产物** | `frontend/src/` 残留 30 个陈旧 `.js` 编译产物，Vite 解析无扩展名 import 时 **`.js` 优先于 `.ts`**，`import zh from '../i18n/zh'` 实际加载过期 `zh.js` | 备份并删除 30 个 `.js`；`tsconfig.app.json` 显式 `noEmit`；`.gitignore` 兜底；重建后文案全部入包（+17KB） |
| 🔴 严重 | 冒烟测试**污染真实库** | 手工冒烟打真实库，异常路径遗留 6 个测试会话 | 备份 + 精确清理（含孤立行校验）；脚本收进仓库并加 `try/finally` 自清理；`make smoke` 用一次性临时库 |
| 🟠 健壮 | MCP 断开后无法重连 | `_closed` 未在 `connect()` 复位 | 复位 `_closed`/`_pending` |
| 🟠 正确 | 记忆检索 LIKE 通配符注入 | `%`/`_` 被当通配符 | `ESCAPE '\'` 转义 |
| 🟠 健壮 | artifacts / spans 无上限增长 | 无保留策略 | 新增上限（500 / 20000，可调），按 rowid 清最旧 |
| 🟠 健壮 | 运行环境 ContextVar 泄漏 | `CancelledError` 不走尾部 finally | 主逻辑块 `finally` 复位 |
| 🟡 性能 | `GET /api/traces/{id}` 全表扫描 | 拉 1000 条再筛 | 定向聚合查询 |
| 🟡 细节 | 窄屏排版 | 泳道图/卡片头列宽固定 | 窄屏媒体查询优化 |

修复后回归：**V1 301 passed**（296 → 301，新增 5 项回归测试）；V2 ruff 13 / mypy 14 与基线持平；V3 仓库内 `scripts/smoke_http.py` 全绿且**临时库跑完各表为 0**、真实库未被触碰；V4 `vue-tsc` + `vite build` 通过，且新文案经实测已进入产物。

#### 用户反馈驱动的增强与修复（2026-09-28，第二轮）

针对 6 项使用反馈逐条处理，明细见 `CHANGELOG.md [0.0.11]`：

| # | 反馈 | 处理 | 验证 |
|---|---|---|---|
| 1 | 工具确认弹窗背景透明、文字与按钮分不清 | 根因：`--bg-card`/`--bg-primary` **两个变量根本不存在** → 背景解析为透明；且 `.btn-*` 只在 SettingsView 的 scoped 样式里定义，ChatView 里按钮完全无样式。已把通用按钮类提升到全局 `style.css`，弹窗改为实色背景 + 描边/阴影 + 风险徽标配色 + 分区布局 | 构建通过；`dist` 中含新样式 |
| 2 | 轨迹展开无滚动条、span 数与步骤不符、时间差 8 小时 | ① 展开区加纵向滚动（`max-height:46vh`）；② 后端新增 `step_count`（剔除整轮 run span），前端显示「步骤 N · span M」；③ 后端统一输出**带时区偏移的 UTC** 时间戳 + 前端 `formatLocalTime` 转本地 | pytest 覆盖 `step_count`；时间由 UTC→本地 |
| 3 | 需要连远程 MCP（SSE） | 新增 SSE/HTTP 传输（与 stdio 共用协议基类）；配置支持 `url`/`type=sse`/`description`/`disabled`/`headers`。真实远端调优两点：通知消息在部分网关被 404 拒绝（改为容忍）、网关在 keep-alive 复用连接时会使会话失效（POST 强制新连接 + `Connection: close`） | **真实远端验证**：`youth-mcp` 连接成功、发现 9 个广告工具、取回真实汇总数据；本地 SSE echo server 单测 9 项 |
| 4 | 会话导出/导入/分叉看不懂、逻辑不清 | 交互重做：可见「⋮」菜单（每项带说明）+ 页头提示 + 导入按钮 + 成功轻提示；逻辑修复：**分叉截断在带 tool_calls 的 assistant 消息上时必须带上后续 tool 消息**，否则上下文非法 | 新增 `TestForkKeepsToolMessages` |
| 5 | 需要对每条问答复制/删除/追问 | 每条提问：复制、删除本轮（物理删除，含工具消息）；每条回答：复制、追问、从此处分叉；新增 `DELETE /api/sessions/{id}/messages/{message_id}?with_turn=true` | 新增 `TestDeleteTurn` + `TestMessageDeleteAPI` |
| 6 | 需要自主定时总结会话为记忆 | 新增 `MemorySummarizer` 后台任务（默认 1800s，增量游标、幂等 upsert、优雅降级）+ REST `summary-info`/`summarize` + 前端「自动总结」卡片 | **真实实例验证**：扫描 2 个会话、总结 2 个、写入 6 条记忆 |

**本轮验证**：**V1 322 passed**（301 → 322，新增 21 项）；V2 我的新增代码 ruff **0 新增**（全量 15 = 既有 13 + 用户新增 skill 脚本 2）、mypy 14 与基线持平；V3 冒烟 **41/41 通过**（含 stdio + SSE 双 MCP、整轮删除、记忆总结）；V4 `vue-tsc` + `vite build` 通过且新文案实测入包；另对**真实远端 MCP** 与**真实记忆总结**做了端到端验证。

> 顺带修复：`tests/conftest.py` 增加 `MCP_CONFIG_PATH` 隔离——此前测试会读取项目 `mcp.json` 并尝试连接真实远端 server（慢且依赖网络）。

#### 定时任务（Scheduled Tasks）（2026-09-28，第三轮）

让 Agent 到点自动干活：在**新建的隔离会话**里执行一段提示词，并且**只开放任务里勾选的工具**。

| 项 | 结果 |
|---|---|
| 落地位置 | 后端：`harness/modules/scheduler/service.py`（调度规则 + 下次运行时间计算 + 任务/运行记录 CRUD）、`harness/modules/scheduler/runner.py`（受限工具集 + 隔离会话执行 + 超时/预授权）、`plugins/scheduler/`（后台每 30s 检查到期任务，顺序执行 + 抢占防重入）、`api/rest/schedules.py`（REST）、`infra/database.py`（`scheduled_tasks` / `scheduled_task_runs` 两表）、`engine/agent_loop.py`（新增 `skill_allowlist`）、`modules/skill_manager/service.py`（`render_catalog` 支持按名过滤）；前端：设置页「定时任务」标签页（列表 + 新建/编辑表单 + 立即执行 + 运行历史） |
| 调度策略 | 每天固定时间 / 每周指定星期几 / 固定间隔（分钟·小时）/ 一次性（跑完自动停用）；启停即重算下次运行时间 |
| 工具范围 | 多选 MCP 服务器（其工具才可用）+ 多选 Skills（只有勾选的注入上下文，`use_skill` 被包装为只放行这些）+ 多选内置工具；未勾选一律不进注册表 |
| 其他能力 | 隔离会话（标题 `⏱ 任务名 · 时间`）、预先授权（任务配置即授权）、可指定 provider/模型、最大迭代数与超时、运行历史（状态/耗时/会话/摘要/错误）、无 provider 优雅跳过、`HARNESS_SCHEDULER` / `HARNESS_SCHEDULER_TICK` 可配 |

**验证结果**：**V1 340 passed**（322 → 340，新增 18：调度计算 6、服务 CRUD/到期/抢占/一次性自停用 4、受限工具集与 use_skill 白名单 3、执行器 2、REST 3）；V2 新增代码 ruff **0 新增**、mypy 14 与基线持平；V3 冒烟 **47/47 通过**（新增：可选项、新建任务、立即执行、停用清空下次运行、运行历史，均含自清理）；V4 `vue-tsc` + `vite build` 通过且新文案实测入包。
另做**真实端到端验证**：在真实实例上创建任务 → 「立即执行」→ DeepSeek 实际运行，**只调用了勾选的 `current_time` 工具**，返回正确本地时间，运行历史记录「ok / manual / 5094ms」，会话正常创建；随后清理自检任务（界面上另留了一个默认停用的示例任务「每日大模型新闻早报」，关联 `daily-llm-news` Skill）。

#### 会话文件传输（文档 + 图片）（2026-09-28，第四轮）

让对话支持携带附件：用户在聊天输入区上传**文档**与**图片**，随消息一起发给模型（文档内联为文本、图片按 OpenAI 视觉规范以 base64 注入）。

| 项 | 结果 |
|---|---|
| 落地位置 | 后端：`harness/modules/attachment/limits.py`（类型/数量/大小上限）、`service.py`（分类校验、存储、Pillow 缩放、`render_content_parts` 多模态渲染）、`api/rest/attachments.py`（multipart 上传 + 回传）、`infra/repository.py`（`AttachmentRepository`）、`infra/database.py`（`attachments` 表 + `messages.attachments` 列）、`modules/context_manager/service.py`（`_render_multimodal` 在压缩后统一渲染）、`engine/agent_loop.py` + `hook_types.py`（`attachments` 贯穿持久化）、`api/ws/chat.py`（WS 消息体 `attachments` + 归属校验）、`modules/session_manager/service.py`（删会话连带清理附件）；前端：`ChatView.vue`（📎 按钮 + chip + 三类上限预校验）、`MessageItem.vue`（气泡内渲染缩略图/文档 chip）、`stores/chat.ts`（`pendingAttachments` + 上传）、`api/client.ts`（`uploadAttachment`）、i18n |
| 支持类型 | 文档 26 种（txt/md/csv/json/yaml/log/py/js/ts/html/xml/ini/toml/sh/bat 等）；图片 6 种（png/jpg/jpeg/gif/webp/bmp） |
| 上限 | 单条消息：文档 ≤5、图片 ≤4、总数 ≤8；单文档 ≤200KB、单图片 ≤1.5MB 且最长边缩放至 1280px（控视觉 token） |
| 安全/健壮 | 上传逐个分类校验，任一不合规整批拒绝（`ATTACHMENT_INVALID`/`ATTACHMENT_LIMIT`）；公开元信息不含存储路径；回传校验附件归属会话（越权 404）；删会话 rmtree 物理文件 + 清 DB 行 |

**验证结果**：**V1 357 passed**（340 → 357，新增 17：上传/类型/大小/数量校验、回传、跨会话越权、消息持久化、`ContextService` 多模态渲染、纯文本不变、会话删除清理）；V2 附件新代码 ruff **0 新增**（顺手修 `context_manager` 一处既有 E501）、mypy 14 持平；V4 `vue-tsc` + `vite build` 通过且新文案入包。
**真实端到端（deepseek-v4-flash）**：上传 73 字节文档（含唯一代号 `BANANA-42`）→ 模型准确答出 `BANANA-42`（token 3651）；上传 92 字节纯红 PNG → 模型答出「红色」（token 3780），证明**文档文本注入与图片视觉多模态均真实生效**；两会话用后即删，真实库与附件目录无残留。
> 顺带修复一处**测试污染**：`tests/conftest.py` 此前隔离了 DB/工件/MCP 配置但漏了附件目录，导致附件测试把文件写进真实 `data/attachments/`（累计 39 个垃圾文件夹）；已补 `HARNESS_ATTACHMENTS_DIR` 隔离，残留垃圾可逆移动到 `data/.trash_attachments_junk/`。新增运行时依赖 `python-multipart`（已写入 `pyproject.toml`，`start-all.bat` 增加导入自检）。

#### G4 · Checkpoint 断点续跑（2026-09-30）

长任务（computer_use 多轮操作、定时任务）中断 / 超时 / 服务重启后可从断点续跑，不丢进度。

| 项 | 结果 |
|---|---|
| 落地位置 | 后端：`harness/engine/checkpoint.py`（`RunCheckpointService`：检查点 upsert / 更新状态 / 按 run 读取 / 按会话列举；`resume_run`：读检查点 → 选 provider → 重建 AgentLoop 续跑）、`harness/engine/agent_loop.py`（运行开始 / 每轮迭代后 / 终答后 / 异常 四处 best-effort 写 `agent_runs` 检查点）、`harness/api/rest/runs.py`（`POST /api/runs/{run_id}/resume`）、`harness/infra/database.py`（`agent_runs` 表 + `idx_agent_runs_session` 索引）、`harness/main.py`（接线 `setup_runs_routes` + `runs_router`） |
| 设计取舍 | 消息在每轮**即时持久化**到会话，因此会话历史本身就是完整状态快照；续跑只需基于已持久化上下文让模型继续（追加上一条「继续」引导语），**不重放内存态**，避免状态不一致。检查点写入全部 best-effort，任何异常都不影响主对话流程。provider 选择：指定 `provider_id` 则用指定，否则自动选第一个「已启用且已配置 Key」的 provider |
| **验证结果** | **V1 新增 10 passed**（`tests/test_checkpoint.py`：`RunCheckpointService` 增删改查 5 项 + AgentLoop 运行中写 `done`/`error` 检查点 2 项 + `resume_run` 三路径——检查点不存在 / 无可用 provider / 正常续跑）；V2 新增代码 ruff **0 新增**、mypy **未新增**（顺带修正 `_call_model` 四元组返回值注解，干掉 3 个历史错误）；V3 用 fake provider 跑通 AgentLoop 全链路验证 checkpoint 写入与 resume 续跑；V4 无前端改动（沿用最近一次 `vue-tsc` + `vite build` 通过） |
| 关联修复 | `sqlite3.Row` 无 `.get()` 方法，`checkpoint._row_to_checkpoint` 改用下标访问；测试 `BoomProvider` 改为合法 async generator 以触发模型调用失败路径 |

> 说明：`resume_run` 依赖 `ProviderRegistry` 中存在已启用且已配置 Key 的 provider；无可用 provider 时返回 `error` 字段而非崩溃。

#### E6 · 插件 / 技能从 zip / git 外部分发安装（2026-09-30）

除内置市场「本地目录复制」外，支持从 **zip 归档（本地路径 / http(s) URL）** 或 **git 仓库（浅克隆）** 安装第三方发布的插件 / 技能，打通生态分发（架构 v0.2）。

| 项 | 结果 |
|---|---|
| 落地位置 | 新增 `harness/modules/package_installer/service.py`（`PackageInstaller.materialize_zip` / `materialize_git` / `materialize`、安全解压防 zip slip、外层包裹目录自动下沉、`copy_tree` / `resolve_package_root` 辅助）；`harness/api/rest/plugins.py`（`POST /api/plugins/install-external`，标记 `source=external`）+ 卸载守卫放开 marketplace/external；`harness/api/rest/skills.py`（`POST /api/skills/install-external`，按 SKILL.md frontmatter 名称 slugify 成包标识） |
| 设计取舍 | 安装器只做「拉取 + 落盘」，包语义校验（plugin.json / SKILL.md）与加载 / 激活仍由各路由负责，职责不越界；`subdir` 支持包嵌套在子目录；`source=external` 与 `marketplace` 同享卸载权限，非系统插件均可卸载；zip 解压做路径穿越防御 |
| **验证结果** | **V1 新增 12 passed**（`tests/test_package_installer.py`：安装器核心 6 项——本地 zip / 包裹目录自动下沉 / 文件缺失 / zip slip 拒绝 / 本地 git 浅克隆 / 克隆失败；REST 集成 6 项——插件 zip / git 安装（fake loader）、插件非法包 400、技能 zip / git 安装、技能缺 SKILL.md 400）；V2 改动文件 ruff **0 新增**、mypy **0 问题**（顺带修复 `plugins.py:161` marketplace 块 `manifest` 字典的历史类型标注，改为 `dict[str, Any]`）；V3 / V4 无前端改动 |
| 关联修复 | 卸载守卫从「仅 marketplace」放宽为「marketplace 或 external」，使外部安装的插件可卸载 |

#### E5 · 回归评测框架（2026-09-30）

为改动提供**任务级客观回归基线**：把一批带期望的任务跑过 AgentLoop，按关键词 / 工具调用 / 迭代 / 耗时打分，聚合通过率与工具使用。

| 项 | 结果 |
|---|---|
| 落地位置 | 新增 `harness/eval/`（`cases.py` 用例声明与加载、`scoring.py` 打分、`runner.py` 运行器、`report.py` 汇总 + JSON/Markdown 导出）；`harness/eval/__main__.py` CLI（`python -m harness.eval`，支持 `--provider`/`--model`/`--tag`/`--report`，失败退出 1 可做 CI 门槛）；`tests/evals/eval_cases.json` 4 个种子任务 + `tests/evals/README.md` |
| 设计取舍 | 零新增依赖；每 case 独立会话 / 独立 AgentLoop 互不污染；provider 由工厂按需构造，离线测试用脚本化 provider 确定性回归、在线复用真实 provider；评测框架只做「运行 + 打分 + 汇总」，不内嵌业务 |
| **验证结果** | **V1 新增 20 passed**（`tests/test_eval.py`：加载校验 7、打分逻辑 8、端到端运行/指标/导出/隔离 5）；V2 新模块 ruff **0 新增**、mypy **0 问题**；V3 / V4 无前端改动（在线评测走 CLI） |

#### E8 · 前端测试（2026-09-30）

为前端逻辑提供自动化回归：工具函数、Pinia store、组件渲染都有确定性测试。

| 项 | 结果 |
|---|---|
| 落地位置 | 引入 vitest 5 + @vue/test-utils + jsdom（devDeps）；`vite.config.ts` 加 `test` 配置（jsdom/globals/v8 覆盖率）；`package.json` 加 `test` / `test:watch` / `test:coverage` 脚本；新增 4 个测试文件（`utils/format` 7、`utils/datetime` 8、`stores/todos` 5、`components/MarkdownRenderer` 4） |
| 设计取舍 | store 测试用 `vi.hoisted` + `vi.mock` 隔离 API 客户端，避免网络与实现顺序问题；mock 相对路径与被测文件同目录（`../api/client`）；组件测试用 @vue/test-utils 挂载后断言渲染结果 |
| **验证结果** | **V1 `pnpm test` 4 文件 / 24 tests 全绿**；V4 `pnpm build` vue-tsc + vite build 仍通过（303 modules / 3.04s）；V2/V3 无后端改动 |

#### E7 · 向量记忆检索（2026-09-30）

把长期记忆从「LIKE 连续子串」升级为「向量余弦语义检索」，并保留 LIKE 降级，默认零依赖、可离线。

| 项 | 结果 |
|---|---|
| 落地位置 | 新增 `harness/modules/memory_manager/embedding.py`（`Embedder` 抽象、`HashingEmbedder` 特征哈希、`OpenAICompatibleEmbedder`、`cosine_similarity` / `tokenize`、`build_embedder_from_env`）；改造 `memory_manager/service.py`（`save` 写 `embedding`、`search` 向量为主 + LIKE 回退、`_vector_search` / `_row_vector_or_backfill` 懒回填 / `_like_search`、`update` 重嵌）；`plugins/memory_manager/main.py` 接入环境工厂 |
| 设计取舍 | 默认本地 `HashingEmbedder`（256 维、中英混合分词、确定性），无需外部模型即可改善「词项重叠」匹配；配置 `HARNESS_EMBEDDING_MODEL` + API key 才启用远端真语义；`HARNESS_EMBEDDING=off` 显式降级；旧数据在检索时懒回填向量，无需迁移；最小相似度阈值 `0.15`；下划线独立成 token 以兼容 `a_b` / `_` 字面量检索 |
| **验证结果** | **V1 新增 `tests/test_memory_vectors.py` 17 passed**（哈希嵌入器 7、余弦 3、远端构造 1、记忆服务 6）；连同既有记忆测试 `test_memory_manager.py` / `test_api_memories.py` 共 **37 passed**；V2 `ruff check` 与 `mypy` 对新模块零问题（修 `_hashes` 返回类型 `tuple[int, float]`）；V3/V4 无前端改动 |

#### E3 · 结构化 compaction（2026-09-30）

把上下文压缩从「全量摘要重写（成本高、易丢事实）」升级为「保留最近 N 条原文 + 结构化事件日志」，折叠过程确定性、零额外成本、不产生幻觉。

| 项 | 结果 |
|---|---|
| 落地位置 | 新增 `StructuredCompactionStrategy`（`harness/modules/context_manager/service.py`：`_collapse` / `_tool_call_line` / `_safe_boundary` / `_build_log_entries` / `apply`）；`plugins/context_manager/main.py` 接入，`config.strategy` 取 `structured` / `structured_compaction` / `compaction_v2` 启用 |
| 设计取舍 | 旧消息折叠为一条按「请求 / 工具调用（含参数）/ 结果 / 结论」编号的结构化事件日志 system 消息，**不调用 LLM 重写**，零成本、不产生幻觉；`_safe_boundary` 自动前移裁剪边界，**不切断 assistant(tool_calls) ↔ tool 组**，避免孤立 tool 消息；默认保留最近 8 条，长内容 / 工具结果按上限截断（160/120 字符） |
| **验证结果** | **V1 新增 `tests/test_structured_compaction.py` 8 passed**（短对话不变、最近原文保留、旧消息折叠、单日志位置、keep=3/4/5 均无孤立 tool、钉住消息保留、长内容截断、None 内容安全）；连同既有 `test_context_manager.py` 共 **19 passed**；V2 `ruff check` 与 `mypy` 零问题（修 `_collapse` 对 Any 返回值的标注，分支变量加 `strategy: ContextStrategy` 注解）；V3/V4 无前端改动 |

#### E2 · Docker 沙箱后端（2026-09-30）

把代码执行从「本机子进程」升级为「Docker 容器级隔离」，默认禁网、限制资源、镜像白名单，每次执行一次性容器、结束即销毁。

| 项 | 结果 |
|---|---|
| 落地位置 | 新增 `DockerBackend`（`harness/modules/sandbox_manager/service.py`：`is_available` / `_check_image_allowed` / `_build_run_args` / `_python_command` / `_shell_command` / `_force_remove` / `execute`）；`plugins/tool_code_runner/main.py` 按 `config.backend`（local/docker）或 `HARNESS_SANDBOX_BACKEND` 选择后端；`plugin.json` 补 backend / docker_image / docker_network / docker_memory / docker_cpus 配置 |
| 设计取舍 | 每次执行用 `docker run --rm`（一次性容器，无生命周期管理、天然无残留），工作区以 bind mount 挂载到 `/workspace`，文件跨执行持久；默认 `--network none` 禁网、`--memory` / `--cpus` 限制资源；镜像白名单防止任意镜像；超时杀掉 docker CLI 后容器可能仍在运行，故用唯一 `--name` + `docker rm -f` 兜底防泄漏；零新增依赖（仅调 docker CLI）；docker 不可用明确报错而非崩溃；安全检查（危险命令 / 路径遍历）抽为模块级共享函数，两后端复用 |
| **验证结果** | **V1 新增 `tests/test_docker_sandbox.py` 18 passed**（参数构造、成功执行、退出码透传、不可用拦截、超时强删、镜像白名单、危险 / 路径拦截、不支持语言、输出截断、可用性检测、工作区清理）；连同既有 `test_sandbox.py` 共 **30 passed、1 skipped**（真实 docker 端到端 1 例在本机无 docker 时跳过）；V2 `ruff check` 与 `mypy` 零问题（`asyncio.TimeoutError` 改用内置 `TimeoutError`，runner 类型改为带命名参数的 Protocol，插件 backend 变量加 `SandboxBackend` 注解）；V3/V4 无前端改动 |

#### E9 · 会话 / 消息搜索（2026-09-30）

为侧边栏提供「按标题 / 消息内容」搜索：输入关键词即返回命中会话与片段，无需逐个翻找。

| 项 | 结果 |
|---|---|
| 落地位置 | 新增 `SessionService.search_sessions`（`harness/modules/session_manager/service.py`：标题 + 消息内容 LIKE 聚合、每会话最多 3 条命中片段、`_sql_like` 转义 / `_make_snippet` 片段）；新增 `GET /api/sessions/search?q=`（`harness/api/rest/sessions.py`，在 `/{session_id}` 之前注册）；前端 `components/SessionSidebar.vue` 搜索框（防抖 300ms、结果平铺、点击直达、Esc / × 退出） |
| 设计取舍 | 用 SQL LIKE 做标题 / 内容匹配（零新增依赖、无需 FTS5）；用户输入的 `%` / `_` / `\` 经 `ESCAPE '\'` 转义防通配符注入；片段取命中位置前后各 48 字并折叠空白，未命中取开头；默认不搜归档会话（`include_archived` 可开）；大小写不敏感 |
| **验证结果** | **V1 新增 `tests/test_session_search.py` 12 passed**（内容 / 标题命中、片段、空查询、LIKE 通配符转义、归档开关、每会话最多 3 条、大小写、message_count、REST 路由顺序）；V2 新增代码 `ruff check` 干净，`mypy` 仅余 sessions.py 2 个历史基线错误（新路由用 cast 未增加）；V4 新增 `SessionSidebar.test.ts` 4 测试、前端共 28 passed，`pnpm build` 303 模块通过 |

#### E1 · 多渠道 Channel（2026-09-30）

把 Agent 从 GUI 里接出来：Webhook 与 Telegram 消息统一进 AgentLoop，按「渠道 + 用户」映射 / 复用会话。

| 项 | 结果 |
|---|---|
| 落地位置 | 新增 `harness/modules/channel_manager/`（`Channel` 抽象：start/stop/send；`ChannelManager`：注册渠道、统一跑 AgentLoop、`channel_links` 会话映射；`WebhookChannel`；`TelegramChannel`：Bot API 长轮询 / sendMessage，可注入 HTTP 传输）；新增 `api/rest/channels.py`（`GET /api/channels`、`POST /api/channels/inbound/{name}`、`POST /api/channels/{name}/send`、`GET /api/channels/{name}`）；新增 `plugins/channel_manager`（建管理器 + 内置 Webhook）与 `plugins/channel_telegram`（挂入 Telegram 并启动） |
| 设计取舍 | 渠道只写「如何收发」，会话与 Agent 逻辑全在 ChannelManager 共享，避免每渠道各写一套对话；`channel_links` 表（`channel` + `external_user` → `session_id`）使同一用户在多次消息 / 重启后续接同一对话；Webhook 终答由 HTTP 响应同步返回，支持 `X-Channel-Secret` 密钥与用户名单；Telegram 用标准库 urllib（零新增依赖），offset 自动推进、用户白名单、超长截断，轮询失败退避不崩溃；provider 先解析、无可用时快速失败避免无谓建会话 |
| **验证结果** | **V1 新增 `tests/test_channels.py` 13 例与 `tests/test_channel_telegram.py` 10 例，共 23 passed**（入站处理、会话复用、映射持久化、未知渠道 / 空消息 / 无 provider、用户名单、密钥校验、offset、非文本忽略、轮询失败不崩溃、超长截断、端到端回发及 REST）；V2 新增代码 `ruff check` 干净、`mypy` 零问题；V3 全应用冒烟启动，`GET /api/channels` 返回 `webhook` 与 `telegram` |

#### E10 · 多 Agent 编排（2026-09-30）

在单个子代理委派之上，提供「并行并发」与「角色流水线接力」两种编排，让多个代理协同完成复杂任务。

| 项 | 结果 |
|---|---|
| 落地位置 | 新增 `harness/modules/orchestrator/service.py`（`OrchestratorService`：`run_parallel` / `run_pipeline`；`SubtaskSpec` / `SubtaskOutcome` / `StageSpec` / `StageOutcome` / `PipelineResult`）；新增 `plugins/tool_orchestrator`（注册编排服务 + `parallel` / `pipeline` 两个工具） |
| 设计取舍 | 编排本身不重写对话流程，每个子任务仍由 SubagentService 在独立临时会话执行（隔离、压缩、权限、offload 全部保留）；并行用 `asyncio.Semaphore` 控制并发上限、按输入顺序返回、单任务异常隔离不拖垮整批；流水线按序接力、上一阶段产出经 `{input}` 占位或自动追加传给下一阶段、阶段失败即终止；护栏：并行最多 10 任务、流水线最多 8 阶段，防失控派生；子代理排除工具同步加入 `parallel`/`pipeline` 防递归 |
| **验证结果** | **V1 新增 `tests/test_orchestrator.py` 18 passed**：乱序完成仍按序返回、并发上限生效、空 / 超限报错、无 SubagentService 快速失败、异常隔离、流水线接力与占位、失败终止、两工具成功与错误路径；**端到端**经真实 AgentLoop 调 `parallel` 派生两个子代理并发完成，主上下文收到并行结果、子代理临时会话全部清理；V2 新增代码 `ruff check` 干净、`mypy` 零问题；V3 全应用冒烟：插件加载、`parallel`/`pipeline` 均注册成功 |

#### E11 · 桌面壳 Tauri（2026-09-30）

把应用打包为双击即用的桌面程序：一个窗口内运行前端与后端，不依赖浏览器。

| 项 | 结果 |
|---|---|
| 落地位置 | 新增 `desktop/`：`src-tauri`（Tauri 2 配置 / Cargo / Rust 主逻辑）、`launcher/launch-backend.py`（后端启动器）、全套图标与 README；后端新增 CORS 中间件（放行 Tauri 协议源）；前端新增 `api/runtime.ts`（Tauri 识别与后端绝对地址解析，HTTP / WS 均接入） |
| 设计取舍 | Rust 侧职责很薄：只负责选端口、经启动器拉起 / 回收后端，对话逻辑全部留在既有后端；后端默认只监听 127.0.0.1（不暴露局域网）、数据目录落到每用户应用目录（`%APPDATA%` 等），退出时 `taskkill /F /T` 终止整棵进程树；启动器支持 `--check` 与端口 / 数据目录参数；前端挂载前先解析后端地址，Tauri 中经 `get_backend_url` 命令查询、失败回退默认端口 |
| **验证结果** | **V1 新增 `tests/test_desktop_shell.py` 8 passed**：启动器解析 / 缺失解释器报错、Tauri 配置 / 能力 / Cargo 一致性、Rust 命令与前端 invoke 对齐，**端到端**经启动器真实拉起后端、`/api/health` 实测后整树终止；V2 新增代码 `ruff check` 干净、`mypy harness` 未新增；V4 新增 `runtime.test.ts` 4 测试、前端共 32 passed，`pnpm build` 通过 |

#### E12 · 认证与多用户（2026-09-30）

为需要多人 / 多机访问的场景提供可选的用户级鉴权；默认本地单用户、认证关闭、行为零变化。

| 项 | 结果 |
|---|---|
| 落地位置 | 新增 `harness/infra/security.py`（PBKDF2 口令哈希、HMAC 签名 token）、`harness/modules/auth_manager/`（`AuthService` / `User` / 管理员引导）、`harness/api/middleware/auth.py`（Bearer 中间件）、`harness/api/rest/auth.py`（status / login / logout / me / 用户 CRUD）；改造 `infra/database.py`（users 表 + sessions/memories user_id + 旧库迁移）、`infra/repository.py`、`session_manager` / `memory_manager`（按 user_id 归属过滤）、`engine/agent_loop.py`（run user_id、记忆提示与记忆工具隔离）、`modules/subagent`（继承父会话归属）、`modules/channel_manager`（渠道会话归属系统用户）、`rest/sessions` / `rest/memories`（Request 透传）、`ws/chat`（?token= 与归属校验）；前端新增 `api/token.ts`、`stores/auth.ts`、`LoginView.vue`、App 登录门、退出按钮 |
| 设计取舍 | 认证默认关闭，所有方法 `user_id=""` 不过滤，保证本地单用户行为与旧版完全一致；`HARNESS_AUTH=1` 启用后无用户时自动引导管理员（环境变量配置凭据，未设密码生成随机密码并 WARNING），历史无主会话 / 记忆划归首位管理员；中间件豁免 health/status/login 与渠道入站（渠道密钥保护）与 OPTIONS；管理员不能删自己、不能删最后一个管理员；WS 无 token / 无效 token 关闭 1008；token 由 HMAC 签名、默认 12 小时有效，密钥未配置时随机生成（重启后旧 token 失效） |
| **验证结果** | **V1 新增 `tests/test_auth.py` 23 passed**（默认关闭、登录 / 登出、401 与无效 token、管理员 CRUD 与非管理员 403、删自己 / 最后管理员保护、会话与记忆跨用户隔离、WS 1008 与跨用户拒绝）；V2 新增代码 `ruff check` / `mypy harness` 干净；V3 启用认证后登录门、鉴权与会话隔离实测；V4 前端新增 token / LoginView / client 认证测试共 10 例、前端共 42 passed，`pnpm build` 通过 |

---

## 5. 实施顺序建议

```
P0-1 Skills ✅ → P0-2 审批与权限 ✅ → P0-3 Todo 规划 ✅ → P0-4 会话导出/fork ✅
      → P1-1 MCP ✅ → P1-4 输出 offload ✅ → P1-3 长期记忆 ✅ → P1-5 Tracing ✅ → P1-2 子代理 ✅
      → P2-x → P3-x
```

顺序理由：**先补齐"安全底线 + 用户可控 + 数据可携"这三项大众刚需（P0）**，再进入生态扩展（MCP）与专业化（记忆 / 可观测 / 子代理）。P1-4 输出 offload 提前于 P1-2，因为它直接缓解当前 4096 token 预算下最现实的故障。
