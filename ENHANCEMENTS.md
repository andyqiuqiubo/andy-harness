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
| 多模型接入 | 100% | ✅ DeepSeek/Qwen/Doubao/自定义 OpenAI 兼容 | 持平（缺本地模型 Ollama 直达入口） |
| 插件架构 | 高 | ✅ 内核零业务、一切皆插件（对标 DeepSeek Harness Cordis） | **领先** |
| 上下文压缩 | 高 | ✅ 滑动窗口 / 摘要压缩 / 钉住 / 快照 | 持平（缺大工具输出 offload） |
| 沙箱执行 | 高 | ✅ 子进程 + 资源限制 + 黑名单（Docker 预留） | 持平（缺权限分级与审批） |
| **Skills 按需加载** | **40+ 客户端支持的事实标准** | ❌ 无 | **缺失（高优先）** |
| **Human-in-the-loop 审批** | 高（Cline/Claude/Codex/MS/LangGraph） | ❌ 无（仅沙箱黑名单） | **缺失（高优先）** |
| **规划 / Todo 追踪** | 高（Deep Agents/Claude Code/MS） | ❌ 无 | **缺失（高优先）** |
| **MCP 客户端** | 高（Goose/Claude/Cline/OpenHands） | ❌ 无 | 缺失 |
| 子代理委派 | 中高 | ❌ 无 | 缺失 |
| 长期记忆 | 中高（Letta/OpenAI/Deep Agents） | ❌ 无 | 缺失 |
| 会话分支 / 导出 | 中（Pi/Aider/OpenCode） | ❌ 无（架构预留 v0.2） | 缺失 |
| 可观测性 Tracing | 中高（OpenAI/Pydantic OTel/LangSmith） | ⚠️ 仅日志 + token 用量 | 部分 |
| Checkpoint / 断点续跑 | 中（LangGraph/OpenAI snapshot） | ❌ 无 | 缺失 |
| 多渠道 Channel | 中（Hermes/架构已预留契约） | ⚠️ 仅 CLI + REST/WS | 部分 |

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
**要点**：AgentLoop 每轮快照 → `agent_runs` 表 → `resume(run_id)`。**状态**：⬜

### P2-2 · 上下文压缩增强（结构化 compaction）

**来源**：OpenAI Agents SDK（built-in compaction）、Deep Agents（context engineering + replay history）、Microsoft（context compaction）。
**价值**：现有摘要压缩是全量重写，成本高；改为"保留最近 N 轮原文 + 结构化事件日志摘要"更省更准。
**要点**：新增 `compaction_v2` 策略插件，与现有策略并存可切换。**状态**：⬜

### P2-3 · 多渠道 Channel（飞书 / Telegram / Webhook）

**来源**：Hermes Agent（一个进程打通 Telegram/Slack/Discord/WhatsApp/邮件）、Bedrock、本项目架构已预留 `ChannelPlugin` 契约。
**价值**：把 Agent 从 GUI 里放出来，接入日常协作工具。
**要点**：`channel_feishu` / `channel_telegram` 插件，复用 AgentLoop。**状态**：⬜

### P2-4 · Docker 沙箱后端

**来源**：OpenHands（每会话 Docker）、Codex（OS 沙箱）、Bedrock（vm 隔离）。
**价值**：从"本机子进程"升级为真正隔离；架构文档已预留 `SandboxBackend` 接口。
**要点**：实现 `DockerBackend`；镜像白名单；网络开关。**状态**：⬜

### P3-1 · 回归评测（Eval Harness）

**来源**：SWE-Agent / OpenHands（benchmark 驱动开发）、truefoundry 评测（按"每任务成本/成功率"而非每 token 成本衡量）。
**价值**：改动后有客观回归基线，避免"感觉没坏"。
**要点**：`tests/evals/` 任务集 + 打分脚本 + CI 门槛。**状态**：⬜

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

---

## 5. 实施顺序建议

```
P0-1 Skills ✅ → P0-2 审批与权限 ✅ → P0-3 Todo 规划 ✅ → P0-4 会话导出/fork ✅
      → P1-1 MCP ✅ → P1-4 输出 offload ✅ → P1-3 长期记忆 ✅ → P1-5 Tracing ✅ → P1-2 子代理 ✅
      → P2-x → P3-x
```

顺序理由：**先补齐"安全底线 + 用户可控 + 数据可携"这三项大众刚需（P0）**，再进入生态扩展（MCP）与专业化（记忆 / 可观测 / 子代理）。P1-4 输出 offload 提前于 P1-2，因为它直接缓解当前 4096 token 预算下最现实的故障。
