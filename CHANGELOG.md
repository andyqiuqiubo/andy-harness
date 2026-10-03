# Changelog

本项目的所有重要变更都记录在此文件中。

格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/)。

---

## [1.0.0] - 2026-10-03（开发者预览版）

首个对外版本号。自 `v0.0.9` 起积累了 0.0.10 ~ 0.0.48 共 39 个补丁版本的演进，
本条目做**精简汇总**（完整逐条记录见下方 0.0.10 ~ 0.0.48 各节）。

### 新增（相对 v0.0.9 的主要能力）
- **MCP 客户端**：stdio / SSE 双传输，配置 `mcp.json` 自动连接并注册远端工具；
  兼容旧版 SSE 与 streamable-http 两种形态（GET 405 时自动降级 POST 直连）。
- **MCP 市场**：libgen / deepwiki / context7 等免鉴权公开服务一键接入。
- **长期记忆 + 定时自主总结**：按间隔把新增问答压缩为长期记忆；默认零依赖离线
  向量检索（可配 `HARNESS_EMBEDDING_MODEL` 走 OpenAI 兼容 `/embeddings`）。
- **运行轨迹 Tracing**：span 链落库 + 泳道图，可看耗时 / token / 错误。
- **子代理委派与多 Agent 编排**：`task` 委派、`parallel` 并发、`pipeline` 角色流水线。
- **定时任务**：每天 / 每周 / 固定间隔 / 一次性四种调度，按任务勾选开放工具集，
  在隔离会话中执行并记录运行历史。
- **制品库**：超大工具输出自动落盘（阈值 `HARNESS_OFFLOAD_THRESHOLD`，默认 6000 字符），
  上下文只留摘要与引用。
- **Skills 技能系统 + 技能市场**：对齐 SKILL.md 开放标准，三级渐进式披露。
- **Computer Use 桌面操控**、**多渠道接入（Webhook / Telegram）**、**Jev 结构化决策**、
  **Docker 沙箱**、**结构化上下文压缩 compaction v2**、**Eval 回归评测框架**、
  **Tauri 桌面壳**、**插件 / 技能外部分发安装（zip / git）**。
- **可选认证与多用户**（`HARNESS_AUTH=1`）：PBKDF2 + HMAC 签名，会话 / 记忆按
  `user_id` 隔离，前端自动出现登录门。

### 修复（1.0.0 发布前的安全与稳定性收口）
- 附件 / 制品 / 轨迹按 `user_id` 归属校验（多用户场景生效，单用户行为不变）。
- WebSocket 单 reader 架构重构，消除 stop / 人工确认回复被并发读取抢帧的问题。
- 认证开启后图片附件改用 fetch + `blob:` 渲染，解决 `<img>` 不带 Bearer 的 401 破图。
- 权限检查异常日志由 debug 升 error（fail-open 不再静默）。
- 认证签名密钥持久化到 `data/auth_secret.txt`（0600），重启后已签发 token 不再失效。
- 前端设置接通 `/api/settings`（此前只写 localStorage）；修复「持久化的主题只在
  访问过设置页后才生效」——main.ts 启动时即实例化设置 store。

### 文档
- 新增《andy-harness v1.0.0 用户使用手册》（16 张界面截图）。
- 新增《项目面试解说》与《发布 v1.0.0 操作清单》。
- README 订正两处不实描述（「设置页可查看渠道状态」「聊天页开启 Computer Use 开关」），
  并为渠道 / Computer Use / Jev / 断点续跑 / 用户管理标注「仅 REST，暂无界面入口」。

### 已知限制（预览版）
- 渠道 / Computer Use / Jev / 断点续跑 / 用户管理**暂无界面入口**，仅 REST API。
- 仅 Windows 10 已验证；仅 DeepSeek `deepseek-v4-flash` 做过深度联调。
- 完整清单见《发布 v1.0.0 操作清单》第九节与《发布前全项目分析报告》。

### 发布后修订（2026-10-01 安全与工程收口）

在 1.0.0 发布基础上补充的 hardening（单用户默认部署行为不变）：

- **安全加固**：插件 / 技能 / MCP 的安装卸载、权限模式与系统设置写入等**状态变更类端点**统一接入 `require_admin` 依赖；仅当 `HARNESS_AUTH=1` 时校验管理员身份，关闭认证时透传，单用户默认行为零变化。
- **后端默认仅本机监听**：`Makefile` 与 `start-all.bat` 的后端启动由 `0.0.0.0` 改为 `127.0.0.1`；`docker-compose.yml` 端口绑定 `127.0.0.1` 并去除过时的 `version` 字段，强化本地部署不被局域网直连。
- **设置落盘路径修正**：用户设置文件由错误的 `backend/harness/data/settings.json` 修正为 `backend/data/settings.json`（与文档一致），并迁移既有配置、清理错误目录。
- **Docker 镜像补齐**：`backend/Dockerfile` 现 COPY `marketplace/` `skills/` `mcp_marketplace/`，解决此前容器内市场空白的问题。
- **CI 全绿**：后端 `ruff check` / `ruff format --check` / `mypy harness` / `pytest` 均通过；前端 `eslint` / `vitest`（43 用例）/ `vue-tsc` / `vite build` 均通过，`ci.yml` 前端 job 新增 `pnpm test` 步骤。

### 文档同步（2026-10-01 全面复核后）

以「重新跑一遍全部门禁 + 逐文件核对源码」的方式把全部说明性文档与当前代码对齐：

- **README**：修正 FastAPI 徽章版本（0.115 → 0.141）、统一模型名 `deepseek-v4.1-flash` → `deepseek-v4-flash`、合并重复的「会话导出/导入/分叉」条目、订正 Provider API Key 的读取方式（**仅内置 DeepSeek 支持 `DEEPSEEK_API_KEY` 回退，Qwen / Doubao 只能从设置页读取**）、重写项目结构为真实目录（补 `marketplace/` `skill_marketplace/` `mcp_marketplace/` `skills/` `scripts/`、模块数 18、插件数 33、测试 593）、新增质量门禁现状与「质量与审计记录」文档索引、补充 Docker 前端未配反向代理与桌面壳数据目录仅覆盖两项的**已知限制**说明。
- **用户使用手册 §12**：环境变量由 15 条补齐为**全表**（按运行与存储 / 认证与多用户 / 调度与记忆 / 渠道 / Key 回退 / 桌面壳 / 开发测试七组，含默认值的真实代码来源）；数据文件表补 `attachments/`、`computer_use_workspace/`、沙箱临时工作区，并给出备份建议。
- **`01-architecture.md`**：新增「三套版本号不要混淆」说明（产品版本 1.0.0 / 内核 API 版本 0.1.0 / 插件版本 0.1.0）与升级 `CORE_API_VERSION` 的风险提示；测试规模由「20+ 测试文件」更正为 59 个测试模块 / 593 个用例。
- **`02-development-plan.md`**：测试数由 141 更正为「593 后端 + 43 前端」，DoD 补充完整门禁命令。
- **`CONTRIBUTING.md`**：新增「安全红线」（禁提交密钥与运行时数据、不得绕过 `require_admin`、不留调试产物、沙箱/权限/认证改动必须补测试、示例配置不写真实路径）与「版本号约定」两节；补 `pnpm test` 到提交前检查；修正克隆地址。
- **`frontend/README.md`**：由 Vite 模板原文改写为正式前端开发说明（命令、目录结构、8 条约定、已知限制）。
- **`docs/RELEASE-v1.0.0.md`**：2.1 版本号表改为「已统一」并列出 7 处实际位置 + 刻意保持 `0.1.0` 的两处；新增「发布前门禁实测」与「Release Notes 必须披露的已知问题」两节。
- 新增 `docs/ANALYSIS-v1.0.0-2026-10-01.md`（全项目分析报告）与 `docs/security-isolation-analysis.md`（安全隔离技术分析），并在 README 建立索引。

---

## [0.0.48] - 2026-09-30

依据 `docs/AUDIT-2026-09-30.md` 复核后落地的修复与完善（仅多用户 / 认证启用场景生效，单用户默认部署行为不变）。

### 修复
- **P0-1 附件跨用户读写**：`api/rest/attachments.py` 的 `upload_attachments` /
  `serve_attachment` 在启用认证时按 `request.state.user` 两跳校验归属，他人会话
  附件返回 404；单用户（`user_id=''`）不过滤，行为不变。
- **P0-2 工件 / 轨迹跨用户可读可删**：`artifact_store` 与 `tracing` 的
  `list_artifacts` / `list_traces` 新增 `user_id` 参数，非空时 `JOIN sessions`
  按 `user_id` 收敛；`api/rest/artifacts.py`、`traces.py` 的读取 / 删除路径按
  `session → user` 归属校验。抽象基类签名同步以免 mypy override 报错。
- **P0-3 启用认证后图片附件 401 破图**：新增 `api/client.ts::fetchAttachmentBlobUrl`
  （fetch + `blob:` 渲染，自动带 Bearer，失败回退直链）；`MessageItem.vue` 与
  `ChatView.vue` 的图片附件改用该方式，并新增 `AttachmentImage.vue` 统一复用；
  组件卸载时 `revokeObjectURL` 释放，避免内存泄漏。
- **P0-4 WebSocket 双 `receive_text` 竞争丢帧**：`api/ws/chat.py` 重构为单一
  reader 协程读取入向帧，按 `stop` / `confirm_reply` 分发到 `control_queue`、
  其余进 `message_queue`，由 dispatcher 处理控制帧，主循环不再被并发读取抢帧。
- **P1-1 权限检查 fail-open 静默放行**：`engine/agent_loop.py` 的权限异常由
  `logger.debug` 升为 `logger.error`（服务已装配却异常是真实故障，必须可见）。
- **P2-2 认证密钥重启失效**：`modules/auth_manager/service.py` 在未按
  `HARNESS_AUTH_SECRET` 显式配置时，将签名密钥持久化到
  `data/auth_secret.txt`（权限 0o600），重启后复用，已签发 token 不再集体失效。

### 完善
- **D-1 会话设置只写 localStorage**：`stores/settings.ts` 的 `setTheme` /
  `setLanguage` / `setSessionSettings` 现同步 `PUT /api/settings`（后端
  `rest/settings.py` 此前从未被前端调用），并在启动时安全拉取（仅当本地无值时
  填充，避免覆盖既有偏好）；localStorage 仍作即时与离线兜底。
- **README 文档失实与「前端零入口」标注**：订正两处不实描述——「设置页可查看渠道
  状态」与「在聊天页开启桌面操控模式（Computer Use）」（两者均无对应界面）；为
  渠道 / Computer Use / Jev / 断点续跑 / 多用户标注「仅 REST API，暂无界面入口」。

