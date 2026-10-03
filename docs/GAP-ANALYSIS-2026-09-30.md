# andy-harness 缺口分析（2026-09-30）

> 对照「设计目标 / 需求说明 / 路线图」与当前代码，梳理**尚未实现或半成品**的重要能力。  
> 每项说明：功能名称与预期作用 → 当前状态 → 影响程度 → 建议优先级。  
> 并区分「核心必备」与「可选增强」，最后回答「是否存在阻塞性缺失」。

---

## 0. 审计范围与方法

| 维度       | 内容                                                                                                                                                                                 |
| -------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 需求/路线图来源 | `README.md`、`01-architecture.md`（含 §11 演进路线）、`02-development-plan.md`、`ENHANCEMENTS.md`（P0–P3 清单 + §2 差距表）、`docs/review-plan-2026-09-28.md`、`CHANGELOG.md`                         |
| 代码范围     | `backend/harness/`（kernel / engine / modules / api / infra）、`backend/plugins/`（30 个插件）、`backend/marketplace`、`backend/skill_marketplace`、`backend/mcp_marketplace`、`frontend/src/` |
| 实测验证     | 全量 `pytest`：**412 passed**（7m50s）；ruff / mypy 未新增；`vue-tsc` + `vite build` 通过；对 5 个内置 MCP 与 libgen 做过真实握手                                                                          |

**一句话结论**：主链路（对话 ↔ 模型 ↔ 工具 ↔ 落库）**无阻塞性缺失**；但产品定位（Computer Use）与安全性上有 3 项准阻塞/核心缺口，另有 6 项规划能力（P2-1/2-2/2-3/2-4、P3-1/3-2）完全未实现，文档债务较重。

---

## 1. 核心必备（建议优先处理）

### G1 · Computer Use 运行时依赖未纳入依赖声明 🔴 **准阻塞**

| 项    | 内容                                                                                                                                                                                                      |
| ---- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 预期作用 | 桌面操控（截图 / 鼠标 / 键盘 / 文件读写 / 命令执行 / wait / finish 全套原语）开箱即用，这是项目**核心产品定位**（Computer Use 为目标，Browser Use 是其子集）                                                                                             |
| 当前状态 | `plugins/computer_use/` 能力完整（`desktop.py` 基于 pyautogui + mss，`agent.py` 含 10+ 原语与计划先行/降本策略），但 `pyautogui`、`mss` **既不在 `pyproject.toml` 的 `dependencies`，也不在 `requirements-sandbox.txt`**；当前 venv 里是手工装的 |
| 影响程度 | 🔴 **准阻塞**：全新克隆按 README 执行 `uv sync` 后，`RealDesktopController.available` 为 `False`，工具返回「桌面不可用：未安装 pyautogui/mss」——核心能力在新环境直接不可用。当前仓库能跑纯属本机手工安装                                                          |
| 建议   | 加入 `pyproject.toml` 的可选 extra（如 `desktop = ["pyautogui", "mss"]`）并写进 README；或在安装/启动时给出明确的安装指引                                                                                                           |
| 优先级  | **P0**                                                                                                                                                                                                  |

### G2 · Windows 沙箱：既无资源限制，也未真正终止进程树 🔴

| 项    | 内容                                                                                                                                                                                                                                |
| ---- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 预期作用 | 架构 §4.5 与 README 承诺「Windows 用 psutil 做进程级内存限制」「进程树终止」；Phase 8 DoD 要求「连续执行后宿主机无残留进程/临时文件泄漏」                                                                                                                                        |
| 当前状态 | `sandbox_manager/service.py:_get_preexec_fn` 只对 `Linux/Darwin` 返回 RLIMIT，**Windows 分支直接 `return None`（仅靠超时）**；`_kill_process_tree` 在 Windows 只 `proc.kill()`，不枚举/杀子孙进程（Unix 才用 `killpg`）。全仓 `psutil` **仅出现在注释里，从未 import，也未声明依赖** |
| 影响程度 | 🔴 安全底线弱于文档承诺：**唯一实测验证的平台（Windows 10）上沙箱没有内存/CPU 限制**，且被执行的脚本派生的子进程会残留。用户按文档信任其隔离强度会误判                                                                                                                                            |
| 建议   | 二选一：① 实现 Windows 侧限制（psutil 或 Job Object）+ `taskkill /T /F` 杀树，并补上依赖；② 若暂不实现，先修正文档，避免「声称有、实际无」                                                                                                                                    |
| 优先级  | **P0**                                                                                                                                                                                                                            |

