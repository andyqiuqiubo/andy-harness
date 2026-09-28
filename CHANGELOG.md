# Changelog

本项目的所有重要变更都记录在此文件中。

格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/)。

---

## [0.0.12] - 2026-09-28

### 新增

#### 定时任务（Scheduled Tasks）
让 Agent 具备「到点自动干活」的能力：到点后在**新建的隔离会话**里执行一段提示词，
并且**只开放任务里勾选的工具**。

- **调度策略**：每天固定时间 / 每周指定星期几 + 时间 / 固定间隔（分钟或小时）/ 一次性指定日期时间
  （一次性任务跑完自动停用）；启用/停用后自动重算「下次运行时间」
- **工具范围（多选）**：① MCP 服务器（勾选后其工具才可用）② Skills（只有勾选的会注入上下文，
  且 `use_skill` 被包装为只放行这些）③ 其他内置工具（calculator / web_search / code_runner 等）；
  未勾选的一律不进入注册表
- **隔离会话**：每次运行新建会话（标题 `⏱ 任务名 · 时间`），可事后查看完整对话与工具调用
- **预先授权**：任务配置即授权——运行期只对「已勾选工具」自动放行确认；未勾选工具根本不存在
- **模型与上限**：可指定 provider / 模型（不指定则优先 DeepSeek），并可设最大工具迭代数与超时（超时中断并按失败记录）
- **运行历史**：每次运行记录状态（成功/失败/跳过）、耗时、会话 id、结果摘要与错误；前端可展开查看、可清空
- **健壮性**：后台每 30s（`HARNESS_SCHEDULER_TICK`）检查一次到期任务，**顺序**执行避免并发打爆配额；
  执行前先「抢占」推后 next_run_at 防重入；无可用 provider 时按「跳过」记录并给出原因；可用 `HARNESS_SCHEDULER=0` 关闭
- **REST**：`GET|POST /api/schedules`、`PATCH|DELETE /api/schedules/{id}`、`POST /api/schedules/{id}/run`（立即执行）、
  `GET|DELETE /api/schedules/{id}/runs`、`GET /api/schedules/options`（可选 MCP / Skills / 工具 / 模型）
- **前端**：设置页新增「定时任务」标签页——任务列表（状态点、调度描述、下次运行、上次状态、运行次数）
  + 新建/编辑表单（调度类型切换、星期选择、时间/间隔、Prompt、MCP/Skills/工具多选、模型与上限）
  + 一键「立即执行」与运行历史
- 存储：新增 `scheduled_tasks` / `scheduled_task_runs` 两张表（`CREATE TABLE IF NOT EXISTS`，旧库自动升级）

### 变更
- `AgentLoopConfig` 新增 `skill_allowlist`（None=注入全部启用 Skill；[]=不注入；[…]=仅这些），
  `SkillService.render_catalog()` 支持按名称过滤——支撑定时任务的 Skill 范围限定
- `main.py` 把 `HookManager` 注册进服务注册表，供定时任务复用同一份钩子（权限 / 落盘 / 追踪对任务运行同样生效）
- 冒烟脚本新增定时任务检查（可选项、新建、立即执行、停用、运行历史），并纳入自清理

---

## [0.0.11] - 2026-09-28

### 新增

#### MCP 远程 SSE 连接（在 stdio 之外）
- MCP 客户端新增 **HTTP + SSE 传输**（`MCPSSEConnection`）：`GET <url>` 建立事件流 → 服务器下发 `event: endpoint` → 请求 POST 到该地址、响应经事件流回传；同时兼容「POST 直接返回 JSON-RPC」的 streamable-http 形态
- 配置格式支持 `url` / `type: "sse"` / `description` / `disabled`（Claude 风格），并可带自定义 `headers`；类型也可从「有 url 无 command」自动推断
- 抽出传输无关的协议基类（握手 / tools.list / tools.call / 请求-响应配对），stdio 与 SSE 共用
- 兼容性修复（真实远端验证发现）：① 通知类消息（无 id）被部分网关以 404 拒绝 → 通知改为 fire-and-forget 容忍失败；② 网关在复用 keep-alive 连接时会使会话失效 → SSE 的 POST 一律使用新连接 + `Connection: close`
- MCP 工具风险等级智能判定：名字以 `get/query/list/search/read/fetch/...` 开头视为 `read`（免确认），其余保守为 `write`
- 前端设置页 MCP 标签新增「本地进程 (stdio) / 远程服务 (SSE)」切换与 URL、请求头、描述输入
- 已用真实远端 server 验证：连接成功、发现 9 个广告数据工具，并成功取回真实汇总数据