### 测试
- 新增 `tests/test_audit_fixes.py`：P0-1/P0-2 归属过滤（多用户隔离 + 单用户不过滤）、
  P0-4 WS 控制帧 `stop_ack` 稳定返回、P2-2 密钥跨实例持久化。

---

## [0.0.47] - 2026-09-30

桌面壳可编译化 + 启停脚本（E11 收尾）与文档对齐。

### 新增
- **`start-desktop.bat` / `stop-desktop.bat`**（与 `start-all.bat` 同风格）：
  - 启动脚本检查 `backend/.venv`、`frontend/node_modules`、pnpm、Rust
    （`where cargo` / `where rustc`，并从 `HKCU\Environment` 兜底读取
    `CARGO_HOME` / `RUSTUP_HOME` 以兼容旧终端）以及 **MSVC 工具集**
    （`vswhere -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64`，
    能识别装在非默认盘的实例），缺任一项即打印对应安装指引并退出；
    随后检查 5173 端口占用、拉起 `tauri dev`，并用 `timeout` + `netstat`
    轮询等待 Vite 就绪（同 `start-all.bat` 的等待策略）。
  - 停止脚本按窗口标题、`andy-harness-gy.exe`、命令行特征
    （`launch-backend.py` 与监听 `127.0.0.1` 的 uvicorn，仅限
    `python*` 以免误杀自身 helper 进程）与 5173 端口四路回收。
- `start-all.bat` / `stop-all.bat` 横幅互相点名两种模式，提示不要同时占用 5173。
- `start-desktop.bat` 新增**构建新鲜度体检与出处打印**（不强制重编，因为不需要）：
  - `Checking build freshness ...` 用一次 PowerShell 比对
    `target\debug\andy-harness-gy.exe` 与 `src/`、`Cargo.toml`、`Cargo.lock`、
    `build.rs`、`tauri.conf.json`、`capabilities/` 的最新 mtime，输出
    `FRESH|STALE|MISSING ~ <时间>` 并点名最新源文件；判定基于 mtime，
    权威结论仍由 cargo 的内容指纹给出；
  - `What is running` 区块打印 exe 路径、构建时间、`git rev-parse --short HEAD`
    与工作区是否干净（`status --porcelain -uno`）、前端与后端形态；
  - 新增 `--help`（用法说明）与 `--clean`（`choice` 交互确认后删除
    `desktop\src-tauri\target` 从零重编）两个开关。
- `src-tauri/src/lib.rs` 在 `setup()` 与退出回调里打印后端基地址 / 前端来源 /
  「正在终止后端进程树」，`tauri dev` 窗口（stdout）里即可确认当前窗口对应哪个实例。
- `tests/test_desktop_shell.py`：新增 `test_exit_handler_takes_child_via_mutex`
  （静态防回归）与 `TestShellCompiles::test_cargo_check_compiles`
  （设 `HARNESS_SHELL_CARGO_CHECK=1` 时执行真实 `cargo check`，默认跳过）。

### 修复
- **桌面壳根本编译不过（`error[E0596]`）**：`src-tauri/src/lib.rs` 退出回调里
  `state.0.take()` 需要可变借用，而 `tauri::State` 只实现 `Deref`。
  改为 `struct BackendProcess(Mutex<Option<Child>>)` +
  `state.0.lock().ok().and_then(|mut g| g.take())`（`Mutex` 仍满足
  `manage` 要求的 `Send + Sync`）。E11 落地时因本机无 Rust 工具链从未真正
  编译过，`tests/test_desktop_shell.py` 只做配置/文本校验，本次首次真实
  编译才暴露。
- `CHANGELOG.md` 补回缺失的 `0.0.34` 版本标题（MCP 市场条目此前没有版本头）。

### 文档
- `README.md`：新增「桌面壳（Tauri）启动」章节（rustup + MSVC 工具集 →
  `desktop/pnpm dev` → `pnpm build`）；功能特性补 E1 / E2 / E3 / E5 / E7 /
  E9 / E10 / E11 / E12 / G4；项目结构补 `desktop/`；开发命令补桌面壳与
  `pnpm test` / `harness.eval`；排错新增第 10–12 条（**光装 rustup 编不过**、
  端口冲突与停止方式、数据目录与 `--check` 自检）。
- `desktop/README.md`：前置依赖补 **MSVC 工具集 + Windows SDK**（含
  `--installPath` / `--cache` 与 `CachePath` / `SharedInstallationPath` 策略改盘）。

### 验证（本机装齐工具链后真实跑通）
- `start-desktop.bat` 连续两次成功：依赖检查输出
  `Dependencies OK. [Rust + MSVC toolset at F:\VSBuildTools]` → 5173 空闲 →
  `tauri dev` 产出 `target/debug/andy-harness-gy.exe`（12.9 MB）→ 窗口
  `andy-harness` 打开、壳按**动态端口**（52507 / 52212）拉起后端 →
  `/api/health` 返回 `{"status":"ok"}`；`stop-desktop.bat` 后壳 / 后端 / Vite
  全部退出、端口释放。
- `pytest tests/test_desktop_shell.py`：**9 passed, 1 skipped**（默认跳过
  编译项）；`HARNESS_SHELL_CARGO_CHECK=1` 时 **10 passed**（`cargo check`
  真实编译 498 个 crate，4m41s）；`ruff check tests/test_desktop_shell.py`
  **All checks passed**。
- 工具链落盘位置：Rust（`RUSTUP_HOME`/`CARGO_HOME`）与 MSVC 工具集
  （`F:\VSBuildTools`）均在 F 盘；下载缓存 `F:\VSCache`、Windows SDK 经
  junction 指向 `F:\WindowsKits`、67 个 SDK MSI 缓存条目指向
  `F:\VSPackageCache`，C 盘仅保留 junction（0 字节）并回收 2.15 GB。

---

## [0.0.46] - 2026-09-30

E12 · 认证与多用户（安全增强，方案 A 第 11 步，E4 暂缓）。

设计定位：本地单用户工具，认证**默认关闭、行为零变化**；
`HARNESS_AUTH=1` 显式启用后，全站 `/api/**` 强制 Bearer token，会话与
长期记忆按用户隔离。

### 新增
- 安全原语 `harness/infra/security.py`：PBKDF2-HMAC-SHA256（200,000 次）
  口令哈希与校验；HMAC 签名 token（`base64url(payload).base64url(hmac)`，
  claims 含 uid / username / is_admin / iat / exp），默认 12 小时有效。
- `harness/modules/auth_manager/`：`AuthService`（启停、登录、token 解析、
  用户 CRUD、系统会话归属用户）；启用时若无用户自动引导管理员（凭据来自
  `HARNESS_AUTH_ADMIN_USERNAME` / `HARNESS_AUTH_ADMIN_PASSWORD`，未提供
  密码则生成随机密码并 WARNING 输出），并把历史无主会话与记忆划归首位
  管理员。
- `AuthMiddleware`：启用认证后对 `/api/**` 校验 Bearer；豁免
  health / status / login 与渠道入站（另由渠道密钥保护）与 OPTIONS；
  未认证返回统一 401 格式。
- REST：`/api/auth/status`、`/api/auth/login`、`/api/auth/logout`、
  `/api/auth/me` 与 `/api/users`（管理员 CRUD，含不能删自己、不能删最后
  一个管理员等保护）。

### 变更
- 数据模型：新增 `users` 表；`sessions` / `memories` 增加 `user_id`
  列（建表 SQL + 旧库 ALTER 迁移）。
- 会话与记忆服务、仓储全部支持按 `user_id` 归属过滤；`user_id=""` 时
  不过滤，保证未启用认证时行为与旧版完全一致。
- AgentLoop 新增 `user_id` 运行参数：记忆提示与记忆工具按用户，
  子代理继承父会话归属；渠道会话归属系统用户。
- WebSocket `/ws/chat` 支持 `?token=` 认证（无 token / 无效关闭 1008），
  并按用户校验会话归属。
- 前端：新增 `api/token.ts`（localStorage 持久化）、`stores/auth.ts`、
  `LoginView.vue` 与 App 登录门；请求自动附带 Authorization，WS 自动
  附带 token；401 / 1008 自动回到登录页；对话页头部加入退出登录按钮。

### 验证
- 新增 `tests/test_auth.py`（**23 passed**）：默认关闭、状态 / 登录 / 登出、
  401 与无效 token、管理员用户 CRUD 与非管理员 403、删除自己 / 最后管理员
  保护、会话与记忆跨用户隔离、WS 无 token 关闭 1008 与跨用户拒绝。
- 前端新增 token / LoginView / client 认证测试（10 例），前端共 **42 passed**；
  `pnpm build` 通过。
- 新增代码 `ruff check`、`mypy harness` 干净。

---

## [0.0.45] - 2026-09-30

E11 · 桌面壳 Tauri（应用形态，方案 A 第 10 步）。

### 新增
- 新增 `desktop/` 桌面壳工程：
  - `src-tauri/`（Tauri 2）：`tauri.conf.json`（复用既有前端产物，
    `frontendDist` 指向 `frontend/dist`，dev 指向 Vite 5173）、`Cargo.toml`、
    `src/main.rs` + `src/lib.rs`；
  - Rust 侧负责选端口、经启动器拉起后端、暴露 `get_backend_url` 给前端，
    退出时终止整个后端进程树（Windows `taskkill /F /T`）；
  - `launcher/launch-backend.py`：解析 venv 解释器、默认只监听 127.0.0.1、
    数据目录落到每用户应用目录、支持 `--check` 与端口 / 数据目录参数；
  - 全套应用图标（PNG / ICO / ICNS）及重新生成说明。
- 后端新增 CORS 中间件：放行 Tauri 协议源（`tauri://localhost`、
  `http://tauri.localhost`）与本地开发端口，可用 `HARNESS_ALLOWED_ORIGINS`
  追加。
- 前端新增 `api/runtime.ts`：识别 Tauri 环境并解析后端绝对地址（HTTP 与
  WebSocket 均接入）；应用挂载前先解析后端地址。

### 验证
- 新增 `tests/test_desktop_shell.py`（**8 passed**）：启动器 `--check`
  解析、缺失解释器报错、端口 / 数据目录参数、Tauri 配置 / 能力 / Cargo
  一致性、Rust 命令与前端 invoke 对齐；**端到端**经启动器真实拉起后端、
  `/api/health` 实测后整树终止。
- 前端新增 `runtime.test.ts` 4 测试、前端共 **32 passed**；`pnpm build` 通过。
- 新增代码 `ruff check` 干净；`mypy harness` 未新增错误。

---

## [0.0.44] - 2026-09-30

E10 · 多 Agent 编排（并行任务 + 角色流水线，增强能力，方案 A 第 9 步）。