### G3 · SQLite 并发：无 WAL、无连接锁 🟠

| 项    | 内容                                                                                                                                                                 |
| ---- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| 预期作用 | 多处并发写入下稳定：WS 对话流、REST 管理面、定时任务执行器、长期记忆自动总结会**同时写库**                                                                                                                |
| 当前状态 | `database.py` 用**单连接** `sqlite3.connect(..., check_same_thread=False)`，仅 `PRAGMA foreign_keys = ON`；**无 `journal_mode=WAL`、无显式 `busy_timeout`、也无任何 `Lock`** 保护共享连接 |
| 影响程度 | 🟠 单用户串行无感；多会话并行 / 后台任务同时写时可能触发 `database is locked`、事务交错或 `ProgrammingError`。是"平时看不出来、并发一来就炸"的隐患                                                                  |
| 建议   | 开启 WAL + `busy_timeout`，并对写操作加互斥（或改为按请求建连接）                                                                                                                        |
| 优先级  | **P1（健壮性）**                                                                                                                                                        |

### G4 · Durable Execution / Checkpoint 断点续跑（ENHANCEMENTS P2-1）🟠

| 项    | 内容                                                                                                                                                         |
| ---- | ---------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 预期作用 | 长任务中断后可从断点续跑，服务重启不丢进度（LangGraph checkpointing / OpenAI snapshot 的做法）                                                                                       |
| 当前状态 | 全仓检索 `agent_runs` / `resume` / `checkpoint` / `snapshot_rehydrate` **零命中**。唯一近似的半成品是 `computer_use` 的 `resume_run_id`（仅用于 interactive 模式下人工授权后续跑），不是通用断点续跑 |
| 影响程度 | 🟠 长链路任务（computer_use 多轮操作、定时任务）一旦中断/超时/重启即前功尽弃。历史上出现过「跑满 30 轮被终止」「429 中断后需重跑」的真实损失                                                                        |
| 建议   | AgentLoop 每轮快照 → `agent_runs` 表 → `resume(run_id)`                                                                                                         |
| 优先级  | **P1**                                                                                                                                                     |

### G5 · 文档与实现脱节（含 Computer Use 未入需求文档）🟠

| 项    | 内容                                                                                                                                                                                                                                                    |
| ---- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 预期作用 | 需求说明 / 路线图应能正确反映能力边界，供二次开发者判断                                                                                                                                                                                                                         |
| 当前状态 | ① `ENHANCEMENTS.md §2 差距表`仍把 **Skills / HIL 审批 / Todo / MCP / 子代理 / 长期记忆 / 会话分支** 标为「❌ 无」，这些**均已实现**；② `README.md` 全文**零提及** Computer Use、技能市场、MCP 市场三个最新核心能力（`grep -c` 命中 0）；③ `ENHANCEMENTS.md` 对 computer（Computer Use）零提及——**项目核心目标完全没进需求/路线图文档** |
| 影响程度 | 🟠 不阻塞运行，但会让二次开发者/未来的自己严重误判能力边界，也是本次审计花最多时间的地方                                                                                                                                                                                                        |
| 建议   | 更新 §2 差距表与 README；把 Computer Use 作为一等目标补进 ENHANCEMENTS 并给出验收标准                                                                                                                                                                                        |
| 优先级  | **P1（成本低，建议顺手做）**                                                                                                                                                                                                                                     |

---

## 2. 可选增强（规划中的 P2/P3 与体验型能力）