#### 问答对操作（复制 / 物理删除 / 追问）
- 每条提问：**复制提问**、**删除本轮**（物理删除该提问及其回答，含工具消息，直到下一条提问为止，保证上下文自洽）
- 每条回答：**复制回答**、**追问**（把该问答作为引用带入输入框）、**从此处分叉**
- 后端新增 `DELETE /api/sessions/{id}/messages/{message_id}?with_turn=true`（整轮物理删除）与 `SessionService.delete_turn/delete_message`

#### 会话导出 / 导入 / 分叉（交互重做）
- 侧边栏改为**可见的「⋮」会话操作菜单**（右键同样可用），每项都带一句说明：重命名 / 导出为 JSON / 复制为新会话（分叉）/ 归档 / 删除；页头新增操作提示与「导入」按钮
- 导出/分叉/导入成功后给出轻提示；导入前校验文件结构并给出明确错误
- 修复业务逻辑缺陷：**分叉截断点落在「带 tool_calls 的 assistant 消息」上时，会把其后的 tool 消息一并带上**（否则新会话上下文非法，下一轮必被 API 拒绝）

#### 长期记忆：定时自主总结
- 新增 `MemorySummarizer`：后台按间隔（默认 1800s，`HARNESS_MEMORY_SUMMARY_INTERVAL`）扫描最近若干活跃会话，把「上次总结之后的新增问答」交给大模型压缩成长期记忆（默认最多 6 条/会话）
- 会话级游标（`data/memory_state.json`）保证**增量**，不重复消耗 token；记忆按 key upsert，幂等
- 优雅降级：无可用 provider / 无 API Key 时跳过并给出原因；可用 `HARNESS_MEMORY_SUMMARY=0` 关闭
- REST：`GET /api/memories/summary-info`、`POST /api/memories/summarize`（手动触发）；前端「记忆」标签新增自动总结卡片（状态 + 间隔 + 立即总结）
- 已用真实实例验证：扫描 2 个会话、总结 2 个、写入 6 条记忆

### 修复
- **AI 工具确认弹窗样式**：`--bg-card` / `--bg-primary` 两个 CSS 变量**并不存在** → 弹窗背景解析为透明，文字与按钮混在一起；`.btn-primary` / `.btn-ghost` 此前只在 SettingsView 的 scoped 样式里定义 → ChatView 里的按钮完全没有样式。已把通用按钮类提升到全局 `style.css`，并把弹窗改为实色背景 + 明显边框/阴影 + 风险徽标配色 + 分区布局
- **运行轨迹**：展开区新增纵向滚动条；`span 数` 与实际步骤不一致（统计里含整轮 run span）→ 后端新增 `step_count`（不含 run），前端显示「步骤 N · span M」；时间显示差 8 小时（后端存 UTC、前端原样展示）→ 后端统一输出带时区偏移的 UTC 时间戳，前端新增 `formatLocalTime` 转本地时区
- **测试隔离**：`tests/conftest.py` 增加 `MCP_CONFIG_PATH` 隔离（此前测试会读取项目 `mcp.json` 并尝试连接真实远端 server，慢且依赖网络）

### 变更
- 新增 `backend/mcp.json`（示例/默认连接 `youth-mcp`）
- `mcp.json` 加入 `.gitignore`（可能含鉴权 header）
- 补充公开示例配置 `mcp.example.json`（`youth-mcp` 替换为公开可用的 libgen 服务）

---

## [0.0.10] - 2026-09-28

### 修复（全量自检发现的缺陷）

本次对 P0/P1 全部新功能做了一次端到端自检（真实库污染 / 业务逻辑 / 前端细节 / 任意 bug），
共发现并修复 8 处问题：

- **【严重｜前端】`src/` 下 30 个陈旧 `.js` 影子文件导致新代码不生效**：`frontend/src/` 里残留了一批
  编译产物（`i18n/zh.js`、`stores/*.js`、`api/client.js`、`*.vue.js` 等，时间戳停在 09-27 23:31）。
  Vite 解析无扩展名 import 时按 `.mjs → .js → .mts → .ts …` 顺序命中，**`.js` 优先于 `.ts`**，
  于是 `import zh from '../i18n/zh'` 实际加载的是过期的 `zh.js`——本轮新增的 i18n 文案根本没进构建产物
  （已实测：修复前 `dist` 中 `MCP 客户端/输出制品/运行轨迹/长期记忆` 命中数均为 0）。修复：备份并删除
  全部 30 个 `.js`，`tsconfig.app.json` 显式 `"noEmit": true`，`.gitignore` 增加 `frontend/src/**/*.js`
  兜底。修复后重建，新文案全部入包（bundle +17KB）。