### 新增
- 新增 `harness/modules/orchestrator/`：
  - `OrchestratorService`：在现有子代理能力之上提供两种编排模式；
  - **并行（parallel）**：多个互相独立的子任务同时跑，`asyncio.Semaphore`
    控制并发上限，按输入顺序返回结果，单个任务异常不拖垮整批；
  - **流水线（pipeline）**：多个有角色的阶段串行接力，上一阶段产出自动
    作为下一阶段输入（提示词中 `{input}` 替换，或自动追加），阶段失败即
    终止流水线；
  - 安全护栏：一次最多并行 10 个子任务 / 流水线最多 8 个阶段，防止模型
    失控派生大量代理。
- 新增 `plugins/tool_orchestrator`（工具插件）：注册编排服务与 `parallel`、
  `pipeline` 两个工具，编排结果带编号、逐任务 / 逐阶段状态。
- 子代理排除工具清单同步加入 `parallel` / `pipeline`，避免子代理内部
  递归派生。

### 验证
- 新增 `tests/test_orchestrator.py`（**18 passed**）：乱序完成仍按序返回、
  并发上限生效、空输入 / 超限报错、无 SubagentService 全部快速失败、
  单任务异常隔离、流水线接力与 `{input}` 占位、失败终止、两个工具的成功
  与错误路径；**端到端**经真实 AgentLoop 调 `parallel` 派生两个子代理
  并发完成，主上下文收到并行结果、子代理临时会话全部清理。
- 全应用冒烟启动：插件加载、`parallel` / `pipeline` 工具均注册成功。
- 新增代码 `ruff check` 干净；`mypy` 零问题。

---

## [0.0.43] - 2026-09-30

E1 · 多渠道 Channel（Webhook / Telegram，增强能力，方案 A 第 8 步）。

### 新增
- 新增 `harness/modules/channel_manager/`：
  - `Channel` 抽象（start/stop/send，只负责「如何收发」）+ `ChannelManager`
    （注册渠道、统一跑 AgentLoop、按 `(channel, user)` 映射 / 复用会话）；
  - 新增 `channel_links` 表（`channel` + `external_user` → `session_id`），
    使同一渠道用户在多次消息 / 服务重启后仍续接同一对话，会话标题自动为
    `渠道名 · 用户标识`；
  - 内置 `WebhookChannel`：外部系统 POST 入站，终答由 HTTP 响应同步返回，
    支持 `X-Channel-Secret` 密钥与用户名单；
  - 新增 `TelegramChannel`：Bot API `getUpdates` 长轮询收消息，`sendMessage`
    回发，offset 自动推进、支持用户白名单，超长文本截断；HTTP 传输层可
    注入，默认基于标准库 urllib（零新增依赖）。
- 新增 REST：`GET /api/channels`（列表）、`GET /api/channels/{name}`（详情）、
  `POST /api/channels/inbound/{name}`（Webhook 入站）、
  `POST /api/channels/{name}/send`（主动发送）。
- 新增插件：`channel_manager`（服务，建管理器 + 内置 Webhook）、
  `channel_telegram`（渠道，挂入 Telegram 并启动）。
- 环境变量：`HARNESS_CHANNEL_PROVIDER` / `HARNESS_CHANNEL_MODEL` /
  `HARNESS_CHANNEL_SYSTEM_PROMPT`、`HARNESS_WEBHOOK_SECRET` /
  `HARNESS_WEBHOOK_ALLOWED_USERS`、`HARNESS_TELEGRAM_BOT_TOKEN` /
  `HARNESS_TELEGRAM_ALLOWED_USERS`。

### 验证
- 新增 `tests/test_channels.py`（13 例）与 `tests/test_channel_telegram.py`
  （10 例）：共 **23 passed**——入站处理、会话复用、映射持久化、未知渠道 /
  空消息 / 无 provider 报错、用户名单、密钥校验、长轮询 offset、非文本忽略、
  轮询失败不崩溃、超长截断、端到端回发及 REST 路由。
- 全应用冒烟启动：`GET /api/channels` 返回 `webhook` 与 `telegram` 两个渠道。
- 新增代码 `ruff check` 干净；`mypy` 零问题。

---

## [0.0.42] - 2026-09-30

E9 · 会话 / 消息搜索（增强能力，方案 A 第 7 步）。