| 编号  | 功能                               | 预期作用                              | 当前状态                                                                                    | 影响                    | 优先级 |
| --- | -------------------------------- | --------------------------------- | --------------------------------------------------------------------------------------- | --------------------- | --- |
| E1  | **多渠道 Channel**（P2-3）            | 把 Agent 接进飞书 / Telegram / Webhook | `ChannelManager` + `channel_links` 会话映射已实现，Webhook 同步 + Telegram 长轮询可用；飞书渠道待按同模式补（见 §6 E1） | 低：主链路已通，飞书为增量 | P3 |
| E2  | **Docker 沙箱后端**（P2-4）            | 容器级真隔离、网络开关、镜像白名单                 | `sandbox_manager` 文档字符串明确写「DockerBackend: 后期实现」，无实现                                     | 中高：本机子进程隔离强度有限        | P2  |
| E3  | **结构化 compaction v2**（P2-2）      | 更省更准的压缩（保留最近 N 轮 + 结构化事件日志）       | 仅 `SlidingWindowStrategy` + `SummaryCompressionStrategy` 两个策略，无 v2                      | 中：成本/准确率优化            | P2  |
| E4  | **本地模型直达 Ollama**（P3-2）          | 零成本、隐私，BYOK                       | 仅 `provider_deepseek` / `provider_qwen` / `provider_doubao` 三个 provider 插件              | 中：README 已自承"缺本地模型入口" | P2  |
| E5  | **回归评测 Eval Harness**（P3-1）      | 改动后有客观回归基线与成功率/成本门槛               | 无 `tests/evals/`，无打分脚本、无 CI 门槛                                                          | 中：长期质量保障              | P2  |
| E6  | **插件从 zip / git 安装**（架构 v0.2）    | 生态分发                              | 市场仅支持「本地目录复制」，`plugins.py` / `loader.py` 无 `zipfile` / git 支持                           | 中：市场只能内置，不能外部分发       | P2  |
| E7  | **语义 / 向量记忆检索**                  | 按语义而非关键词召回长期记忆                    | `memories.embedding` 列**已预留**，检索仍是 SQLite `LIKE` 关键词                                    | 低中：召回质量               | P3  |
| E8  | **前端自动化测试**                      | 前端回归保护                            | `frontend/package.json` 无 vitest/jest，scripts 只有 dev/build/lint/format，**前端零自动化测试**     | 中：前端改动只能靠手点验证         | P2  |
| E9  | **会话 / 消息搜索**                    | 历史内容检索                            | `SessionSidebar.vue` 无搜索框，无搜索 API 使用痕迹                                                  | 低中：会话多了以后体验           | P3  |
| E10 | **多 Agent 协作 / 并行子代理**（架构 v0.3+） | 并行委派、角色协作                         | `OrchestratorService` 已实现：`parallel`（并发上限 + 按序返回 + 异常隔离）与 `pipeline`（角色流水线接力，`{input}` 传递），基于 SubagentService 复用独立会话（见 §6 E10） | 低：差异化能力已落地 | P3 |
| E11 | **桌面壳 Tauri**（架构 v0.3+）          | 桌面应用形态                            | `desktop/` 工程已落地：Tauri 2 配置 + Rust 壳（选端口 / 拉起后端 / 退出回收进程树）+ 后端启动器，复用前端产物；CORS 与 Tauri 运行时解析已接入；需 Rust 工具链后 `tauri build`（见 §6 E11） | 低：工程就绪，待工具链编译 | P3 |
| E12 | **认证与多用户**                       | 多用户部署                             | 已落地：认证默认关闭、行为零变化；`HARNESS_AUTH=1` 后全站 `/api/**` Bearer 鉴权，会话与记忆按用户隔离（见 §6 E12）               | 低：本地单用户无感，需要时一键开启   | 已实现 |


## 4. 建议落地顺序

```
P0:  G1 Computer Use 依赖入声明（+ README 说明）      ← 半天内
     G2 Windows 沙箱资源限制 + 进程树终止（或先修文档）
P1:  G3 SQLite WAL + busy_timeout + 写锁
     G4 Checkpoint / 断点续跑
     G5 文档同步（ENHANCEMENTS §2 / README / Computer Use 入册）
P2:  E1 Channel 插件化 → E2 Docker 沙箱 → E3 compaction v2
     → E4 Ollama → E5 Eval → E6 zip/git 安装 → E8 前端测试
P3:  E7 向量记忆 → E9 搜索 → E10 多 Agent → E11 桌面壳
已实现: E12 认证与多用户（默认关闭，HARNESS_AUTH=1 开启）
```

---

## 5. 附：本次审计的"已完成/无需再列入"核对

避免重复劳动，以下项经核查**已实现**，不应再当作缺口：