- **【严重｜数据】冒烟测试污染真实开发库**：手工冒烟脚本对**真实库**（`backend/data/harness.db`）写入并在异常
  路径遗留了 6 个测试会话（4×`冒烟测试会话`、`New Session`、`dbg`）。修复：先备份库、精确删除这 6 条
  （并确认无孤立 messages/snapshots），并把冒烟流程改为**一次性临时库**（`HARNESS_DB_PATH=.smoke-test.db`）
  起服务，从机制上杜绝污染（已验证：跑完临时库各表均为 0，真实库仅剩用户自己的 35 个会话）。
- **【健壮性】MCP 连接断开后无法重连**：`MCPServerConnection._closed` 在 `connect()` 未复位，重连时读取循环
  会立即退出。已修复并加回归测试。
- **【正确性】长期记忆检索的 LIKE 通配符注入**：查询里的 `%` / `_` 会被当通配符，`search("%")` 命中全部记录。
  已转义（`ESCAPE '\'`）并加回归测试。
- **【健壮性】工件 / span 表无上限增长**：落盘的 artifacts 与 traces 的 spans 会无限累积。新增保留上限
  （`HARNESS_ARTIFACTS_MAX=500`、`HARNESS_SPANS_MAX=20000`，可调，0=不限），超出按 `rowid`（插入顺序）
  清理最旧记录；artifact 连带删除落盘文件。已加回归测试。
- **【健壮性】AgentLoop 运行环境（ContextVar）泄漏**：`reset_runtime` 原挂在尾部 `try/finally`，遇
  `CancelledError`（BaseException，`except Exception` 捕不到）时不会复位。已改为在主逻辑块的 `finally` 复位。
  已加回归测试。
- **【性能】`GET /api/traces/{id}` 全表扫描**：原实现拉取最近 1000 条 trace 再筛选，改为针对该 trace 的定向
  聚合查询。
- **【前端细节】窄屏排版**：轨迹泳道图行、制品/记忆/轨迹卡片头在窄屏下改用更紧凑的列宽并允许换行。

### 变更
- `frontend/tsconfig.app.json` 显式 `noEmit`；`.gitignore` 增加 `frontend/src/**/*.js` 与 `backend/.smoke-test.db`。

---

## [0.0.9] - 2026-09-28

### 新增

#### MCP（Model Context Protocol）客户端
对齐 Goose（MCP-first）/ Claude Code / Cline / OpenHands —— 通过 MCP 打通整个工具生态：
- 新增 `mcp_client`（service 插件）：**零第三方依赖**的 stdio 传输客户端（换行分隔 JSON-RPC 2.0，协议版本 `2024-11-05`），支持握手、`tools/list`、`tools/call`、按 server 缓存工具清单
- 启动时读取 `mcp.json`（项目根）或 `~/.andy-harness/mcp.json`（可用 `MCP_CONFIG_PATH` 覆盖，多个以 `;` 分隔），自动连接所有 `enabled` server
- 每个远端工具自动注册进 ToolRegistry，命名 `mcp__{server}__{tool}`，AI 可直接调用；插件停用时自动注销并断开子进程
- REST：`GET /api/mcp/servers`、`GET /api/mcp/tools`、`POST /api/mcp/refresh`（重连）、`POST /api/mcp/servers`（新增并连接）、`DELETE /api/mcp/servers/{name}`
- 前端：设置页新增「MCP」标签页，展示每个 server 的连接状态、启动命令与已注册工具，支持一键重连与增删 server
- 测试：新增 13 项（客户端连接/调用/错误处理/配置解析、插件注册与执行、REST 全链路含真实子进程 echo server），全量 256 项通过