### 新增
- `SessionService.search_sessions`：按**会话标题**或**消息内容**（SQL LIKE）
  搜索，返回会话信息与最多 3 条命中消息的片段（命中位置前后各取 48 字、
  折叠空白）；默认不搜归档会话；用户输入的 `%` / `_` / `\` 经转义，
  不会被当作通配符。
- 新增 `GET /api/sessions/search?q=...`（在 `/{session_id}` 之前注册，
  避免 "search" 被当作会话 id），支持 `include_archived` / `limit`。
- 前端侧边栏新增搜索框：输入防抖 300ms 调用搜索接口，结果平铺展示
  标题 / 命中片段，点击结果直达会话并清空搜索；Esc 或 × 可退出。

### 验证
- 新增 `tests/test_session_search.py`：**12 passed**（内容 / 标题命中、
  片段、空查询、LIKE 通配符转义、归档开关、每会话最多 3 条、大小写、
  message_count、REST 路由顺序）。
- 新增 `SessionSidebar.test.ts` 4 测试（搜索框渲染、防抖结果、× 清空、
  无结果状态），前端共 **28 passed**；`pnpm build` 303 模块通过。
- 后端 `ruff check` 干净；`mypy` 仅余 sessions.py 2 个历史基线错误
  （本次新增路由用 cast 未增加错误）。

---

## [0.0.41] - 2026-09-30

E2 · Docker 沙箱后端（容器级隔离，增强能力，方案 A 第 6 步）。

### 新增
- 新增 `DockerBackend`（`harness/modules/sandbox_manager/service.py`）：
  - 每次执行以一次性容器运行（`docker run --rm`），结束即销毁，**天然无残留**；
  - 会话工作区以 bind mount 挂载到容器内 `/workspace`，文件在多次执行间持久；
  - 默认 `--network none` **禁网**（可配置打开）；`--memory` / `--cpus`
    施加资源限制；支持**镜像白名单**；
  - 超时 / 异常时 `docker rm -f` 强制删除本次容器，**防止容器泄漏**；
  - docker 不可用时给出明确错误而非崩溃；零新增依赖，仅调用本机 `docker` CLI。
- code-runner 插件接入：`config.backend=docker` 或环境变量
  `HARNESS_SANDBOX_BACKEND=docker` 启用，可配镜像 / 网络 / 内存 / CPU；
  默认仍为本地子进程后端。
- 安全检查（危险命令 / 路径遍历）抽取为模块级共享函数，两个后端复用。

### 验证
- 新增 `tests/test_docker_sandbox.py`：**18 passed**（参数构造、成功执行、
  退出码透传、不可用拦截、超时强删、镜像白名单、危险 / 路径拦截、
  不支持语言、输出截断、可用性检测、工作区清理）；真实 docker 端到端
  1 例在本机无 docker 时自动跳过。
- 连同既有 `tests/test_sandbox.py` 共 **30 passed、1 skipped**；
  `ruff check` 与 `mypy` 对改动零问题。

---

## [0.0.40] - 2026-09-30

E3 · 结构化 compaction（compaction v2，增强能力，方案 A 第 5 步）。

### 新增
- 新增 `StructuredCompactionStrategy`（`context_manager/service.py`）：
  - **保留最近 N 条消息原文**，旧消息折叠为一条**结构化事件日志**，按
    「请求 / 工具调用（含参数）/ 结果 / 结论」逐条编号；
  - 折叠过程**确定性、不依赖 LLM 重写**（零额外成本、不会产生幻觉）；
  - 裁剪边界自动避让 assistant(tool_calls) ↔ tool 消息组，**不切断工具组**，
    避免产生孤立 tool 消息；长内容与工具结果按上限截断。
- context-manager 插件接入新策略：`config.strategy` 取
  `structured` / `structured_compaction` / `compaction_v2` 时启用。

### 验证
- 新增 `tests/test_structured_compaction.py` 共 **8 passed**：短对话不变、
  最近原文保留、旧消息折叠、单日志位置、多组边界不切断、钉住消息保留、
  长内容截断、None 内容安全。
- 连同既有 `test_context_manager.py` 共 **19 passed**；`ruff check` 与
  `mypy` 对改动零问题（顺带修复 `_collapse` 对 Any 返回值的标注问题）。

---

## [0.0.39] - 2026-09-30

E7 · 向量记忆检索（增强能力，方案 A 第 4 步）。

### 新增
- `backend/harness/modules/memory_manager/embedding.py`：嵌入器模块。
  - `Embedder` 抽象 + `HashingEmbedder`（零依赖、确定性的特征哈希嵌入，中英混合分词，
    反映词项重叠，默认离线可用）；
  - `OpenAICompatibleEmbedder`：调 OpenAI 兼容 `/embeddings` 端点，获得真正的
    语义（同义词 / 改写）检索；
  - 纯函数 `cosine_similarity`、`tokenize`（拉丁词整体、CJK 按字、下划线独立成 token）。
- 改造 `service.py`：`save` 时把嵌入写入 `memories.embedding` 列；`search` 变为
  **向量余弦排序为主、无命中或无嵌入器时回退 LIKE** 的混合实现；旧数据在检索时
  懒回填向量；`update` 内容变更时重算嵌入。默认最小相似度阈值 `0.15`。
- `build_embedder_from_env()`：设置 `HARNESS_EMBEDDING_MODEL` + API key 时启用真实
  嵌入，`HARNESS_EMBEDDING=off` 显式禁用，否则本地哈希；已接入 memory-manager 插件。

### 验证
- 新增 `backend/tests/test_memory_vectors.py` 共 **17 passed**：哈希嵌入器 7 例、
  余弦函数 3 例、远端嵌入器构造 1 例、记忆服务向量检索 6 例（含语义排序、空查询、
  无嵌入器降级、update 重嵌、旧行回填）。
- 连同既有记忆测试，`test_memory_manager.py` / `test_api_memories.py` 共 **37 passed**；
  `ruff check` 与 `mypy` 对新模块零问题。

---

## [0.0.38] - 2026-09-30

E8 · 前端测试（vitest，质量保障，方案 A 第 3 步）。

### 新增
- 引入 **vitest 5 + @vue/test-utils + jsdom**（devDependencies）：
  - `vite.config.ts` 增加 `test` 配置（jsdom 环境、globals、v8 覆盖率）；
  - `package.json` 增加脚本：`test`（vitest run）、`test:watch`、`test:coverage`。
- 新增 4 个测试文件共 **24 例**：
  - `src/utils/format.test.ts`（7）：token / 耗时格式化 K/M 档与边界；
  - `src/utils/datetime.test.ts`（8）：UTC 时间解析（带时区 / 无时区按 UTC / 非法值）与本地格式化；
  - `src/stores/todos.test.ts`（5）：Pinia store 的加载成功 / 失败安全清空 / 空会话 / 清空 / 重置
    （用 `vi.hoisted` + `vi.mock` 隔离 API 客户端）；
  - `src/components/MarkdownRenderer.test.ts`（4）：markdown / 代码块 / 链接 / 空内容渲染。

### 验证
- `pnpm test`：**4 test files / 24 tests 全绿**。
- `pnpm build`：vue-tsc 类型检查 + vite build 仍通过（303 modules，3.04s）。

---

## [0.0.37] - 2026-09-30

E5 · Eval 回归框架（质量保障，方案 A 第 2 步）。

### 新增
- `backend/harness/eval/`：任务级评测框架，零新增依赖。
  - `cases.py`（`EvalCase` / `load_cases`）：任务 + 期望的声明与 JSON 加载、字段与重复 id 校验；
  - `scoring.py`（`score_case`）：按「答案关键词（all/any）+ 期望工具 + 禁用工具 + 迭代/耗时上限 +
    无错误」逐项打分，产出可读的失败原因；
  - `runner.py`（`EvalRunner`）：每 case 独立会话/独立 AgentLoop，provider 由工厂按需构造，
    内置 calculator / current_time 工具，支持注入额外工具；
  - `report.py`（`EvalReport`）：聚合通过率、总耗时、工具调用次数、工具使用分布、按标签通过率，
    支持 JSON / Markdown 导出。
- `backend/harness/eval/__main__.py`：`python -m harness.eval --cases <file> --provider <id>`，
  支持 `--model` / `--tag` / `--report`；全部通过退出 0、有失败退出 1，可直接作为 CI 门槛。
- `backend/tests/evals/eval_cases.json`：4 个种子任务（纯对话、计算器、当前时间、禁用工具约束）；
  `backend/tests/evals/README.md`：用例格式与运行说明。

### 验证
- 新增 `backend/tests/test_eval.py` 共 **20 passed**：加载校验 7 例、打分逻辑 8 例、
  端到端运行/指标/导出/隔离 5 例（离线脚本化 provider，确定性、不依赖网络）。
- `ruff check` 与 `mypy harness/eval` 对新模块零问题。

---

## [0.0.36] - 2026-09-30

E6 · 插件 / 技能从 zip / git 外部安装（生态分发，方案 A 第 1 步）。

### 新增
- `backend/harness/modules/package_installer/service.py`（`PackageInstaller`）：
  - `materialize_zip`：本地路径或 http(s) URL 的 zip 归档，下载后解压；
  - `materialize_git`：`git clone --depth 1`（可指定 `--branch <ref>`）；
  - `resolve_package_root`：自动识别并下沉 zip 常见的「外层包裹目录」，支持显式 `subdir`；
  - `_safe_extract`：逐成员校验目标路径，**防御 zip slip 路径穿越**；
  - `copy_tree`：合并复制（目标目录可已存在）。
- `backend/harness/api/rest/plugins.py`：`POST /api/plugins/install-external`，
  拉取后校验 `plugin.json`、重写清单并标记 `source=external`，加载并激活；外部插件与市场插件一样可卸载。
- `backend/harness/api/rest/skills.py`：`POST /api/skills/install-external`，
  校验 `SKILL.md` 后安装到用户级 skills 目录并重载扫描。
- 两端点请求体统一支持 `source` / `kind`（`zip`|`git`）/ `ref` / `subdir`。

### 验证
- 新增 `backend/tests/test_package_installer.py`：安装器核心 6 例（zip 本地/包裹目录/缺文件/路径穿越、
  git 本地/克隆失败）+ REST 集成 6 例（插件 zip/非法包/git、技能 zip/缺 SKILL.md/git），**12 passed**。
- `ruff check` 与 `mypy` 对相关改动零问题；顺带修复 `plugins.py` 市场安装端点 `manifest`
  字典的类型标注（历史 mypy 错误，标注为 `dict[str, Any]`）。

---

## [0.0.35] - 2026-09-30

按 `docs/GAP-ANALYSIS-2026-09-30.md` 建议顺序落地的核心缺口修复（G1–G5）。

### G1 · Computer Use 运行时依赖入声明（P0）
- `backend/pyproject.toml` 新增 optional extra：`desktop = ["pyautogui>=0.9.54", "mss>=9.0.1"]`。
- 缺失时 Computer Use 工具优雅降级为「桌面不可用：未安装 pyautogui/mss」；启用 Computer Use 需
  `uv sync --extra desktop`。README 已写明安装方式与排错项。

### G2 · Windows 沙箱资源限制 + 进程树终止（P0）
- `backend/harness/modules/sandbox_manager/service.py`：
  - 用 **Job Object**（ctypes 调 `kernel32`）对被执行的子进程施加 **512MB 内存上限**
    （best-effort，权限不足时跳过）。
  - 超时/终止时改用 `taskkill /F /T /PID <pid>` 杀整棵进程树（含孙进程）；Unix 保留 `os.killpg`。
- 新增 `tests/test_sandbox.py::TestWindowsProcessTreeKill`（仅 Windows 运行），验证超时后孙进程被清掉。

### G3 · SQLite 并发：WAL + busy_timeout + 写锁（P1）
- `backend/harness/infra/database.py`：连接后开启 `PRAGMA journal_mode = WAL` 与
  `PRAGMA busy_timeout = 5000`，并用 `threading.RLock` 包裹共享连接的读写关操作，缓解并发写
  `database is locked` / 事务交错。
- 新增 `tests/test_database_concurrency.py`：WAL/busy_timeout 断言 + 8 线程 ×50 次 UPSERT 无损坏。

### G4 · Checkpoint 断点续跑（P1）
- 新增 `backend/harness/engine/checkpoint.py`：`RunCheckpointService`（检查点 upsert / 状态更新 /
  按 run 读取 / 按会话列举）+ `resume_run()`（读检查点 → 选 provider → 重建 AgentLoop 续跑）。
- `backend/harness/engine/agent_loop.py`：运行开始 / 每轮迭代后 / 终答后 / 异常 四处 best-effort 写
  `agent_runs` 检查点。
- 新增 `backend/harness/api/rest/runs.py`：`POST /api/runs/{run_id}/resume`；`main.py` 接线。
- `agent_runs` 表（含 `idx_agent_runs_session` 索引）随库自动建表。
- 设计取舍：消息每轮即时持久化，会话历史即完整状态快照，续跑只基于已有上下文让模型继续，不重放内存态。
- 新增 `tests/test_checkpoint.py`（10 例：服务增删改查 + AgentLoop 写 done/error 检查点 +
  `resume_run` 三路径）。

### G5 · 文档与实现脱节（P1）
- `ENHANCEMENTS.md` §2 差距表：Skills / HIL 审批 / Todo / MCP / 子代理 / 长期记忆 / 会话分支 + Checkpoint
  由 ❌ 改为 ✅；新增 Computer Use、插件/技能/MCP 市场行。
- `ENHANCEMENTS.md` 新增 §0.5「核心产品定位：Computer Use（一等目标）」并给出验收标准与落地位置；
  P2-1 Checkpoint 状态 ⬜→✅ 并补落地位置；§4 实施记录新增 G4 明细。
- `README.md`：顶部简介与「功能特性」补 **Computer Use 桌面操控**、**技能市场**、**MCP 市场**；
  「使用指南」新增 Computer Use / 技能市场 / MCP 市场 用法；「排错」新增第 9 条（desktop extra）。
- `docs/GAP-ANALYSIS-2026-09-30.md` 新增 §6 落地进度表。

### 验证（四关）
- **后端测试**：`tests/test_sandbox.py`（12 passed）、`tests/test_database_concurrency.py`、
  `tests/test_checkpoint.py`（10 passed）全绿；G2/G3/G4 回归套件（36 passed）全绿。
- **静态检查**：相关文件 `ruff check` 全绿；`mypy harness` 未新增错误（顺带修正
  `agent_loop._call_model` 四元组返回值注解，消除 3 个历史错误，总错误由 15→12）。
- **前端**：G1–G5 无前端代码改动，沿用最近一次 `vue-tsc` + `vite build` 通过结果。

---

## [0.0.34] - 2026-09-30

新增 MCP 市场：设置页 MCP 标签增加「从 MCP 市场安装 MCP」，并内置 5 个**免注册、免鉴权**的公开 MCP 服务。

### MCP 市场（新增）
- 后端 `harness/api/rest/mcp.py`：
  - `GET /api/mcp/marketplace` 列出 `backend/mcp_marketplace/<id>.json` 里的服务（含类型、URL、
    `auth_required`、已安装标记）。
  - `POST /api/mcp/marketplace/{id}/install` 校验后写入配置并**立即连接**（连接失败也保留配置，
    返回 `connected=false` 与原因，可稍后在列表刷新重试）。
  - `DELETE /api/mcp/marketplace/{id}` 断开并移除。
  - 复用既有 `add_server` / `remove_server`，与手动新增的 server 走同一套持久化。
- 前端 `SettingsView.vue`：MCP 页新增「从 MCP 市场安装 MCP」按钮，展开市场面板，
  交互与插件市场完全一致（卡片 → 说明弹窗 → 安装 / 卸载），卡片标注类型与「免鉴权」徽标。

### 内置 5 个免鉴权 MCP（均已实测握手 + 拉取工具清单）
| 名称 | 端点 | 工具 | 用途 |
|------|------|------|------|
| `libgen` | `https://mcp.jmrp.io/libgen` | search / get_details / download / read | 书籍·论文·文献检索 |
| `deepwiki` | `https://mcp.deepwiki.com/mcp` | ask_wiki_question / read_wiki_contents / read_wiki_structure | GitHub 仓库文档问答 |
| `context7` | `https://mcp.context7.com/mcp` | resolve-library-id / query-docs | 开发库官方文档与示例 |
| `cloudflare-docs` | `https://docs.mcp.cloudflare.com/mcp` | search_cloudflare_documentation / migrate_pages_to_workers_guide | Cloudflare 官方文档 |
| `gitmcp` | `https://gitmcp.io/docs` | fetch/search 文档与源码等 5 个 | 任意 GitHub 仓库文档/代码检索 |

### 顺带修复：GET 建流但不给 endpoint 的服务连不上
- DeepWiki 这类服务 `GET` 返回 200 事件流却**从不下发 `event: endpoint`**，
  原实现空等 20s 后直接失败。现在等待超时也会**退回直接 POST 模式**（事件流任务保留）。
- 把等待时长抽为常量 `_OPEN_ENDPOINT_TIMEOUT`，便于测试调小。