- P0-1 Skills / P0-2 审批与权限 / P0-3 Todo / P0-4 导出·导入·fork
- P1-1 MCP（stdio + SSE + streamable-http）/ P1-2 子代理 / P1-3 长期记忆 / P1-4 大输出 offload / P1-5 Tracing
- 定时任务、会话文件传输（文档 + 图片）、记忆自动总结、插件市场、**技能市场**、**MCP 市场**
- `docs/review-plan-2026-09-28.md` 的 16 项（P0×2 / P1×5 / P2×9）已在 `CHANGELOG [0.0.15]` 全部修复，本次**未**重复列入
- i18n 中英文键数量对齐（294 / 294）

---

## 6. 落地进度（2026-09-30）

按 §4 建议顺序已逐条落地，全部完成：

| 编号     | 能力                             | 落地要点                                                                                                                                                                                                      | 验证                                         |
| ------ | ------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------ |
| **G1** | Computer Use 依赖入声明             | `pyproject.toml` 新增 `desktop = ["pyautogui>=0.9.54","mss>=9.0.1"]` extra；README 写明安装方式                                                                                                                    | ruff / mypy 通过；README 已补 Computer Use 说明   |
| **G2** | Windows 沙箱资源限制 + 进程树终止         | `sandbox_manager/service.py` 用 Job Object 施加 512MB 内存上限 + `taskkill /F /T /PID` 杀整棵树；Unix 保留 `os.killpg`；新增 `tests/test_sandbox.py`                                                                       | `test_sandbox.py` 12 passed；ruff / mypy 通过 |
| **G3** | SQLite WAL + busy_timeout + 写锁 | `database.py` 开启 WAL + `busy_timeout=5000` + `threading.RLock` 包裹共享连接读写关；新增 `tests/test_database_concurrency.py`                                                                                          | 并发 8 线程 ×50 写无损坏；WAL/busy_timeout 断言通过     |
| **G4** | Checkpoint 断点续跑                | `engine/checkpoint.py`（`RunCheckpointService` + `resume_run`）、`agent_loop.py` 四处 best-effort 写点、`api/rest/runs.py`（`POST /api/runs/{run_id}/resume`）、`agent_runs` 表、main 接线；新增 `tests/test_checkpoint.py` | 10 passed；ruff 全绿；mypy 未新增（顺带修 3 个历史错误）    |
| **G5** | 文档同步                           | ENHANCEMENTS §2 差距表 7 项 ❌→✅（Skills/HIL/Todo/MCP/子代理/长期记忆/会话分支）+ Checkpoint/Computer Use/三市场；README 补 Computer Use / 技能市场 / MCP 市场；ENHANCEMENTS 新增 §0.5 核心定位（Computer Use 一等目标 + 验收标准）                     | 人工核对一致                                     |
| **E6** | 插件 / 技能从 zip / git 外部分发安装      | 新增 `modules/package_installer/service.py`（`materialize_zip` / `materialize_git` + 防 zip slip + 包裹目录自动下沉）；`api/rest/plugins.py` 与 `skills.py` 各新增 `POST /install-external`，外部安装标记 `source=external` 且可卸载 | 12 passed（安装器核心 6 + REST 集成 6）；ruff / mypy 未新增 |
| **E5** | 回归评测 Eval Harness         | 新增 `harness/eval/`（cases / scoring / runner / report + `__main__` CLI，失败退出 1 可做 CI 门槛）；4 个种子任务 + README | 20 passed；ruff / mypy 未新增 |
| **E8** | 前端自动化测试（vitest）          | 引入 vitest 5 + @vue/test-utils + jsdom；新增 4 个测试文件（format / datetime / todos / MarkdownRenderer） | `pnpm test` 24 全绿；`pnpm build` 通过 |
| **E7** | 向量记忆检索                    | 新增 `memory_manager/embedding.py`（`HashingEmbedder` 默认 + `OpenAICompatibleEmbedder` + `build_embedder_from_env`）；`search` 向量余弦为主 + LIKE 回退，旧数据懒回填，`update` 重嵌 | 新增 17、记忆相关共 37 passed；ruff / mypy 未新增 |
| **E3** | 结构化 compaction（compaction v2） | 新增 `StructuredCompactionStrategy`（`context_manager/service.py`）：旧消息折叠为「请求 / 调用 / 结果 / 结论」结构化事件日志、保留最近 N 条原文，确定性不依赖 LLM；裁剪边界 `_safe_boundary` 不切断 tool 组；插件 `config.strategy=structured` 启用 | 新增 8、连同 `test_context_manager.py` 共 19 passed；ruff / mypy 未新增 |
| **E2** | Docker 沙箱后端 | 新增 `DockerBackend`（`sandbox_manager/service.py`）：一次性容器（`docker run --rm`）+ bind mount 工作区到 `/workspace`，默认 `--network none` 禁网、`--memory`/`--cpus` 限制、镜像白名单、超时 `docker rm -f` 兜底防泄漏；插件 `config.backend=docker` 或 `HARNESS_SANDBOX_BACKEND=docker` 启用 | 新增 18、连同 `test_sandbox.py` 共 30 passed、1 skipped（真实 docker 端到端跳过）；ruff / mypy 未新增 |
| **E9** | 会话 / 消息搜索 | 新增 `SessionService.search_sessions`（标题 + 消息内容 LIKE、每会话最多 3 条片段、通配符转义）；新增 `GET /api/sessions/search?q=`；前端侧边栏搜索框（防抖 300ms、点击直达、Esc / × 退出） | 新增 12 passed；`SessionSidebar.test.ts` 4 测试、前端共 28 passed；`pnpm build` 通过；ruff 干净、mypy 未新增 |
| **E1** | 多渠道 Channel（Webhook + Telegram） | 新增 `channel_manager`（`Channel` / `ChannelManager` / `WebhookChannel` / `TelegramChannel`，`channel_links` 表按 `(channel,user)` 映射会话）；新增 `GET /api/channels`、`POST /api/channels/inbound/{name}` 等；`plugins/channel_manager` 与 `plugins/channel_telegram` 两插件 | 新增 23 passed（含端到端回发与 REST）；全应用冒烟返回 `webhook`/`telegram`；ruff / mypy 零问题 |
| **E10** | 多 Agent 编排（并行 + 流水线） | 新增 `modules/orchestrator/service.py`（`run_parallel`：Semaphore 并发上限、按序返回、异常隔离；`run_pipeline`：角色阶段串行、`{input}` 传递、失败终止）；`plugins/tool_orchestrator` 注册 `parallel` / `pipeline` 两工具；护栏：并行 ≤10 / 流水线 ≤8 | 新增 18 passed（含真实 AgentLoop 端到端并行 + 子代理会话清理）；全应用冒烟工具注册成功；ruff / mypy 零问题 |
| **E11** | 桌面壳 Tauri | 新增 `desktop/` 工程（`src-tauri` Tauri 2：Rust 壳选端口 / 经启动器拉起后端 / 退出时 `taskkill /F /T` 回收；`launcher/launch-backend.py` 启动器，默认 127.0.0.1 + 每用户数据目录，支持 `--check`；全套图标）；后端加 CORS 中间件（放行 Tauri 协议源 + 本地端口正则）；前端 `api/runtime.ts` 识别 Tauri 并经 `get_backend_url` 解析后端地址（HTTP / WS 均接入，失败回退默认端口） | 新增 8 passed（启动器 / 配置 / 一致性 + 真实拉起后端健康检查）；前端新增 4、共 32 passed，`pnpm build` 通过；ruff / mypy 未新增。**本机缺 Rust，编译 / 打包待工具链** |
| **E12** | 认证与多用户 | 新增 `infra/security.py`（PBKDF2 口令哈希 + HMAC 签名 token）、`modules/auth_manager/`（AuthService、管理员引导）、`api/middleware/auth.py`（Bearer 中间件）、`api/rest/auth.py`（status / login / logout / me / 用户 CRUD）；users 表与 sessions/memories user_id（旧库迁移）；会话 / 记忆 / AgentLoop / 子代理 / 渠道 / WS 全链路按 user_id 归属；前端新增 token 存储、auth store、LoginView、App 登录门与退出按钮 | 新增 23 passed（鉴权、用户 CRUD、隔离、WS 1008）；前端新增 10、共 42 passed，`pnpm build` 通过；ruff / mypy 干净 |

> 注：G1–G5、E6 落地未引入新的 mypy 错误（基线 12 个历史错误均为其他文件既有问题）；各能力均带针对性测试，回归套件全绿。**方案 A 已全部落地（E12 认证与多用户完成，E4 因本机 2GB 显存 + CUDA 9.2 现实不可行而暂缓）**。