#### 大工具输出落盘（Context Offload）
对齐 Deep Agents 的虚拟文件系统 / OpenAI Agents SDK 的 offload large tool outputs：
- 新增 `artifact_store`（service 插件 + `post_tool_call` 钩子）与 `tool_read_artifact`（`read_artifact` 工具）
- 工具输出超过阈值（默认 **6000 字符**，可 `HARNESS_OFFLOAD_THRESHOLD` 调整）时自动落盘到 `workspace/artifacts/`（可 `HARNESS_ARTIFACTS_DIR` 覆盖），上下文里只保留「路径 + 摘要 + artifact_id」，显著降低 token 占用
- `read_artifact` 支持 offset/limit 分页回读全文，单次上限 2 万字符，避免再次撑爆上下文；对 `read_artifact` 自身结果不再二次落盘
- 新增 `artifacts` 表（`CREATE TABLE IF NOT EXISTS`，旧库自动升级无需迁移）记录工件元数据
- REST：`GET /api/artifacts`、`GET /api/artifacts/{id}`（支持分页）、`DELETE /api/artifacts/{id}`
- 前端：设置页新增「制品」标签页，可查看落盘输出的摘要与全文、删除单个制品
- 测试：新增 15 项（服务、钩子、工具、AgentLoop 端到端、REST），全量 271 项通过

#### 长期记忆（跨会话记忆）
对齐 Letta 分层记忆 / OpenAI Agents SDK built-in memory / Deep Agents store：
- 新增 `memory_manager`（service 插件）、`tool_memory`（`memory_save` / `memory_search` 工具）与 `memories` 表（`embedding` 列预留给未来向量检索）
- `global` 作用域跨所有会话共享、`session` 作用域仅当前会话可见；相同 `(scope, session_id, key)` 保存即覆盖（幂等）
- AgentLoop 在 system 前缀自动注入最近的全局记忆（最多 8 条、单条截断 200 字符），新会话即可"记得"此前保存的偏好与事实
- REST：`GET /api/memories`（列出/按关键词检索）、`POST /api/memories`（保存）、`PATCH /api/memories/{id}`、`DELETE /api/memories/{id}`
- 前端：设置页新增「记忆」标签页，支持搜索、新增、编辑、删除，并区分全局/会话作用域
- 测试：新增 13 项（服务、工具、AgentLoop 注入、REST），全量 284 项通过

#### 可观测性 Tracing（运行轨迹）
对齐 OpenAI Agents SDK tracing / Pydantic AI(OTel) / LangSmith：
- 新增 `tracing`（service 插件）与 `spans` 表；引擎在已知计时点发布 `trace.span` 事件（run / model / tool 三类 span，含耗时、prompt/completion/total token、输入输出摘要、状态与错误）
- 一次对话 = 一条 trace，包含整轮 run span、每次模型调用与每次工具调用 span，形成完整 span 链
- 为让引擎发布事件，`main.py` 将 loader 的 EventBus 注册进服务注册表（幂等）；EventBus 未注册或无订阅者时发布为空操作，对主流程零影响
- REST：`GET /api/traces`（列出轨迹）、`GET /api/traces/{trace_id}`（span 链）、`DELETE /api/traces/{trace_id}`
- 前端：设置页新增「轨迹」标签页，展开可见泳道图（按耗时比例绘制模型/工具条）+ span 明细（耗时、token、输入输出、错误）
- 测试：新增 6 项（服务聚合、插件订阅落库、AgentLoop 端到端 span 链、REST），全量 290 项通过

#### 子代理委派（Subagent）
对齐 Claude Code Sub-agent delegation / Deep Agents subagent spawning：
- 新增 `subagent`（service 插件）与 `tool_task`（`task` 工具）；主代理可把一个自包含子任务交给子代理在**独立临时会话**中执行，只把最终摘要回传
- 子代理复用现有 `AgentLoop`（含上下文压缩、钩子、权限、offload），工具集默认排除 `task` 自身以防无限递归；子代理会话用完即删，不留痕迹
- 新增 `harness/engine/runtime.py`：用 `ContextVar` 承载当前运行环境（provider/model/session 等），让 `task` 工具能拿到本轮 provider，且并发会话互不干扰
- 子代理运行期间抑制其流式 token 推送到主界面，避免子代理中间过程混入主对话
- 测试：新增 6 项（服务、工具、端到端上下文隔离），全量 296 项通过

### 修复
- **`PluginLoader` 不支持重复启动**：同一进程内重复进入 lifespan（uvicorn `--reload`、测试多次 `TestClient(app)`）时，插件因「已加载」而被跳过激活，导致其注册的工具/服务/钩子不再生效；`load_and_activate_all` 现支持**幂等再激活**（已加载但未激活则重新激活）
- **`HookManager` 丢失累积元数据**：钩子管线执行完毕后返回时未带上各处理器累积的 `metadata`，调用方无法读取；现连同累积 metadata 一并返回
- **设置页缺失样式类**：`.btn-sm` / `.btn-xs` / `.btn-danger` / `.section-desc` / `.empty-state` 此前只有引用、没有定义；已补齐，避免按钮尺寸与空态样式穿帮