### 验证
- 新增 `tests/test_api_mcp_marketplace.py`（4 例：列表 5 个服务且全部 `auth_required=false`、
  字段完整；用本地 stdio echo server 做安装 → 列表出现 → 重复安装 409 → 卸载 → 重复卸载 404 往返）。
- 新增 `test_mcp_streamable_http.py::test_get_stream_without_endpoint_falls_back`
  （模拟 DeepWiki 的「GET 建流无 endpoint」形态，验证超时回退）。
- 线上实测 5 个服务全部握手成功并取到工具；`pytest` 相关 MCP 测试全绿；
  `ruff` 零问题、`mypy` 零报错；前端 `vue-tsc` 0 错、`vite build` 通过。

---

## [0.0.33] - 2026-09-29

修复 MCP 客户端无法连接 streamable-http（POST-only）类型 server 的问题 —— 已安装的 libgen 因此完全不可用。

### 问题
配置的 `libgen`（`backend/mcp.json`，`https://mcp.jmrp.io/libgen`）连接失败：
- 服务端**本身正常可用**（POST 握手返回完整能力与 25 条检索结果），但它已迁到
  streamable-http：`GET <url>` 只回 **405 Method Not Allowed**（响应头 `allow: POST`）。
- 客户端 `MCPSSEConnection` 强制先 `GET` 建立 SSE 事件流等 `event: endpoint`，
  拿不到就抛「MCP SSE 尚未获得 POST endpoint」，整个 server 连不上、工具数为 0。
- 改配置 `"type": "streamable-http"` 也无效：`make_connection` 把 SSE / http /
  streamable-http 全都映射到同一个实现，没有真正的分支。

### 修复（`harness/modules/mcp_client/service.py`）
- **GET 建流失败自动退回「直接 POST」模式**：捕获 HTTP 状态码错误（如 405）后，
  把配置里的 URL 直接作为 JSON-RPC 端点。旧版 SSE server 行为完全不变。
- **支持 POST 响应体即 SSE 事件流**：新增 `_parse_inline_sse()`，解析
  `event: message` / `data: {...}` 形态的响应体并分发（原来只认 `application/json`）。
- **会话保持**：记录 initialize 下发的 `Mcp-Session-Id` 并在后续请求回传。

### 验证
- 新增 `tests/test_mcp_streamable_http.py`（4 例）+ `tests/mcp_streamable_http_server.py`
  （本地测试服务器，模拟 GET 405 / POST 返回内联 SSE）：含连接取工具清单、
  工具调用往返、`Mcp-Session-Id` 回传断言。
- 线上 libgen 端到端实测：握手成功，发现 `download / get_details / read / search`
  4 个工具，`search("Deep Learning")` 真实返回 25 条书目（含 md5 与下载链接）。
- 既有 MCP 测试 23 passed（无回归）；`ruff` 零问题；`mypy` 零报错。

---

## [0.0.32] - 2026-09-29

混合语种回答策略；插件市场新增 5 个常用工具插件；新增技能市场并把 Skills 页「重新扫描」按钮改为「从技能市场安装技能」。

### 混合语种回答判定
- `harness/engine/agent_loop.py` 的语言策略系统消息补充混合语种规则：
  ① 以「承载主要意图/占比最大的语言」为准；② 占比相当难以判断时以中文作答；
  ③ 专有名词、代码、命令、产品名保留原文，不强制翻译。使多语混杂时的回答语种判定更确定。

### 插件市场：新增 5 个常用工具插件
在 `backend/marketplace/` 下新增 5 个零依赖、自包含的工具插件包（`plugin.json` + `main.py`）：
- `tool_calculator` 计算器：基于 `ast` 白名单的安全数学表达式求值（禁止任意代码执行），
  支持 + - * / ** // %、括号与 sqrt/abs/round/floor/ceil/sin/cos/tan/log/log10/log2/exp/pow/factorial，常量 pi/e/tau。
- `tool_datetime` 日期时间：指定时区的当前时间、`from_timestamp` 时间戳互转、`add` 日期加减，含星期与 ISO 8601。
- `tool_unit_converter` 单位换算：长度/重量/温度/面积/体积/速度/数据七大类的单位互算。
- `tool_hash_encode` 哈希与编码：md5/sha1/sha256/sha512 摘要，base64 与 base64url 的编解码。
- `tool_file_manager` 文件管理：受控目录内的 list/read/write/mkdir/delete，**所有路径强制限制在 base_dir 内**（拦截 `../` 越权），写操作自动建父目录、读操作超长截断。

### 技能市场（新增）
- 后端 `harness/api/rest/skills.py`：
  - `GET /api/skills/marketplace` 列出 `backend/skill_marketplace/<name>/SKILL.md` 技能包（含版本、许可、正文、已安装标记）。
  - `POST /api/skills/marketplace/{package_id}/install` 复制技能包到用户级 skills 目录并重新扫描。
  - `DELETE /api/skills/marketplace/{package_id}` 卸载已安装技能并重新扫描。
- 新增 5 个常用 Skill（`backend/skill_marketplace/`）：
  `meeting-notes`（会议纪要抽取）、`email-draft`（邮件/消息起草）、
  `explain-concept`（概念通俗讲解）、`text-summary`（长文结构化摘要）、
  `translate-text`（多语种翻译与本地化润色）。
- 前端 `SettingsView.vue` + `stores/skills.ts`：Skills 页原「重新扫描」按钮改为
  **「从技能市场安装技能」**，点击展开技能市场面板，交互与插件市场完全一致
  （卡片列表 → 说明弹窗 → 安装 / 卸载）；store 新增 `marketplace` /
  `fetchMarketplace` / `installMarketplaceSkill` / `uninstallMarketplaceSkill`。

### 验证
- 新增 `tests/test_api_marketplace.py`（6 例）：插件市场列出 5 个新工具且字段完整（含
  entry 与 plugin_code 非空）、技能市场列出 5 个新 Skill 且 frontmatter 可解析、
  技能市场安装/重复安装 409/卸载/重复卸载 404 往返。
- `pytest`（7 个相关测试文件）：**101 passed**。
- `ruff` 零问题；`mypy harness`：本次改动的两个文件零报错（总 18 错为既有基线）。
- 前端 `vue-tsc -b --force` 0 错误；`vite build` 通过。
- 5 个插件均通过功能冒烟（含 `tool_file_manager` 的 `../etc/passwd` 越权拦截验证）。

---

## [0.0.31] - 2026-09-29

回答语言策略：面向用户的回答须与用户「本条消息」语种一致，内部思考可用中文。

### 新增
- 主对话 `AgentLoop`（`harness/engine/agent_loop.py:_build_context`）在系统消息前缀中
  注入「语言策略」：要求模型始终用用户本条消息所用自然语言作答（中文问→中文答、
  英文问→英文答、其他语种同理），内部思考/推理可用中文，但终答必须与提问语种一致。
  该消息位于用户自定义提示词、当前时间之后，会话历史之前。
- `computer_use` 子 Agent（`backend/plugins/computer_use/agent.py:_SYSTEM_PROMPT`）
  同步补充语言策略段，并把 `finish` 终答由「中文总结」放宽为「与用户提问语种一致的总结」，
  内部思考仍可用中文，使其经主 Agent 转述后保持一致。

### 验证
- 新增回归测试 `TestAgentLoopLanguagePolicy`：`_build_context` 须注入含「语言策略/本条消息/
  内部思考/中文/语种一致」的系统消息，且位于第一条 user 消息之前。
- `pytest tests/test_agent_loop.py tests/test_plugin_computer_use.py`：38 passed。
- `ruff` 零问题；`mypy harness` 未因本次改动新增错误（既有 `_call_model` 元组解包报错为历史基线）。

---

## [0.0.30] - 2026-09-29

会话列表按「年月日」分组、降序排列（近期日期靠上），并支持按日期收缩/展开。

### 新增
- 侧边栏会话列表不再平铺，改为按 `updated_at` 的本地日期（`YYYY-MM-DD`）分组；
  组间按日期降序（今天/昨天 → 更早），组内按最后活跃时间降序。
- 每个日期分组头部显示：可点击的折叠箭头、本地化日期标题
  （中文 `今天`/`昨天`/`YYYY年M月D日`，英文 `Today`/`Yesterday`/本地化）、
  以及该组会话数量徽标。
- 点击分组头可在「展开 / 折叠」间切换，折叠状态独立记忆于 `collapsedGroups`，
  默认全部展开。展开/折叠带轻量淡入淡出过渡。

### 验证
- `vue-tsc -b --force`：0 错误；`vite build`：通过。
- 分组键基于本地朴素时间解析，避免时区导致的跨日错位。

---

## [0.0.29] - 2026-09-29

删除会话时连带清理运行轨迹。

### 修复
- **删除会话后轨迹仍残留**：`SessionServiceImpl.delete_session` 此前只清消息、
  附件，不清 spans 表，导致设置→轨迹里被删会话的轨迹还在（「清空全部会话」
  是循环调用单个删除，也有同样问题）。
- `SpanService` 新增 `delete_by_session(session_id)`，一条 SQL 删除该会话全部
  spans；`delete_session` 在删消息后调用它（tracing 插件未启用时静默跳过）。
  这样单个删除、清空全部都会连带清理对应轨迹。

### 验证
- 40 passed；脚本验证：删除会话 A 后其 trace 全部清除、会话 B 的 trace 不受影响。

## [0.0.28] - 2026-09-29

修复流式与完成后的视觉跳变，以及完成后执行过程消失。

### 修复
1. **流式正文直接显示为 assistant 气泡**：此前流式输出时 `streamingContent` 是
   裸文字（无背景/边框），完成后突然变成气泡样式，视觉跳变突兀。现在
   `.streaming-content` 采用与 `.assistant-bubble` 完全一致的背景、边框、圆角、
   阴影，从第一个字到完成一直是气泡，完成时无感切换。
2. **完成后执行过程框不再消失**：思维链（reasoning_content）此前只在流式时
   临时显示，后端不落库，完成 reload 后过程框就没了。现在：
   - messages 表加 `reasoning` 列（自动迁移）；
   - `Message` / `MessageRecord` / `MessageRepository` / `SessionService.append_message`
     全链路加 reasoning；
   - `agent_loop._call_model` 收集每次调用的 reasoning_content，整轮（含工具迭代）
     累积后随最终 assistant 消息持久化；
   - 前端 `displayItems` 把最终答案的 reasoning 作为过程步骤排在答案气泡之前，
     完成后「执行过程」框照常显示（默认收缩，可展开回看思维链）。

### 验证
- vue-tsc 0 错；test_agent_loop + test_api_ws 通过；
- 端到端脚本验证：分两块发送的 reasoning_content 正确合并持久化
  （`让我先想想。嗯，想清楚了。`），消息接口含 reasoning 字段。

## [0.0.27] - 2026-09-29