---

## [0.0.8] - 2026-09-27

### 新增

#### 能力增强清单
- 新增 `ENHANCEMENTS.md`：调研 16 个主流开源 / 商用 Agent Harness 项目（Claude Code、OpenAI Codex/Agents SDK、OpenHands、Aider、Cline、Goose、OpenCode、Pi、DeepSeek Harness、Deep Agents、LangGraph、Letta、Microsoft Agent Framework、CrewAI、Pydantic AI、Bedrock AgentCore），归纳 14 项可借鉴能力并标注来源、价值与实施优先级

#### 工具权限分级与人工确认（Human-in-the-loop）
把模型生成的工具调用当作**不可信输入**处理（对齐 Cline / Claude Code / Codex / Microsoft Agent Framework）：
- 新增 `permission_manager`（service 插件）与 `ToolPlugin.risk_level` 契约字段（read / write / dangerous，未声明默认 write）
- 四档全局策略：全部放行 / **仅危险操作需确认（默认）** / 写与危险需确认 / 全部需确认；支持按工具单独覆盖为自动放行、需确认或拒绝
- AgentLoop 在工具执行前拦截；需确认时通过回调暂停等待用户决定，超时（120s）按拒绝处理；无接入层回调时安全默认亦为拒绝
- 被拒绝的工具调用同样回填 tool 消息，避免会话末尾残留「带 tool_calls 却无 tool 响应」的消息导致下一轮 API 400
- WebSocket 新增 `confirm_request` / `confirm_reply` / `confirm_timeout` 帧；前端聊天页弹出确认框（展示工具名、风险等级、参数），可「允许执行 / 拒绝」
- REST：`GET|PUT /api/permissions`、`PUT /api/permissions/{tool}`；前端设置页可配置全局策略与单工具覆盖
- 已为现有工具标注风险等级：`code_runner` = dangerous，`theme_switcher` = write，其余为 read
- 测试：新增 24 项（策略决策、覆盖优先级、持久化、AgentLoop 拦截、WS 确认全链路、REST），全量 204 项通过

#### 规划与 Todo 追踪（todo_write）
对齐 Deep Agents `write_todos` / Claude Code TodoWrite / MS Agent Framework todo tracking：
- 新增 `todo_manager`（service 插件）与 `tool_todo`（工具插件）；AI 可用 `todo_write` 拆解多步骤任务并跟踪进度
- 会话级持久化（新增 `todos` 表，`CREATE TABLE IF NOT EXISTS`，旧库自动升级无需迁移）；采用覆盖式写入，语义幂等
- 新增 `ToolPlugin.needs_session` 契约字段：声明后 AgentLoop 会在调用前注入 `session_id`（默认关闭，不影响既有工具）
- REST：`GET /api/sessions/{id}/todos`、`DELETE /api/sessions/{id}/todos`
- 前端：聊天页顶部任务面板，显示进度（已完成 / 总数）与每项状态（待开始 / 进行中 / 已完成），切换会话自动同步，支持一键清空
- 测试：新增 18 项（服务、工具、AgentLoop 端到端、REST），全量 222 项通过

#### Skills 系统（SKILL.md 渐进式披露）
对齐 Agent Skills 开放标准（Claude Code / Codex / Cursor / Copilot / Gemini 等 40+ 客户端采纳）：
- 新增 `skill-manager`（service 插件）与 `tool_use_skill`（工具插件），扫描三处目录：内置 `backend/skills/`、用户级 `~/.andy-harness/skills/`、插件自带 `plugins/*/skills/`
- 三级渐进式披露：L1 仅 name + description 常驻 system prompt（约 100 token/个）；L2 命中后由 `use_skill` 工具加载正文；L3 `scripts/ references/ assets/` 按需读取，含路径越界防护
- AgentLoop 在上下文前缀注入 Skill 目录；Skill 服务不可用时优雅降级，不影响主对话
- REST：`GET /api/skills`、`GET /api/skills/catalog`、`GET /api/skills/{name}`、`PATCH /api/skills/{name}`（启停）、`POST /api/skills/reload`、`GET /api/skills/{name}/resource`
- 前端：设置页新增「Skills」标签页，可查看 Skill 详情与正文、启停单个 Skill、一键重新扫描
- 内置 3 个示例 Skill：`code-review`、`git-commit-message`、`skill-authoring`（含 references 资源示例）
- 测试：新增 38 项（服务/工具/REST/AgentLoop 注入/端到端工具调用），全量 180 项通过

#### 会话导出 / 导入与分支 fork
对齐 Pi（会话分支）/ Aider（git-native 可回退）/ OpenCode（会话持久化）：
- 新增 `SessionService.export_session` / `import_session` / `fork_session`；导出包含会话元数据 + 全量消息 + 任务清单（version=1，可移植 JSON）
- REST：`GET /api/sessions/{id}/export`、`POST /api/sessions/import`、`POST /api/sessions/{id}/fork?at_message_id=`；`PUT /api/sessions/{id}/todos`（覆盖写入，剥离来源 id 防主键冲突，供前端/导入回填）
- 导入为新会话、绝不复盖现有数据；fork 从指定消息（或开头）截断历史并继承
- 前端：会话侧边栏右键菜单新增「复制为新会话 / 导出 / 导入」，顶栏新增「导入」按钮；导出文件以会话标题自动命名
- `Message.to_dict` 导出补 `tool_call_id`（否则导入后工具消息关联丢失）；导出/导入重建 Todo 记录 id 避免撞主键
- 测试：新增 21 项（导出结构、消息/工具调用保留、含任务、导入幂等、fork 截断、互不影响、404、PUT 写入），全量 243 项通过

---

## [0.0.7] - 2026-09-27

### 新增

#### Token 用量展示（DeepSeek 官方 usage API）
- 流式请求加入 `stream_options: {"include_usage": true}`，透传响应末块的 usage 用量
- DeepSeek Provider 覆盖的解析方法补上 usage 透传（处理思维链字段时不丢失用量）
- AgentLoop 聚合多轮模型调用的 usage（prompt / completion / total 及缓存命中明细），终答消息持久化 tokens
- WebSocket done 帧携带 usage，前端在每条 AI 回答下方显示「本次用量」胶囊（输入 / 缓存 / 未命中 / 输出 / 合计）
- 数据库路径支持 `HARNESS_DB_PATH` 环境变量覆盖（优先级：显式参数 > 环境变量 > 默认路径）
- pytest 测试数据库隔离（tests/conftest.py 指向临时库），避免测试污染真实数据

#### 聊天体验
- 流式回复中上滑查看历史即停止自动滚动，聊天区固定显示「向下」按钮
  - 流式时：点击直接回到最新文字
  - 非流式时：每点击一次跳到下一条提问的位置
- 切换会话时自动回到底部

#### 工具调用展示修复
- 历史消息中的 tool_calls 由 OpenAI API 原始格式（id/type/function）转换为执行结果格式（tool_name/args/result/error）返回前端，工具卡片完整展示工具名、参数与执行结果
- `role=tool` 消息不再单独返回渲染（其执行结果已并入 assistant 消息的工具卡片），消除孤立的工具图标行
- 空正文的 assistant 消息（纯工具调用轮次）不再渲染空白气泡

#### 会话与启动脚本
- 修复：问答成功后再次点击「新建会话」误判存在空会话的问题——点击前实时刷新会话列表，按最新 `message_count` 判断，仅真正存在空会话时跳转并提示「已有新会话，请提问」
- start-all.bat 顺序启动：等待后端 8000 / 前端 5173 端口就绪后再打开浏览器，消除启动瞬间 Vite ws proxy ECONNREFUSED 报错
- stop-all.bat 先按窗口标题关闭服务窗口（含进程树），再按端口清理，避免 uvicorn `--reload` 孤儿进程残留
- 默认主题改为亮色（light）

### 待完成
- 插件从 zip/git 安装
- 会话分支与导出（Markdown/JSON）
- Docker 沙箱后端
- 桌面壳（Tauri）
- 多 Agent 协作
- 远程 channel（飞书/Telegram）

---

## [0.0.6] - 2026-09-27

### 新增

#### Jev 结构化决策
- Jev Manager 服务插件（jev_manager）：封装 TypeSafe AI Jev API，支持三种原语
  - Choice（选择型）：从固定选项中选一个，返回概率分布和置信度
  - Score（评分型）：在自定义等级上打分，返回概率分布和置信度
  - Noul（是非型）：判断命题是否成立，返回 0~1 概率值