深化审查后修复的 UI 一致性问题。

### 修复
- **聊天消息里的 token 用量数字未格式化**：`MessageItem.vue` 此前直接渲染
  `message.usage.prompt_tokens` 等原始数字（如 `7680` / `1234567`），与轨迹页
  已统一的 K/M + 千分位风格不一致。抽出 `frontend/src/utils/format.ts`
  共享 `formatTokens()` / `formatMs()`，聊天消息下方的「本次用量」一行现在
  同样显示 `7.7K` / `1.23M`，缓存命中/未命中也按此格式。

### 验证
- vue-tsc 0 错。

## [0.0.26] - 2026-09-29

全面审查后修复的硬 bug。

### 修复
- **done 帧 trace_id 与 spans 表对不上**：`ws/chat.py` 此前 done 帧发的
  `trace_id` 是 WS 连接级 uuid4（连接建立时生成），而 agent run 内部的
  span 全部挂在 `result.trace_id`（agent_loop 生成的 hex[:16]）下。两者
  不一致时，前端若拿 done 帧的 trace_id 去查轨迹会查不到本轮 span。
  现在改为 `getattr(result, "trace_id", "") or trace_id`，与 spans 表一致。
- **轨迹 run 名 hover 看不到全文**：`agent_loop._build_run_title` 此前在后端
  就把标题/问题各截断到 24 字存进 span name，前端 `:title` 取到的还是截断版。
  改为后端存完整文本（仅清洗换行），截断与省略号全部由前端 CSS ellipsis
  负责，hover 时 `:title` 展示完整原文。

### 验证
- 66 passed（test_agent_loop + test_api_ws + test_session_export_fork +
  test_plugin_computer_use）；vue-tsc 0 错。

## [0.0.25] - 2026-09-29

轨迹页（Tracing）可读性改造 + 标题生成计入 model_call。

### 改动
- **标题生成计入轨迹**：`ws/chat.py` 的 `_auto_generate_title` 此前独立调一次 LLM
  却不落 span，导致轨迹页里 model_call 次数与 DeepSeek 后台对不上（首条提问实际
  多一次标题生成调用）。现在标题生成会作为 `kind=model`、`name=title_generation`
  的 span 挂到当前 run 的 trace 下，耗时与 token 一并计入。
- **run span 展示名**：从固定 `agent_run` 改为「会话标题 - 用户问题」（`agent_loop.py`
  `_build_run_title`），两部分各截断到约 24 字加省略号；会话仍为 New Session 时
  退化为问题本身。
- **前端 `SettingsView.vue`**：
  - run 名按「 - 」拆成标题/问题两段，各自 `text-overflow: ellipsis`，hover 各自
    显示全文；
  - 去掉头部 `span N` 计数（用户要求）；
  - token 数字格式化：≥1M 显示 `X.XXM`、≥1K 显示 `X.XK`、其余千分位逗号。
- `AgentLoopResult` 新增 `trace_id` 字段，供 WS 层把标题生成 span 挂到正确 trace。

### 验证
- vue-tsc 0 错；ruff / mypy 零新增；test_agent_loop + test_api_ws 通过。

## [0.0.24] - 2026-09-29

一次提问只出一个答案气泡（过渡语气泡归并进执行过程）。

### 背景
用户实测「桌面新建 andy2.txt 写入：1」，反馈：任务很快完成、但同一个提问在界面上
回答了**两次**——先是一句「我来帮您在桌面创建该文件。」，执行过程后又来一句
「已完成 ✅…」。要求一次提问只回答一次（无论成功失败）。

### 根因
0.0.21 为修「答案正文被误藏进执行过程」定下的规则是「任何带正文的 assistant 消息
都渲染为可见气泡」。模型在工具调用前常先说一句过渡语（「我来帮您…」），工具返回后
再说最终结论——两句正文都成了独立气泡，于是一个提问显示两次回答。

### 修复
- **`frontend/src/views/ChatView.vue` `displayItems`**：改为先倒序扫描、按 user 消息
  分组，每组只把**最后一条带正文**的 assistant 标记为最终答案气泡；它之前的带正文
  assistant 消息（过渡语）不再单独占气泡，而是作为 `kind:'text'` 步骤并入「执行过程」
  过程框；所有 tool_calls 仍归并进执行过程。时间线变为：用户提问 → 执行过程（含
  过渡说明 + 工具调用）→ 唯一的最终答案气泡。
- 同时保住 0.0.21：若完整答案与收尾工具（如保存记忆）同轮发出，那条仍是本组最后一条
  带正文，照常渲染为答案气泡，其 tool_calls 归进执行过程，不会再丢正文。
- 同时保住 0.0.23：工具卡片不散落。

### 验证
- `vue-tsc -b --force` 0 错误。
- 纯前端归并逻辑，无后端改动、无新增测试依赖；历史会话刷新后即生效。

## [0.0.23] - 2026-09-29

「执行过程」归并修正（工具卡片统一入框）+ computer_use/code_runner 调用成本治理。

### 背景
用户以「在桌面新建 andy1.txt 写入：你好，安迪」实测，反馈两点：
1. 调用追踪（agent_run span）显示外层 6 次 `model_call`、累计 ~70k tokens——这正常吗？
2. 聊天页里 `computer_use` 与 `code_runner` 的工具卡片散落在气泡之间，没有进「执行过程」框。

### 分析（问题 1：调用次数）
结构上「每次工具调用后必须 model_call 决策下一步」属正常循环，但本次存在三处真实浪费：
- **重复执行**：外层模型同轮并行调用 `computer_use` + `code_runner`，computer_use 内部
  多次 LLM（51.63s，其 token 不计入外层 70k）规划并执行写文件，外层又用 code_runner 重做一遍。
- **白名单拦截空烧**：computer_use 内部计划用 `cmd /c echo ... > 文件`，而白名单按**首词**
  匹配且默认无 `cmd` → 被拦截，内部空烧重试。
- **路径探测多烧 3 轮**：code_runner 沙箱内 `Path.home()` 返回 `/`，模型逐轮探测桌面路径。

### 修复
- **前端 `ChatView.displayItems`**：带正文 assistant 消息仍渲染为可见答案气泡（保留 0.0.21
  修复），但其 `tool_calls` 不再由 MessageItem 内联展示，而是统一归并进「执行过程」过程框，
  时间线变为「说明气泡 → 执行过程 → 最终回答」。
- **前端 `MessageItem.vue`**：移除内联 tool-calls 卡片模板与样式（避免与执行过程重复显示）。
- **后端 `plugins/computer_use/agent.py`**：系统提示新增守则 10——command_exec 首词必须是
  白名单内命令本身（如 `echo`），严禁 `cmd /c` 等外壳前缀；写文件用 `echo 内容 > 路径`。
- **后端 `plugins/computer_use/main.py`**：工具描述新增【使用时机·省钱纪律】——纯文件读写/
  计算/代码执行类任务直接用 code_runner 等轻量工具，本工具只用于 GUI 交互场景；同一任务
  不得与本工具与 code_runner 并行/先后重复执行。
- **后端 `plugins/tool_code_runner/main.py`**：描述注入环境常识——Windows 用户目录
  `C:\Users\<用户名>`、桌面为其下 `Desktop`；沙箱内 `expanduser('~')` 可能返回 `/` 不可信，
  直接用绝对路径，省去多轮探测。
- **测试**：新增 `test_system_prompt_commands_use_whitelist_first_token`；computer_use +
  permissions 共 32 passed；ruff / mypy 零新增；vue-tsc 零错误、vite build 通过。

---

## [0.0.22] - 2026-09-29

Computer Use 去「浏览器专用」化，回归通用桌面 Agent 定位（browser use ⊂ computer use）。

### 背景
此前 [0.0.20] 为修「搜索落首页/推广页」在 `agent.py` 硬编码了百度搜索 URL 注入
（`_build_search_hint` + `_SEARCH_TRIGGERS`，并满篇 `百度/谷歌/baidu.com/刘德华`）。
这属于 browser-use 思维——替模型锁定了具体浏览器与网站，违背了「Computer Use 是目标、
Browser Use 只是其子集」的产品铁律。

### 修复
- **后端** `backend/plugins/computer_use/agent.py`：
  - 删除 `_SEARCH_TRIGGERS` 与 `_build_search_hint`，不再由服务端为某网站注入专属搜索 URL；
    `run()` 移除搜索提示注入分支；清理相关 `import re` / `from urllib.parse import quote`。
  - 系统提示与 `keyboard_type` 纠错文案改为 **App 无关**：去掉所有 `chrome`/`baidu`/`百度`/`刘德华`
    具体绑定，导航护栏改写为通用表述（「启动程序用 `start <程序名>`」「地址栏须输完整
    http(s):// 网址、不得裸域名」「搜索优先自己构造站点搜索 URL 而非点搜索框」）。
  - 新增模块级注释明确「本插件是通用 Computer Use，不为任何具体 App/网站硬编码」。
- **测试** `backend/tests/test_plugin_computer_use.py`：删除两条依赖百度注入的旧测试，
  新增 `test_system_prompt_is_app_agnostic`（断言系统提示不含 `baidu`、仍保留通用护栏）
  与 `test_run_does_not_inject_browser_specific_url`（断言执行消息不再注入 `baidu.com/s?wd=`）。

### 验证
- `pytest tests/test_plugin_computer_use.py` 25 passed（新增 2 替换旧 2）。
- `ruff` / `mypy` 零新增；`plugin.json` 合法。
- 项目长期约定（`MEMORY.md`）新增「产品范围铁律：Computer Use 是目标，Browser Use 是其子集」。

---

## [0.0.21] - 2026-09-29

修复「最后回答」丢失正文 + 问答操作条增强。

### 严重 Bug 修复：答案正文被误判为「执行过程」而丢失
- **根因**：`ChatView.displayItems` 规定"只要 assistant 消息附带 `tool_calls`，其 `content` 就整段归并进「执行过程」过程框、不渲染为最终答案"。当模型把完整答案（如江苏出发的外省游玩推荐，含 3 个追问问题）与某次工具调用（如保存记忆）同轮发出时，这段正文被藏进执行过程，末尾一条只有小结的短消息反而成了"最后回答"，导致用户看到的最终答案读不懂。
- **修复** `frontend/src/views/ChatView.vue`：改为"**任何带正文的 assistant 消息都渲染为可见答案气泡**，正文永远对外可见"；只有"无正文、仅含工具调用"的中间轮次才进入执行过程框。带正文消息的 `tool_calls` 由 `MessageItem` 内联卡片展示，避免重复。彻底根治"执行过程"与"最后回答"混淆。
- 验证：`vue-tsc -b --force` 零错误；`vite build` 通过。