- 三种原语可混合并行提问，独立计算、同时返回
- 插件配置 schema（api_key + base_url），支持设置页面配置
- 环境变量 JEV_API_KEY 回退支持
- 未配置 API Key 时插件仍可激活，调用工具时返回友好错误提示

#### Provider 状态管理增强
- Provider 列表返回 `enabled` 字段，前端可实时显示启用/停用状态
- ProviderUpdate 接口新增 `enabled` 字段，支持通过 API 启用/停用 Provider
- Provider 启用前自动测试连接，连接失败则不启用并显示错误原因
- 停用 Provider 后：模型从模型选择器隐藏、Token 计数跳过、聊天不可调用
- 内置 Provider 修改的密钥和启用状态持久化到数据库，重启后保留
- 内置 Provider 不可删除（仅可停用），自定义 Provider 可删除
- 测试连接接口返回具体错误信息，便于前端展示失败原因
- ProviderRegistry 新增 `_config_overrides` 机制：内置 Provider 插件激活时自动合并数据库中的密钥和启用状态覆盖项

#### 插件市场体系
- 内置插件市场目录 `backend/marketplace/`，服务端直装端点 `POST /api/plugins/marketplace/{plugin_id}/install`
- `PluginManifest` 新增 `source` 字段：`system`（系统内置）/ `marketplace`（市场安装），持久化到 plugin.json
- 插件管理页新增「从插件市场安装插件」入口：市场卡片含「说明」弹窗（long_description）与「安装」按钮，已安装自动标记
- 卸载保护：仅 `source=marketplace` 的插件可卸载（UI 与 API 双重校验），系统/核心插件不可删除；卸载后可从市场重新安装

#### 元气宠物（Virtual Pet）
- 作为插件市场的首个插件提供，安装后在插件管理中启用即可生效（前后端插件联动）
- 全局悬浮宠物（overlay 机制）：在任意页面自由走动，鼠标悬停随机卖萌（表情/动作/音效）
- 养成系统：三围（饱食/心情/精力）随时间衰减，喂食/玩耍/抚摸/睡觉交互，成长阶段变化体型与颜色
- 本地存档（localStorage，离线折算衰减），粒子特效与 WebAudio 音效

#### 前后端插件联动
- UI 插件通过 `backend_plugin_id` 映射后端插件，启用/停用双向同步，刷新后保持一致
- 后端插件不存在（如市场插件被卸载）时自动停用对应前端 UI 插件
- 前端插件 `contributes` 新增 `overlays`（全局悬浮层），由 PluginOverlayHost 渲染

#### 会话与启动脚本
- 修复：删除/归档会话后自动加载新选中会话的消息（右侧不再空白）
- 重命名图标改为彩色铅笔 ✏️，与删除图标区分
- 新增 start-all.bat / stop-all.bat：一键启动/停止前后端服务，stop 执行后自动关闭 start 打开的全部窗口

### 待完成
- 插件从 zip/git 安装
- 会话分支与导出（Markdown/JSON）
- Docker 沙箱后端
- 桌面壳（Tauri）
- 多 Agent 协作
- 远程 channel（飞书/Telegram）

---

## [0.1.0] - 2026-09-24

### 新增

#### 核心（P0-P1）
- monorepo 骨架（backend + frontend + docs）
- FastAPI 后端 + Vue 3 前端最小应用
- 内核：EventBus（async pub/sub + AMQP 风格通配符）
- 内核：ServiceRegistry（stack 回退策略 + 线程安全）
- 内核：HookManager（有序管线 + 改写 + 短路 + metadata 传播）
- 内核：PluginLoader（发现/校验/加载/激活/停用/核心插件保护）
- 内核：PluginContext 受限门面
- 内核：TokenCounter 契约接口
- 示例插件 hello-plugin

#### 模型层（P2）
- OpenAICompatibleProvider 基类（流式解析/超时/指数退避重试/统一错误）
- DeepSeek Provider 插件（v4-flash/v4-pro/chat/reasoner，含 reasoning_content 思维链）
- Qwen Provider 插件
- Doubao Provider 插件
- ProviderRegistry 统一管理（实例缓存 + 配置更新清除缓存）
- API Key Fernet 加密存储（machine key 派生）
- 环境变量 DEEPSEEK_API_KEY 回退
- health_check 使用 GET /models 端点（不消耗 token）
- TokenCounter tiktoken 估算（类级缓存编码器）