### 问答操作条增强（`frontend/src/components/MessageItem.vue` + `ChatView.vue`）
- 每条 assistant 回答的操作条新增「重新回答」按钮（位于复制之后、追问之前）：原位删除该轮问答并用原问题重新生成（调用 `chatStore.deleteTurn` + `sendMessage`），不波及后续轮次。
- 复制回答成功后出现「复制成功」提示，3 秒后自动淡出（原为 1.2 秒的 `已复制` 标题提示）。

---

## [0.0.20] - 2026-09-29

修复 Computer Use 两类真实运行问题（搜索落地错误 + 外层 Agent 编造"无法操控"。

### 修复
- **搜索落地错误（症状 A）**：模型此前把裸域名 `baidu.com` 输进地址栏，打开的是百度首页/
  浏览器推广页而非搜索结果。改为**服务端确定性注入**——新增 `_build_search_hint`：当目标
  疑似「在百度搜索 X」时，直接从文本抽取查询词并给出完整搜索网址
  `https://www.baidu.com/s?wd=<urlencoded 查询词>`，作为 user 消息注入执行阶段，强制模型
  keyboard_type 完整网址（不再依赖模型自己拼 URL）。系统提示第 2 条同步加「铁律：
  地址栏必须输以 http 开头的完整网址，严禁裸域名」+ 回车后必须截图确认 `/s?wd=` 的验证步骤。
- **外层 Agent 编造失败（症状 B）**：内层 computer_use 其实已真实操作桌面（Chrome 已开、
  键盘已在地址栏打字），但返回不确定摘要后，**外层主对话 Agent 脑补**出"我无法操控本机
  Chrome/桌面不可用/无法执行 start chrome"并擅自切去联网搜索。修复：① 给 `computer_use`
  工具描述加【转述纪律】，强制外层 Agent 如实转述、未出现 `[桌面不可用]`/`未配置 API Key`
  字样前不得声称"无法操控"、不得用联网搜索替代；② `run()` 返回结果前加**事实状态头**
  （"本机桌面自动化已实际执行（鼠标/键盘/程序已真实操作）" 或 "不可用"），锚定外层 Agent。

### 测试
- 新增 `test_search_hint_extracts_query`、`test_search_hint_injected_into_messages`。
- `pytest tests/test_plugin_computer_use.py` → 25 passed；ruff / mypy 零新增问题。

---

## [0.0.19] - 2026-09-29

降低 Computer Use 的模型调用成本（P0 + P1）。

### 优化
- **P0 截图不累积**：截图只作为「下一轮的一次性观察」发出，处理完即从持久历史丢弃，
  不再被后续每次调用反复重发——同时砍掉「轮数失控」和「每轮上下文体积」两个成本爆炸点。
- **P0 调用次数硬预算**：新增 `max_llm_calls`（默认 15）作为模型调用次数硬上限，超出即安全中止，
  给资金成本上一道直观的闸；`max_iterations` 默认从 30 下调到 15（历史字段，现由预算兜底）。
- **P1 计划先行**：新增 `plan_first`（默认开），执行前先用一次「禁用工具」的调用产出步骤清单，
  让后续执行更线性、少走弯路，并把计划随结果回传（成本透明，便于用户先看计划再决定重跑）。
- **P1 步骤合并**：系统提示新增「步骤合并」指引，鼓励模型把连续、顺序相关的步骤用多个
  tool_calls 合并到同一轮返回，减少来回调用次数。
- 卡死检测（连续相同操作提前中止）与输入纠偏（防止把整段指令当输入内容）已具备并保留。

### 测试
- 插件测试 23 passed（原 20 + 输入纠偏 1 + 计划先行 2）；ruff / mypy 零新增。
- 新增 `test_plan_first_prepends_plan`（计划随结果回传）与
  `test_plan_first_falls_back_on_error`（计划阶段失败优雅降级，不阻塞执行）。

---

## [0.0.18] - 2026-09-29

修复 Computer Use 自动化把「整段任务指令」误当成搜索词/文本填入输入框的问题（典型故障：在百度首页搜索框里填入了「打开本地chrome浏览器…点击按钮：百度一下」整句指令，导致任务跑满轮数失败）。

### 修复
- **后端** `backend/plugins/computer_use/agent.py`：
  - `_SYSTEM_PROMPT` 新增「文本输入铁律」与「搜索网站最稳做法」两节，明确 `keyboard_type` 的 `text` 只能填「要写进当前输入框的内容本身」，绝不能是任务指令/步骤/思考；并推荐搜索引擎走地址栏直接输入 `https://www.baidu.com/s?wd=关键词` 的方式，绕开搜索框定位。
  - `ComputerUseAgent` 记录 `self._goal`，并在 `keyboard_type` 分发处新增纠偏：当要输入的文本与任务指令原文高度重合（长度 >12 且等于或包含 goal）时，拒绝真正打字，返回纠正提示并回灌给模型重填正确内容。
  - `run()` 入口填充 `self._goal`，`__init__` 增加默认值以防直接调用 `_dispatch` 时缺字段。
- **测试** `backend/tests/test_plugin_computer_use.py`：新增 `test_keyboard_type_rejects_full_instruction`，验证整段指令被拦截、正确短内容正常放行。

### 验证
- `backend`：`pytest tests/test_plugin_computer_use.py` 21 passed（原 20 + 新增 1）
- `ruff check plugins/computer_use/agent.py` 通过；`mypy plugins/computer_use/agent.py` 无问题

---

## [0.0.17] - 2026-09-29

修复插件配置页配置项标题不随系统语言显示的问题（此前一律显示英文变量名）。

### 修复

- **前端** `frontend/src/views/SettingsView.vue`：
  - `getConfigSchemaProperties` 现额外透传每个属性的 `title`（中文标题）与 `title_en`（英文标题）
  - 新增 `fieldTitle(prop)`：依据 `settingsStore.language` 选择标题——系统语言为 `zh` 时显示 `title`，为 `en` 时显示 `title_en`，两者均未提供则回退字段名（`key`）
  - 模板 label 由直接渲染 `prop.key` 改为 `fieldTitle(prop)`，描述保留为括号内补充
- **后端配置数据**：为全部 7 个带 `config_schema` 的内置插件（`computer_use` / `context_manager` / `jev_manager` / `provider_deepseek` / `provider_doubao` / `provider_qwen` / `tool_code_runner`）的每个配置项补充 `title`（中文）与 `title_en`（英文）
  - 后端 `kernel/loader.py` 原样透传 `manifest.config_schema`，无需改动；新增标题字段不影响默认值应用

### 验证

- `backend`：`pytest tests/test_health.py tests/test_api_rest.py` 12 passed（schema 改动下应用正常启动）
- `frontend`：`CI=true pnpm exec vue-tsc -b --force` 零错误；`pnpm exec vite build` 通过

---

## [0.0.16] - 2026-09-29

新增内置插件 `computer_use`：基于 DeepSeek V4.1 Flash 的 computer use（GUI 自动化）能力，以内建插件形式集成，复用项目既有 ToolPlugin 契约、Provider 注册与审批 REST 约定。

### 新增

- **插件目录** `backend/plugins/computer_use/`：
  - `plugin.json`：声明 `tool` 类型、`core: false`、config_schema（API Key / Base URL / 模型名 / 最大轮数 / 超时 / 截图质量等全部可配置，默认值从环境变量回退）
  - `config.py`：三层注入（plugin.conf 覆盖 > 环境变量 > 内置默认），**禁止硬编码**；桌面依赖（mss / pyautogui）缺失时自动降级为 `DummyDesktop`，插件仍可加载、仅工具报错
  - `safety.py`：风险分级（command_exec / file_write / file_read / keyboard / mouse / screenshot）、`deny_commands`/`deny_paths` 硬性拦截、`allow_commands`/`allow_paths` 作用域约束、`approval_mode`（whitelist 静默放行 / interactive 挂起人工授权）
  - `desktop.py`：`Desktop` 抽象（截图 / 鼠标 / 键盘 / 命令执行 / 文件读写），真实实现走 mss+pyautogui，测试走 `FakeDesktop`
  - `client.py`：DeepSeek V4.1 Flash 非流式 function-calling 客户端（OpenAI 兼容 `/chat/completions`），含重试与超时；`FakeModelClient` 供测试
  - `agent.py`：核心 Agent 循环——系统提示、消息拼接（含截图多模态 image_url 注入）、工具调用解析、执行结果回填、单步/整体超时、失败重试、结果截断、异常兜底、最大轮数与显式 `finish` 终止
  - `main.py`：`ComputerUsePlugin`（ToolPlugin），注册 7 个工具（screenshot / mouse / keyboard / file_read / file_write / command_exec / finish），按 `needs_session` 透传 session，暴露 `computer_use_approve` 审批 REST 路由（与项目既有 `request_user_approval` 约定同源）
  - `tests/test_plugin_computer_use.py`：14 个用例，覆盖安全拦截、白名单放行、交互式授权、截断、超时、最大轮数终止、多模态截图回填

### 验证

- `ruff check`：插件与测试零新增错误
- `pytest tests/test_plugin_computer_use.py`：14 passed
- `pytest tests/test_health.py tests/test_api_rest.py`：插件自动激活下应用正常启动，无回归
- 说明：真实桌面自动化需在本机安装可选依赖 `mss` / `pyautogui`（受项目"依赖落到项目目录"约束，未全局预装）；纯逻辑与既有 API 链路已用替身全面验证

### 修复（同一插件，后续补丁）

- **computer_use 一直报「未配置 API Key」**：根因是项目 DeepSeek 密钥存于数据库（加密字段 `api_key_encrypted`），运行进程内没有 `DEEPSEEK_API_KEY` 环境变量，而插件只读 `COMPUTER_USE_API_KEY` / `DEEPSEEK_API_KEY` 两个变量 → 全部为空。
  - **修复**：`ComputerUseService` 新增 `credential_resolver`，在工具**真正执行时**（而非激活时，避开插件激活顺序）从项目 `ProviderRegistry` 取已加载的 deepseek provider 的 `api_key` / `base_url` 作为最终兜底；仅读取、绝不写入共享 provider 单例。优先级：插件配置 > 环境变量 `COMPUTER_USE_API_KEY`/`DEEPSEEK_API_KEY` > 复用「设置页」录入的 DeepSeek 密钥。未配置时给出明确的三选一指引。
  - 新增 3 项回归测试（resolver 兜底、resolver 为空仍报错、显式 key 优先），`test_plugin_computer_use.py` 现 17 项全过；连同应用启动共 29 项通过，mypy / ruff 零新增。

---

## [0.0.15] - 2026-09-28

系统性端到端审查后的修复批次。审查结论见 `docs/review-plan-2026-09-28.md`（16 项，P0×2 / P1×5 / P2×9），
本批次落地其中全部 P0、全部 P1 与 6 项 P2。