#### 会话/上下文（P3）
- SQLite 数据模型（sessions/messages/context_snapshots/providers/settings/plugins）
- session-manager 插件（CRUD/重命名/归档/消息追加）
- context-manager 插件（滑动窗口/摘要压缩/消息钉住/系统提示词模板）
- tool_call_id 全链路传递
- 孤立 tool 消息清理（压缩前后两次过滤）
- content=null 处理（API 规范）
- 上下文快照持久化

#### Agent 引擎（P4）
- AgentLoop 核心循环（装配上下文 → 调模型 → 解析 tool_calls → 执行 → 回填）
- ToolPlugin 契约 + 工具注册表
- 钩子管线全量接入（7 个钩子点）
- 流式 tool_calls 分片按 index 累积合并
- 带工具调用的 assistant 消息持久化（API 规范）
- 失败轮次自动清理（防止上下文错乱）
- 系统时间注入
- 用户自定义 system_prompt 注入
- 模型调用指数退避重试（默认 3 次）
- 停止/中断生成
- 内置工具：计算器、当前时间

#### API 层（P5）
- REST API（sessions/messages/providers/models/settings/plugins）
- 插件安装接口 POST /api/plugins/install
- 插件卸载接口 DELETE /api/plugins/{id}
- WebSocket /ws/chat（流式/工具事件/停止/自动标题）
- 统一错误格式 {code, message, detail, trace_id}
- 核心插件保护（API 层拒绝停用核心插件）

#### 前端（P6-P7）
- Chat 页面（会话侧栏/消息列表/流式渲染/停止生成）
- Markdown 渲染（markdown-it + highlight.js，流式内容也支持）
- 思维链展示（逐步流式展示，完成后折叠）
- WS 客户端（断线重连/单连接管理/避免 token 重复）
- 模型选择器
- 设置页面（Provider 管理/会话级设置/插件管理/通用设置）
- 多主题系统（matrix/ocean/sunset/dark/light，CSS 变量切换）
- AI 主题切换（tool-theme-switcher 工具 + 前端指令监听）
- 插件在线安装表单
- 插件卸载按钮
- 插件列表滚动条
- 会话标题自动生成（AI 总结，不超过 20 字）
- 会话标题截断显示（hover 显示完整标题）
- 设置按钮（header 齿轮图标跳转）
- Matrix 数字雨首页（Canvas 0/1 瀑布雨 + 鼠标弯曲）
- 微交互（涟漪效果/右键菜单/长按）
- 国际化（中文/英文）
- 前端插件内核（UIPluginContext/PluginLoader/EventBusBridge）

#### 沙箱（P8）
- LocalSubprocessBackend（子进程/超时/输出截断/资源限制/黑名单/进程树终止）
- 平台适配（Linux/macOS resource 模块 + Windows psutil）
- 预装包白名单（requirements-sandbox.txt）
- tool-code-runner 插件

#### 工具插件
- tool-web-search（百度搜索引擎）
- tool-web-fetch（网页正文抓取）
- tool-theme-switcher（AI 主题切换）
- tool-ip-lookup（IP 地址地理位置查询）

#### 文档
- 架构设计文档（01-architecture.md）
- 分阶段开发计划（02-development-plan.md）
- 插件开发指南（docs/plugin-dev-guide.md）
- README 打磨版（徽章/快速开始/使用指南/FAQ）
- Docker 部署（docker-compose.yml + Dockerfile）
- 贡献指南（CONTRIBUTING.md）
- Issue/PR 模板

### 修复
- DeepSeek API 422 "missing field tool_call_id" — tool_call_id 全链路传递
- DeepSeek API 400 "Messages with role 'tool' must be a response to a preceding message with 'tool_calls'" — 孤立 tool 消息清理
- 流式 tool_calls 参数不完整 — 分片按 index 累积合并
- Web 搜索 "未提供搜索关键词" — 分片累积修复后的连带修复
- 思维链显示为每行一个词 — streamingReasoning 从数组改为拼接字符串
- 回答内容字符重复 — WS 单连接管理（connect 前关闭旧连接 + onMessage 返回取消注册函数）
- Markdown 未渲染 — 流式内容改用 MarkdownRenderer 组件
- 失败轮次上下文错乱 — 异常时清理残留的未完成消息
- content 为 None 导致 len() 报错 — 所有 count_tokens 调用使用 `or ""` 回退
- system_prompt 未传递到模型 — WSMessage/AgentLoopConfig 添加 system_prompt 字段