### 修复

#### P0-1 共享 Provider 单例被逐连接 monkey patch（跨会话内容串流）
- **现象**：两个标签页/连接同时对话时，A 的回答会出现在 B 的界面上；严重时某个连接彻底收不到流式 token
- **根因**：`ws/chat.py` 对每个连接执行 `provider.chat = streaming_chat`，而
  `ProviderRegistry.get_provider()` 返回的是**按 provider_id 缓存的共享单例**。并发连接互相嵌套包装，
  一次响应被多个 wrapper 发往不同 socket；谁先结束还会把别人刚打好的补丁回滚掉
- **修复**：新增 `_StreamingProviderProxy`（`__getattr__` 委托 + 覆写 `chat`），每个连接持有自己的代理对象
  并传给 `AgentLoop`，**真实 provider 实例保持不可变**

#### P0-2 WS 未连接时消息被静默丢弃，界面永久锁死
- **现象**：握手完成前发送（弱网/后端刚重启），输入框、附件、发送按钮全部变灰，无提示，只能刷新页面
- **根因**：`client.ts` 的 `ws.send` 只有 `if OPEN { send }`，**无 else 分支**（不抛错/不排队/不回调）；
  而 chat store 已提前把 `isStreaming` 置 true，服务端没收到请求、也不会回帧，状态永远复位不了
- **修复**：`ws.send` 返回布尔值并在 `CONNECTING` 时入队（`onopen` 补发），不可用时返回 false；
  store 先确认可发送再落地乐观状态，发送失败不留下乐观消息；新增 30s「首个响应看门狗」兜底复位

#### P1 附件与既有链路的衔接闭环（5 项）
- **P1-1 切会话携带他人附件**：`selectSession` 未清空 `pendingAttachments`，服务端按 session 归属
  安全丢弃却**只写日志**。现在切会话清空待发附件，服务端额外下发 `attachment_warning` 帧，
  前端以提示条明确告知「附件未随本次提问送达」
- **P1-2 删轮不回收附件**：`delete_turn` / `delete_message` 只删消息行，附件文件与元数据成为双重孤儿
  （磁盘无界增长且文件仍可下载）。新增 `_purge_attachments`，删除前回收文件与 `attachments` 行；
  配套 `AttachmentRepository.delete_by_ids`
- **P1-3 分叉/导入丢附件**：导出含附件而导入/分叉不带，往返不对称。新增
  `_clone_attachments_by_ids` 做**物理复制**（新 id + 新文件），两边互不影响
- **P1-4 上传先兜内存再判数量**：`MAX_FILES_PER_MESSAGE` 校验发生在 `await f.read()` 之后，
  单次 POST 上千文件即可堆出数百 MB 才返回 400；且 `store_file` 成功、`repo.create` 失败会留孤儿文件。
  现在数量前置校验 + 落盘/登记失败整体回滚
- **P1-5 切会话竞态**：`selectSession` 无时序守卫，慢请求回包会覆盖新会话视图；且未清理
  思维链/过程框。新增 `selectSeq` 守卫并补齐复位字段

### 优化（P2）
- **P2-1** `ext_of` 对点文件取不到后缀（`splitext('.gitignore') → ''`），使白名单中的
  `.gitignore` / `.env.example` 成为死条目，而前端正则能放行 → 前端允许、后端拒绝改为一致通过
- **P2-2** 移除 `AgentLoop` 之前那次必然作废的 `ContextService.build`（0/4096 快照随后被覆盖），
  每轮少一次全量上下文构建
- **P2-3** `list_messages` 支持 `limit` / `offset` 分页（不传时行为不变；tool→结果映射仍基于全量）
- **P2-4** 附件缩略图：固定骨架尺寸 + `loading="lazy"` + 加载失败降级占位（不再显示浏览器破图）
- **P2-5** 统一 `ApiError`（保留 status / code / 可读 message，区分网络层失败），替换
  各处 `throw new Error(await res.text())`
- **P2-6** `SessionService` 协议补上 `attachments` 参数，消除协议与实现的签名漂移
- **P2-7** 客户端在等待人工确认时断开，挂起的工具不再白等满 120s，立即按「拒绝」释放
- **P2-8** `FileResponse` 使用净化后的文件名（原名仅用于展示，不参与路径）
- **P2-9** 会话标题生成的兜底模型改为「该 provider 的默认模型」，不再写死厂商 id

### 测试
- `tests/test_api_attachments.py` 增加 5 项回归：删轮回收附件、分叉副本独立、数量超限、
  点文件名可用、消息分页（22 passed）
- `tests/test_api_ws.py` 增加 4 项 P0-1 回归：代理既 emit 又 yield、真实 provider 不被改写、
  两个代理互不干扰、非 chat 属性透传（6 passed）
- `tests/conftest.py` 增加 `HARNESS_ATTACHMENTS_DIR` 隔离，避免附件测试写真实 `data/attachments`

### 验证
- pytest 全量 **368 passed**；mypy 回基线 14；ruff 新增代码 0 问题；`vue-tsc` 0 错；`vite build` 通过
- 真实启动冒烟（`smoke_http.py`，临时库 + 临时附件目录）：全部通过
- 产物自查：`src` 下无 `.js` 影子文件，构建产物确含 `attachment_warning` 与看门狗文案

---

## [0.0.14] - 2026-09-28

### 修复

#### 撞工具迭代上限后无最终回答（定时任务"成功"却看不到结果）
- **现象**：定时任务「每日大模型新闻早报」立即执行后显示成功，但会话里只有几十条
  web_search / web_fetch 工具消息，没有任何最终报告
- **根因**：`AgentLoop` 的 `while...else` 在撞到 `max_tool_iterations` 上限后只打了一条
  warning 就结束，而 `result.content` 还是最后一轮（空内容）的残留值，于是「终答持久化」
  被跳过——会话停在悬空的工具结果上。该缺陷同样影响普通聊天（任何跑满迭代数的对话都拿不到终答）
- **修复**：撞上限后**强制一次无工具收尾调用**——追加一条"已达工具调用上限，禁止再调工具，
  请基于已有信息直接输出最终回答"的指令消息，并以 `allow_tools=False` 调用模型（不注入
  工具定义），确保模型只能输出文字终答；终答照常落库、usage 并入统计。收尾调用失败不视为
  整体失败（前 N 轮工具结果已落库）
- **配套**：定时任务 runner 在「状态 ok 但无终答」时，运行历史的 summary 改为明确说明
  （"运行完成（N 次迭代）但未生成最终回答"），不再留空
- 测试：新增 2 项回归（撞上限必须产出并落库终答；收尾调用失败不影响既有工具结果），
  AgentLoop+Scheduler 28 项通过；真实端到端两次重跑新闻任务，会话分别产出 1673 / 1338 字
  最终报告（模型还诚实标注了撞上限与文件未落盘的原因）

---

## [0.0.13] - 2026-09-28

### 新增

#### 会话文件传输（文档 + 图片）
让对话支持携带附件：用户可在聊天输入区上传**文档**与**图片**，随消息一起发给模型。

- **支持类型**
  - 文档：txt / md / markdown / csv / json / yaml / yml / log / py / js / ts / tsx / jsx /
    html / htm / xml / ini / toml / cfg / tex / rst / sh / bat / ps1 / env.example / gitignore
  - 图片：png / jpg / jpeg / gif / webp / bmp
- **数量与大小上限**（防 token 爆炸，均可由 `limits.py` 调整）
  - 单条消息：文档 ≤ 5、图片 ≤ 4、文件总数 ≤ 8
  - 单个文档 ≤ 200KB；单张图片 ≤ 1.5MB，且最长边自动缩放到 1280px（Pillow）以控制视觉 token
- **多模态注入**：文档按「`[文档 文件名]\n<正文>`」内联为文本块；图片缩放后转 base64
  data URL，按 OpenAI 视觉规范作为 `image_url` 块——`ContextService.build` 在压缩之后统一渲染，
  无附件的消息保持纯字符串 content，不影响既有链路
- **上传即校验**：REST 上传时逐个分类校验（类型/大小/数量），任一不合规整批拒绝（400 + 错误码
  `ATTACHMENT_INVALID` / `ATTACHMENT_LIMIT`）；文件存于 `data/attachments/<session_id>/`，
  公开元信息不含存储路径
- **回传与缩略图**：`GET /api/sessions/{id}/attachments/{att_id}` 按原始 mime 回传（图片可作缩略图），
  并校验附件归属会话（跨会话取他人附件返回 404）
- **生命周期**：删除会话时连带清理附件物理文件与数据库行（`delete_session` 内 rmtree + `delete_by_session`）
- **REST**：`POST /api/sessions/{id}/attachments`（multipart 多文件）、`GET /api/sessions/{id}/attachments/{att_id}`
- **WS**：`/ws/chat` 消息体新增 `attachments` 字段（引用已上传附件 id），服务端校验归属后注入 `AgentLoop.run`
- **前端**：聊天输入区新增「📎 附件」按钮 + 隐藏 file input；已选附件以 chip 展示（图片缩略图 / 文档图标 +
  文件名 + 大小 + 移除按钮）；前端预校验类型与「文档/图片/总数」三类上限；允许「只发附件不带文字」；
  用户消息气泡内渲染附件（图片缩略图可点大图、文档 chip）
- 存储：新增 `attachments` 表；`messages` 表新增 `attachments` 列（`ALTER TABLE` 旧库自动升级）
- 依赖：新增 `python-multipart`（FastAPI 文件上传所需），已写入 `pyproject.toml`

### 修复
- **测试污染真实数据**：`tests/conftest.py` 此前隔离了 DB / 工件 / MCP 配置，但**漏了附件目录**，
  导致附件测试把上传文件写进真实的 `data/attachments/`（累计 39 个垃圾文件夹）。已补
  `HARNESS_ATTACHMENTS_DIR` 隔离，并把残留垃圾可逆移动到 `data/.trash_attachments_junk/`

### 验证
- 后端测试 **357 passed**（340 → 357，新增 17 项：上传/类型/大小/数量校验、回传、跨会话越权、
  消息持久化、`ContextService` 多模态渲染、纯文本不变、会话删除清理附件）
- ruff：附件相关新代码 **0 新增**（顺手修掉 `context_manager` 一处既有 E501）；mypy 14 与基线持平
- **真实端到端（deepseek-v4-flash）**：上传 73 字节文档（含唯一代号 `BANANA-42`）→ 模型准确答出
  `BANANA-42`；上传 92 字节纯红 PNG → 模型答出「红色」，证明**文档文本注入与图片视觉多模态均真实生效**；
  两会话用后即删，真实库无残留
- 前端 `vue-tsc` + `vite build` 通过，新文案（移除附件 / 单次最多添加 / 附件上传失败等）已确认入包

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
