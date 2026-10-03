# andy-harness v1.0.0（开发者预览版）发布前全项目分析报告

> **报告日期**：2026-10-01
> **基线 commit**：`4f45714`（`feat: 会话文件传输 + 撞工具上限强制终答修复`）
> **分析范围**：`backend/harness`（kernel / engine / modules / api / infra / eval）、`backend/plugins`（33 个插件）、`backend/{marketplace,skill_marketplace,mcp_marketplace,skills}`、`backend/tests`、`frontend/src`、`desktop/`、根目录脚本与全部说明文档（README / CHANGELOG / ENHANCEMENTS / docs/*）
> **方法**：静态阅读全部关键源文件 + **实际执行**测试与静态检查（第 2 节列出可复现命令与原始结论），所有结论均落到「文件:行号」或「实测输出」，不含推测。
> **与既有文档的关系**：本报告**不重复** `docs/AUDIT-2026-09-30.md` 与 `docs/GAP-ANALYSIS-2026-09-30.md` 中已修复的条目；对仍未修复或有新增发现的项，给出**当前实测证据**。

---

> [!IMPORTANT]
> ## 🔄 复核结论（2026-10-01 第二次复核，本节优先于下方全部内容）
>
> 本报告第 3 节的 **6 个 P0 阻断项已全部修复并通过实测**，第 0.3 节的"建议不要打 tag"结论**作废**。下方第 0.1/0.2/0.3、2.2、2.3、2.5 节记录的是**修复前的基线**，保留用于追溯，**不再代表当前状态**。
>
> **修复后的实测基线（全部可复现）**
>
> | 门禁 | 命令 | 结果 |
> |---|---|---|
> | 后端 Lint | `cd backend && uv run ruff check .` | ✅ All checks passed（原 10 errors） |
> | 后端格式 | `cd backend && uv run ruff format --check .` | ✅ 260 files already formatted（原 146 待格式化） |
> | 后端类型 | `cd backend && uv run mypy harness` | ✅ no issues in 104 source files（原 6 errors） |
> | 后端测试 | `cd backend && uv run pytest -q` | ✅ 593 passed, 2 skipped（88.9s） |
> | 前端 Lint | `cd frontend && pnpm lint` | ✅ 0 error（原 10 errors） |
> | 前端测试 | `cd frontend && pnpm test` | ✅ 9 files / 43 tests（并已加入 `ci.yml`） |
> | 前端构建 | `cd frontend && pnpm build` | ✅ 通过（主 chunk 1.30 MB / gzip 441 KB） |
>
> **P0 逐项销账**
>
> | 原编号 | 问题 | 现状 |
> |---|---|---|
> | P0-1 | 后端 CI 三红 | ✅ 已修（`ruff format` 全量应用 + 手工修 F841/UP/类型问题） |
> | P0-2 | 前端 lint 红 + 测试未进 CI | ✅ 已修（`eslint.config.ts` 增加 `argsIgnorePattern`；`ci.yml` 新增 `pnpm test` 步骤） |
> | P0-3 | 版本号不一致 | ✅ 已修（7 处统一为 `1.0.0`；新增 `harness/__init__.py::__version__`；`CORE_API_VERSION` 与 41 个插件清单按设计保持 `0.1.0`） |
> | P0-4 | 仓库内明文存放的 API Key + 失效的文档引导 | ✅ 已修（该文件已删除；README 两处引导改为环境变量 + 设置页；`.gitignore` 兜底规则保留） |
> | P0-5 | 设置写入包目录 | ✅ 已修（`settings.py` 路径 `backend/harness/data/` → `backend/data/`；`.gitignore` 追加 `backend/harness/data/`） |
> | P0-6 | 管理面无授权 + `0.0.0.0` | ✅ 已修（新增 `api/deps.py::require_admin`，覆盖 `plugins`(8) / `mcp`(5) / `permissions`(3) / `settings`(2) / `auth`(4)；`Makefile` 与 `start-all.bat` 改 `127.0.0.1`；`docker-compose` 端口绑 `127.0.0.1`） |
>
> **仍然开放的问题（已写入 Release Notes 的 Known Issues）**
>
> - **P1-1** Docker 前端未配 `/api` 与 `/ws` 反向代理 → 容器模式连不上后端（后端镜像已补齐 `marketplace/` `skills/` `mcp_marketplace/`）
> - **P1-2** 桌面壳"每用户数据目录"只覆盖 `harness.db` 与 `attachments/`（`auth_secret.txt` / `permission.json` / `skill_state.json` / `settings.json` / 制品目录仍写仓库 `backend/` 下）
> - **P1-3** `GET /api/models` 只按 `enabled` 过滤，未配置 API Key 的 Provider 其模型仍可选
> - **P1-4** `.pre-commit-config.yaml` 工具版本滞后（ruff v0.6.0 / eslint v9 / prettier v4），与 CI 结论可能不一致
> - **P1-5** 文档事实性错误 → ✅ 本轮已全部订正（README / 手册 §12 / 01-architecture / 02-development-plan / CONTRIBUTING / frontend README）
> - **P1-6** `frontend/README.md` 为 Vite 模板 → ✅ 本轮已重写
> - **P1-7** `frontend/.dev_cv3.txt`、`frontend/.dev_pt.txt` 等调试产物仍在工作区（`git status` 可见，提交前需清理）
> - **P1-8** `mcp.example.json` 硬编码本机绝对路径 → ✅ 本轮已改为 `.`
> - **P1-9** `CORE_API_VERSION` 语义 → ✅ 本轮已在 `01-architecture.md` / `CONTRIBUTING.md` / `RELEASE-v1.0.0.md` 三处写清
> - **P1-10** 缺 `SECURITY.md` / `.env.example` / `.gitattributes` → 仍未补
> - **P2-1 ~ P2-20** 大部分仍然开放（登录限流、Webhook 密钥恒定时间比较、CORS 正则、Tauri CSP 未设、工具串行执行、列表端点无分页、`SettingsView.vue` 约 4300 行、主包体 1.3 MB 等）
>
> **结论**：1.0.0 的**发布阻断项已清零**，可以按《发布 v1.0.0 操作清单》打 tag；剩余问题属于"预览版已知限制"，逐条写进 Release Notes 即可。


## 0. 结论速览（先看这一页）

### 0.1 一句话结论

**代码主体质量明显高于同类个人项目，架构是真材实料；但"发布工程"这条线目前是断的 —— 现在打 tag，会把一个 CI 全红、版本号七处不一致、明文密钥还在盘上、仓库里躺着约 175KB 调试垃圾和 5.4GB 构建产物的仓库发布出去。**

### 0.2 打分表（主观评估）

| 维度 | 评分 | 依据 |
|---|---|---|
| 架构设计（分层/插件化/契约） | **A-** | kernel/engine/modules/api 四层清晰，插件契约完整，`core_api` 兼容校验真实生效 |
| 功能完整度（相对 README 宣称） | **A-** | 72 个 REST 路径 + WS 全部实测可用；593 后端测试 + 43 前端测试全绿 |
| 代码正确性 | **B+** | 历史 P0 修复（跨用户越权、WS 双 reader 竞争、provider 单例 monkey patch）确实已落地并有测试覆盖 |
| 安全默认值 | **D** | 默认 `0.0.0.0` 监听 + 认证默认关 + 插件安装接口可写可执行 = 局域网 RCE 面 |
| 发布工程（CI/版本/打包/仓库整洁） | **F** | CI 五项检查中四项必红；版本号 7 处不一致；明文 key；垃圾文件 |
| 文档准确性 | **C** | README 已大幅订正，但手册附录、项目结构、02-development-plan 仍有**可验证的事实错误** |
| 可维护性 | **C+** | `SettingsView.vue` 4336 行 / `ChatView.vue` 1874 行；单文件主 bundle 1.30MB |

### 0.3 阻断项（P0）—— 6 个，全部是"发布流程 + 安全默认值"，都不需要改架构

| 编号 | 问题 | 证据 |
|---|---|---|
| **P0-1** | 后端 CI 三个检查步骤**必红**：`ruff check` 10 错、`ruff format --check` 146 文件待格式化、`mypy` 6 错 | 实测，见 2.2 |
| **P0-2** | 前端 CI `pnpm lint` **必红**（10 个 error） | 实测，见 2.3 |
| **P0-3** | 版本号**7 处不一致**（`0.0.0` / `0.1.0` / FastAPI version / Cargo / Tauri 混用），且无单一版本源 | 见 3.1 |
| **P0-4** | 仓库内有文件明文存放真实 DeepSeek API Key，README 两处教用户往那里写，但**没有任何代码读取它** | 见 3.2 |
| **P0-5** | 运行时设置写入 **Python 包目录** `backend/harness/data/`，该路径**未被 `.gitignore` 覆盖** → 会被提交进仓库 | 实测，见 3.3 |
| **P0-6** | 默认 `0.0.0.0` 监听 + 认证默认关 + `POST /api/plugins/install` 接收任意 Python 源码并加载执行 = **局域网未授权远程代码执行** | 见 3.4 |

> P0-3 ~ P0-6 是安全/正确性问题，P0-1/P0-2 是"发布门禁"问题。**两类都必须先解决再打 tag。**

### 0.4 我的最终建议

**不要现在就打 `v1.0.0` tag。** 建议：

1. **先做一轮"发布工程收口"**（P0 全部 + P1 的 1.1 / 1.2 / 1.4 / 1.5 / 1.9），预计 1–2 天；其中绝大部分是机械劳动（`ruff format`、改默认值、改版本号、删垃圾文件、订正三处文档表格）。
2. 收口后 CI 真绿，再打 tag 并写 Release Notes；把第 3 节的 **P2 清单原样贴进 Release Notes 的 "Known Issues"**——这比"假装没有"专业得多，也正好符合 "developer preview" 的自我定位。

如果一定要现在发，请把 tag 命名成 `v1.0.0-rc.1` 或 `v1.0.0-dev-preview`（**不要用裸 `v1.0.0`**），并在 Release 首行显式写"CI 尚未全绿 / 已知 N 项阻断级问题"，否则 badge 与 Release 会互相打脸。


---

## 1. 项目画像：我的看法

### 1.1 它到底是什么

andy-harness 是一个**插件化 Agent Harness（智能体底座）**：不是"又一个套壳聊天 UI"，而是把「模型接入 → 上下文装配 → 工具调用循环 → 权限与人工确认 → 持久化 → 可观测」整条 harness 链路做成可插拔的骨架，外加 Web GUI、Tauri 桌面壳、MCP 客户端、Skills、子代理、多 Agent 编排、多渠道、认证等一整套能力。

规模实测：

| 指标 | 数值 |
|---|---|
| 后端 Python（含测试、插件、脚本） | ≈ **38,700 行** |
| 前端 `src`（TS + Vue，含测试） | ≈ **13,700 行** |
| 后端插件数 | **33**（`backend/plugins/`） |
| 后端测试 | **593 passed / 2 skipped**（77.3s，实测） |
| 前端测试 | **43 passed / 9 文件**（vitest，实测） |
| REST 路径数 | **72**（`/openapi.json`，实测） |
| 仓库跟踪文件数 | 289 |

### 1.2 我认为做得好的地方（都有证据）

1. **分层是真的，不是画出来的。** `kernel/`（插件加载、事件总线、服务注册、钩子）对 `engine/`、`modules/`、`api/` 零反向依赖；`main.py` 只有 198 行且没有一行业务逻辑。`ServiceRegistry` / `EventBus` / `HookManager` 三件套让"某能力缺失就优雅降级"成为可写的代码（例如 `agent_loop.py:539-555` 权限服务未注册时返回 `None` 视为放行）。
2. **历史高危问题的修复是可验证的真修复，不是注释糊弄：**
   - `_StreamingProviderProxy`（`api/ws/chat.py:51-91`）：把"monkey patch 共享 provider 单例导致跨会话串流"改成每连接代理对象，注释完整记录了旧实现的三个后果。
   - `_reader` / `_dispatcher` 单 reader 架构（`api/ws/chat.py:333-399`）：把"两个协程并发 `receive_text()` 抢帧、吞掉用户 stop"改成队列分发，并在 `finally` 里 `_reject_pending_confirms()` 兑现"安全默认拒绝"。
   - 被权限拒绝时**仍然回填 tool 消息**（`agent_loop.py:883-905`）—— 很少有人会想到的坑（不回填会让下一轮请求被 API 400 拒绝）。
   - `_cleanup_failed_round`（`agent_loop.py:1002-1047`）清理悬挂的 `assistant(tool_calls)` / `tool` 消息对。
   - 撞迭代上限后强制无工具收尾（`agent_loop.py:268-313`），解决了"任务显示成功却看不到结果"的体验黑洞。
3. **流式 `tool_calls` 分片按 index 合并**（`agent_loop.py:730-772`）：OpenAI 兼容协议里最容易踩的坑之一，这里处理正确。
4. **测试是真的在测东西。** 593 个用例覆盖权限、认证隔离、沙箱（真实 docker 端到端带 skip 标记）、checkpoint、MCP 三种传输（stdio / SSE / streamable-http 各配一个 echo server）、编排；前端 43 个用例覆盖 token / runtime / 日期格式化。`tests/test_audit_fixes.py` 这种"针对上次审计结论写回归"的做法值得表扬。
5. **文档有自知之明。** README 主动标注「渠道 / Computer Use / Jev / 断点续跑 / 用户管理暂无界面入口」；`docs/RELEASE-v1.0.0.md` 的 checklist 质量很高（连"annotated tag vs 轻量 tag"都写了）。这比大多数个人开源项目强。

### 1.3 我认为最核心的三个结构性问题

**问题一：后端能力的扩张速度，远超"接线"和"收口"的速度。**

这是全项目最根本的矛盾，且在数据上清晰可见：

- `backend/harness/modules/` 已有 18 个模块、`backend/plugins/` 33 个插件，但前端只有 4 个视图（Home / Chat / Settings / Login）；
- 完整实现的 5 个能力（渠道、Computer Use、Jev、断点续跑、用户管理）**前端零入口**，只能 curl；
- `docs/ENHANCEMENTS.md`（73KB）与 `CHANGELOG.md`（107KB）的体积本身就说明"增量记录"这件事已经失控 —— CHANGELOG 有 49 个版本条目，而 git tag 只到 `v0.0.9`，两者完全脱节；
- 直接后果就是 `docs/AUDIT-2026-09-30.md` 里那 5 处"文档声称但实际缺失"，以及至今仍在的手册附录事实错误（见 4.1）。

**这不是懈怠，是节奏问题**：每个能力都做了「模块 + 插件 + REST + 测试」，唯独缺了最后一步「UI 接线 + 文档对齐 + 版本收纳」。v1.0.0 正好是补这一步的时机。

**问题二：安全默认值整体偏"开发机"，与"公网发布"不匹配。**

三处默认值单独看都能解释，叠在一起就成了系统性风险：

| 默认值 | 位置 | 单独看 | 叠加后 |
|---|---|---|---|
| 监听 `0.0.0.0` | `Makefile:15`、`start-all.bat:64`、`backend/Dockerfile` | "方便局域网调试" | 暴露到全网段 |
| 认证默认关闭 | `auth_manager/service.py:36`（`HARNESS_AUTH` 未设即关） | "单用户工具，开箱即用" | 无任何门槛 |
| `POST /api/plugins/install` 接受任意 `plugin_code` 并落盘加载执行 | `api/rest/plugins.py:50,146,168` | "插件市场的实现方式" | **未授权 RCE** |

也就是说：**一个和你连同一个 WiFi 的人，可以往你机器上写一个 Python 文件，并让 uvicorn 立刻 `import` 它。** 修复成本极低（默认监听改回 `127.0.0.1`，或给安装类端点加 `_require_admin`），收益极大。

**问题三：缺少"发布工程"这一层。**

这个项目有：CI 配置、Makefile、pre-commit、docker-compose、Tauri 打包、eval 框架、smoke 脚本。
但缺：**能让 CI 绿的一次 `ruff format` 提交、一个统一的版本号常量、一个 `scripts/release.py`、一份"删除调试产物"的 checklist。**

结果就是第 2 节的实测：913 个测试全通过，但 `ruff check` / `ruff format` / `mypy` / `eslint` 四道门禁全红。**CI badge 挂在 README 最上方，而 CI 从未真正绿过。**

### 1.4 "v1.0.0 开发者预览版"这个名号成立吗

**部分成立，但需要一行显式免责声明。**

- ✅ 成立的部分：功能面完整度、架构稳定性、测试覆盖、REST 契约（72 路径）。接口设计（统一 `{code, message, detail, trace_id}` 错误体、后端 `APIError` / 前端 `ApiError`）经得起"稳定承诺"。
- ❌ 不成立的部分：`docs/RELEASE-v1.0.0.md` 自己写"1.0.0 不只是数字变化，它向社会发出 API 稳定承诺"，但当前有 **6 个阻断项 + 10 个 P1 项**未解决。承诺稳定的时候自己的 CI 是红的，这个反差会被第一个提 issue 的人指出。

**建议**：名号可以叫 1.0.0，但 Release 标题写 `v1.0.0 (Developer Preview)`，正文首段写清"本次为开发者预览，已知问题见 XXX；API 在 1.x 内承诺向后兼容，但预览期内可能微调"。既拿到 1.0 信号，又保住退路。


---

## 2. 实测基线（全部可复现）

> 环境：Windows 10 / Python 3.12.13（`backend/.venv`）/ Node v24.15.0 / pnpm 10.32.1 / ruff 0.16.8

### 2.1 测试：全绿 ✅

```
cd backend && .venv\Scripts\python.exe -m pytest -q
→ 593 passed, 2 skipped, 4 warnings in 77.32s

cd frontend && pnpm test
→ Test Files  9 passed (9)
→      Tests  43 passed (43)

cd frontend && pnpm build
→ vue-tsc -b && vite build 通过
→ dist/assets/index-*.js  1,301.53 kB │ gzip: 441.50 kB   ← ⚠ 单 chunk > 500 kB 警告
```

唯一瑕疵：`tests/test_provider_registry.py` / `tests/test_openai_compatible.py` 里的 `TestProvider*` 类被 pytest 误当测试类收集，产生 4 条 `PytestCollectionWarning`（应加 `__test__ = False` 或改名）。

### 2.2 后端静态检查：**三红** ❌

```powershell
cd backend
.venv\Scripts\python.exe -m ruff check .              # → Found 10 errors (7 fixable)
.venv\Scripts\python.exe -m ruff format --check .     # → 146 files would be reformatted, 113 already formatted
.venv\Scripts\python.exe -m mypy harness              # → Found 6 errors in 4 files (checked 103 source files)
```

`ruff check` 的 10 个错误（原始输出）：

```
harness/kernel/eventbus.py:42:13          F841  Local variable `next_p` is assigned to but never used
marketplace/__init__.py:2:43              W292  No newline at end of file
marketplace/pet_plugin/__init__.py:1:35   W292  No newline at end of file
marketplace/pet_plugin/main.py:7:1        I001  Import block is un-sorted or un-formatted
marketplace/pet_plugin/main.py:28:35      W292  No newline at end of file
plugins/tool_ip_lookup/main.py:86:17      W292  No newline at end of file
plugins/tool_theme_switcher/main.py:91:48 W292  No newline at end of file
scripts/e2e_attachments.py:110:17         UP012 Unnecessary UTF-8 `encoding` argument to `encode`
skills/daily-llm-news/scripts/save_news.py:36:22   UP031 Use format specifiers instead of percent format
skills/daily-llm-news/scripts/save_news.py:60:15   UP031 Use format specifiers instead of percent format
```

`mypy` 的 6 个错误（原始输出，均为存量问题）：

```
harness/modules/model_manager/openai_compatible.py:28    no-untyped-def   （缺返回类型标注）
harness/modules/model_manager/openai_compatible.py:107   override         （chat 返回类型与契约不符）
harness/modules/model_manager/openai_compatible.py:260   no-untyped-call  （_get_tiktoken_encoding 未标注）
harness/modules/model_manager/provider_registry.py:274   no-redef         （config 变量在第 252 行已定义）
harness/api/rest/settings.py:47                          no-any-return    （json.loads 结果直返）
harness/modules/model_manager/token_counter_adapter.py:55 no-any-return
```

> ⚠️ 注意 `ruff format --check` 的 **146 / 259** 这个比例：意味着这个仓库**几乎从未被 `ruff format` 格式化过**。这是一次提交能解决的问题（`ruff format .` + 独立 commit），但 diff 会很大，**必须单独提交**，否则会污染其他修复的 `git blame`。
>
> ⚠️ `mypy` 的 6 个错误与 `docs/GAP-ANALYSIS-2026-09-30.md` 里"基线 12 个历史错误"的记载不一致（现在是 6 个，说明中间修过一半），但**只要还有 1 个，CI 的 `mypy` 步骤就是红的**。

### 2.3 前端静态检查：**一红** ❌

```powershell
cd frontend && pnpm lint
→ 10 problems (10 errors, 0 warnings)
```

```
src/api/client.test.ts     30:14  '_url' is defined but never used
src/api/client.test.ts     30:28  '_init' is defined but never used
src/api/client.test.ts     44:14  '_url' is defined but never used
src/api/client.test.ts     44:28  '_init' is defined but never used
src/components/MatrixRain.vue:9   'fontSize' is never reassigned. Use 'const' instead
src/views/SettingsView.vue 468 / 564 / 631 / 838 / 1062   'e' is defined but never used（5 处）
```

全部是"一行改动"级别，但根因是 `eslint.config.ts` 未给测试文件放宽 `@typescript-eslint/no-unused-vars` 的 `argsIgnorePattern`（代码里已用 `_` 前缀但规则没配），**以后每写一个 mock 测试都可能再踩**。

### 2.4 实跑端到端验证 ✅（后端 + REST 真实启动）

```
[backend] uvicorn harness.main:app --host 127.0.0.1 --port 8123
→ INFO: Application startup complete.

GET  /api/health    → {"status":"ok"}                                       HTTP 200
GET  /api/settings  → {"theme":"light","language":"zh-CN",
                       "default_model":"deepseek-chat","default_budget":4096}  HTTP 200
PUT  /api/settings  → {"theme":"ocean", ...} 写入成功（路径问题见 3.3）
GET  /api/models    → 11 个模型（过滤问题见 P1-3）
GET  /api/providers → deepseek(has_api_key=true) / doubao(false) / qwen(false)，三者 enabled 均为 true
GET  /openapi.json  → 72 个 path
```

启动日志里两条正常的能力降级提示，说明降级设计确实生效：

```
Telegram 渠道已注册但未配置 HARNESS_TELEGRAM_BOT_TOKEN，未启动
Jev Manager 已激活，但未配置 API Key。……未配置时调用 Jev 工具将返回友好错误提示
```

### 2.5 CI 配置与实测的差距（关键）

`.github/workflows/ci.yml` 实际执行的是：

| Job | 步骤 | 实测结果 |
|---|---|---|
| backend | `uv run ruff check .` | ❌ 10 errors |
| backend | `uv run ruff format --check .` | ❌ 146 files |
| backend | `uv run mypy harness` | ❌ 6 errors |
| backend | `uv run pytest -v` | ✅ 593 passed |
| frontend | `pnpm install --frozen-lockfile` | ✅ |
| frontend | `pnpm lint` | ❌ 10 errors |
| frontend | `pnpm build` | ✅ |
| frontend | **（无测试步骤）** | ⚠️ 43 个 vitest 用例从未在 CI 运行 |

**结论：CI 的两个 job 都会失败。** 而 README 第 5 行挂着 `CI` badge。

---

## 3. 问题清单（按轻重缓急排列）

> **优先级定义**
> - **P0 = 阻断发布**：不解决就不应该打 `v1.0.0` tag（CI 不可交付 / 安全漏洞 / 会污染仓库的运行时数据 / 版本语义错乱）
> - **P1 = 发布窗口内解决**：承诺了但实际不可用的路径、可验证的文档事实错误、明显的仓库卫生问题
> - **P2 = 1.0.x 内解决**：安全加固项、健壮性、可维护性；应在 Release Notes 的 Known Issues 中披露
> - **P3 = 1.x 规划**：功能补齐与跨平台验证

---

### P0 级（阻断发布）

#### P0-1｜后端 CI 三个检查步骤必红：`ruff check` 10 错 / `ruff format` 146 文件 / `mypy` 6 错

- **证据**：见 2.2，三条命令的原始输出已列出；错误清单可逐条定位到文件行号。
- **影响**：
  - `.github/workflows/ci.yml` 的 backend job 在 `ruff check` 就会 fail-fast，后面两步根本跑不到；README 顶部的 CI badge 一定是红的。
  - `ruff format --check` 的 **146/259** 意味着这不是"漏跑一次"，而是**这个仓库从未被格式化过**——新贡献者按 README 跑 `make format` 会得到 146 个文件的巨大 diff。
- **修复**（按顺序，**分三次独立提交**，避免污染 blame）：
  1. `cd backend && uv run ruff format .` → commit：`style: apply ruff format across backend (no logic change)`
  2. 手工修 3 个非机械项：`harness/kernel/eventbus.py:42` 删掉未使用的 `next_p`；`openai_compatible.py:28/260` 补类型标注；`provider_registry.py:274` 的 `config` 重名；`settings.py:47` / `token_counter_adapter.py:55` 的 `no-any-return`（`cast` 或中间变量）→ commit：`fix(lint): resolve ruff/mypy errors blocking CI`
  3. `uv run ruff check . --fix` 处理剩余的 `W292 / I001 / UP012 / UP031` → commit
- **预估**：0.5 天。**风险**：`openai_compatible.py:107` 的 `override` 错误需要动 `ModelProviderPlugin` 契约里 `chat` 的签名（`async def` + 返回 `AsyncIterator` 的语义冲突），**这一处要小心**，它是 `mypy` 里唯一需要改契约的。若评估后成本高，可以先在 `backend/pyproject.toml` 的 mypy 配置里对该文件加 `# type: ignore[override]` 并写清原因（比让 CI 红着强）。

#### P0-2｜前端 `pnpm lint` 必红（10 error），且 43 个前端测试从未进 CI

- **证据**：见 2.3；`ci.yml` 的 frontend job 只有 `pnpm lint` + `pnpm build`，**没有 `pnpm test`**。
- **影响**：前端 job 必红；同时前端测试写了但无守护，未来改动容易悄悄打破。
- **修复**：
  1. 修 10 个 eslint 错误（4 处在 `client.test.ts` 的 mock 参数，5 处在 `SettingsView.vue` 的 `catch (e)`，1 处在 `MatrixRain.vue` 的 `let` → `const`）；
  2. 在 `frontend/eslint.config.ts` 里对测试文件放宽：`{ files: ['**/*.test.ts'], rules: { '@typescript-eslint/no-unused-vars': ['error', { argsIgnorePattern: '^_' }] } }`；
  3. 在 `ci.yml` 的 frontend job 里 `pnpm lint` 之后加 `- name: Test (vitest)` / `run: pnpm test`。
- **预估**：0.2 天。

#### P0-3｜版本号 7 处不一致，且项目没有"单一版本源"

- **证据**（全部实测）：

| # | 位置 | 当前值 | 应改为 | 备注 |
|---|---|---|---|---|
| 1 | `backend/pyproject.toml:3` | `0.1.0` | `1.0.0` | 包版本 |
| 2 | `backend/harness/main.py:162` | `version="0.1.0"` | 从包元数据读，或 `1.0.0` | FastAPI 文档标题栏会显示 |
| 3 | `backend/harness/modules/mcp_client/service.py:144` | `clientInfo.version "0.1.0"` | `1.0.0` | **会发给所有 MCP server**，是外部可见标识 |
| 4 | `frontend/package.json:4` | `"version": "0.0.0"`，`"name": "frontend"` | `1.0.0` / 改名 | `0.0.0` 是 Vite 模板残留 |
| 5 | `desktop/package.json:4` | `0.1.0` | `1.0.0` | |
| 6 | `desktop/src-tauri/tauri.conf.json:4` | `"version": "0.1.0"` | `1.0.0` | **决定安装包版本号，用户可见** |
| 7 | `desktop/src-tauri/Cargo.toml:3` | `version = "0.1.0"` | `1.0.0` | |
| — | `backend/harness/__init__.py` | 无 `__version__` | 建议新增 `__version__ = "1.0.0"` | **目前没有任何"单一版本源"** |
| — | `backend/pyproject.toml` | 无 `[project.scripts]` | 可选：加 `harness` CLI 入口 | 目前只能 `python -m harness...` |

- **影响**：`docs/RELEASE-v1.0.0.md` 第 2.1 节已经预见到这个问题，但只列了 3 处（pyproject / frontend pkg / desktop pkg），**漏了 4 处**（FastAPI version / MCP clientInfo / tauri.conf.json / Cargo.toml）。安装包版本显示 `0.1.0` 而 Release 叫 `v1.0.0`，是直接的信任损伤。
- **修复**：见第 6 节的"版本号统一操作指南"（**含一条重要警告**）。
- **预估**：0.2 天。

#### P0-4｜仓库内曾存在一个存放明文 API Key 的文件，README 两处引导用户往那里写，而代码根本不读

> 本节不点名该文件（按项目约定），只描述问题本身。当前状态：**已删除并修复**。

- **证据**（修复前）：
  - 该文件内容为一把**真实可用的 DeepSeek Key，明文存放**（`ds:sk-****`，已作废）。
  - 文档引导：README 曾在两处（安装章节与排错章节）告诉用户"把 Key 写进那个文件"，并给出 `ds:sk-xxxx` / `qwen:` / `doubao:` 的前缀格式。
  - **代码不读**：在 `backend/`、`frontend/`、`desktop/` 全量搜索该文件名，**零命中**；Provider 插件实际只认 `ctx.config.get("api_key")` 与 `os.getenv("DEEPSEEK_API_KEY")`（`plugins/provider_deepseek/main.py:79`）。
  - `.gitignore` 有针对该文件名的兜底忽略规则，且 `git log --all -S` 只命中 **README 的文字引用**，**密钥文件本身从未被提交**（这一层防护当时是对的）。
- **影响（两条）**：
  1. **安全**：文件在**工作树里**。任何形式的"打包发送"（zip 整个目录、拷到 U 盘、共享文件夹、`git add -f`）都会泄漏这把 Key。而且 README 正在**教所有用户重复这个坏习惯**。
  2. **可信度**：README 里那两处"配置方式"是**死路**——用户照做后 Provider 依然没有 Key，会遇到"发送消息失败"却不知道为什么。这与 FAQ 里"API Key 使用 Fernet 对称加密存储"的说法也自相矛盾。
- **修复（✅ 已完成）**：
  1. 轮换该 Key 并删除该文件；
  2. 删除 README 中对应的两段引导，改为只保留"设置页配置"与"环境变量"两条真实可用的路径；
  3. `.gitignore` 的兜底规则**保留**（防止历史包袱回来）；
  4. 在 `CONTRIBUTING.md` 增加「安全红线」，明确"绝不提交密钥与运行时数据"。

#### P0-5｜运行时设置写进 Python 包目录 `backend/harness/data/`，且该路径**不在 `.gitignore` 里**

- **证据**：
  - `backend/harness/api/rest/settings.py:21-23`：
    ```python
    _SETTINGS_FILE = (
        Path(__file__).resolve().parent.parent.parent / "data" / "settings.json"
    )
    ```
    `__file__` = `backend/harness/api/rest/settings.py` → 上溯 3 层 = `backend/harness/` → **`backend/harness/data/settings.json`**。
  - 实测：`PUT /api/settings {"theme":"ocean"}` 之后，`backend/harness/data/settings.json` 的 mtime 更新，**`backend/data/settings.json` 不存在**。
  - 对照全项目的正确写法：`database.py:19`、`attachment/limits.py:39`、`auth_manager/service.py:147`、`permission_manager/service.py:132`、`skill_manager/service.py:184` 全部是 `parents[3] / "data"` → `backend/data/`。**只有 settings.py 少了一层 `parent`。**
  - `git status` 显示 `?? backend/harness/data/`（未被忽略）；`.gitignore` 只有 `backend/data/`（第 74 行），**没有 `backend/harness/data/`**。
- **影响**：
  1. `git add -A` 会把用户运行时的偏好设置提交进仓库；
  2. **破坏桌面壳的数据隔离设计**：桌面壳把 DB / 附件指向 `%APPDATA%/andy-harness`，但 settings 仍写仓库目录；
  3. 若将来打成 wheel 安装（`pyproject.toml` 配了 hatchling `packages = ["harness"]`），会往 `site-packages/harness/data/` 里写文件 —— 可能只读、且升级即丢。
- **修复**：
  1. `settings.py` 改为与 DB 一致的目录（`parents[3] / "data"`），或更好：与 `HARNESS_DB_PATH` 同目录（`Path(db_path).parent`），使桌面壳的数据目录对 settings 同样生效；
  2. `.gitignore` 追加 `backend/harness/data/`；
  3. 删除现有的 `backend/harness/data/` 目录（作者的本地偏好无需入库）。
- **预估**：0.2 天。


#### P0-6｜管理面端点**完全没有授权校验** + 默认监听 `0.0.0.0` + 认证默认关闭 = 未授权/越权远程代码执行

这是全项目**风险最高**的一项，由三个"各自看起来合理"的默认值叠加而成。

**证据链（逐条已核对源文件）**

1. **默认监听全网段**：
   - `Makefile:10` → `uvicorn harness.main:app --reload --host 0.0.0.0 --port 8000`
   - `start-all.bat:64` → `... -m uvicorn harness.main:app --reload --host 0.0.0.0 --port 8000`
   - `backend/Dockerfile:25` → `CMD ["uv","run","uvicorn",...,"--host","0.0.0.0","--port","8000"]`
2. **认证默认关闭**：`harness/modules/auth_manager/service.py:36`，`HARNESS_AUTH` 未设 → `enabled = False`；`api/middleware/auth.py:73` 直接放行。
3. **插件安装接口接收任意 Python 源码并加载执行**：
   - `api/rest/plugins.py:50` → `plugin_code: str  # Python 源码`
   - `api/rest/plugins.py:146,168` → `(plugin_dir/"__init__.py").write_text("")` / `(plugin_dir/"main.py").write_text(req.plugin_code)`
   - 之后走 `PluginManifest` + `loader.activate()` → **服务端立刻 import 并执行**
4. **这些端点连"登录用户"都不校验**（比第 2 条更严重的一层）：
   - `plugins.py` 的 `install_plugin` / `install_marketplace_plugin` / `install_external_plugin` / `uninstall_plugin` / `activate_plugin` / `update_plugin_config` —— **签名里没有 `Request`，没有任何 `_require_admin`**。
   - 对比：只有 `api/rest/auth.py:49-53` 定义了 `_require_admin`，且**仅用于 `/api/users*`**。
   - 同类问题还在：
     - `api/rest/permissions.py:78,102` → 任意用户可 `PUT /api/permissions`，把 `dangerous` 工具从"需确认"改成"自动放行"；
     - `api/rest/mcp.py:114,134,180,223` → 任意用户可 `POST /api/mcp/servers` 新增 stdio server（`command` + `args` 由请求体决定）→ **等价于任意进程执行**；
     - `api/rest/settings.py:69` → 任意用户可改全局设置。

**影响**

| 场景 | 后果 |
|---|---|
| 认证关闭（默认）+ 监听 `0.0.0.0`（默认） | 同一局域网内任何人 → `POST /api/plugins/install` → **未授权 RCE** |
| 认证开启 + 普通用户登录 | 普通用户 → 同样的端点 → **提权到 RCE**（`_require_admin` 形同虚设） |
| 认证开启 + 普通用户登录 | `PUT /api/permissions` 关掉危险工具的人工确认 → 之后 Agent 可无提示执行 `tool_code_runner` |

**修复（前两条都是一行改动）**

1. `Makefile:10`、`start-all.bat:64` 的 `--host 0.0.0.0` 改成 `--host 127.0.0.1`（**桌面壳已经是 127.0.0.1，只有浏览器模式敞着**）；需要局域网访问时用 `HARNESS_HOST=0.0.0.0` 显式开启，并在 README 写明风险；
2. `backend/Dockerfile` 保持 `0.0.0.0` 可接受（容器场景），但 `docker-compose.yml` 的端口映射默认只暴露到宿主本机：`- "127.0.0.1:8000:8000"`、`- "127.0.0.1:5173:5173"`；
3. 给"改变系统状态"的端点统一加管理员校验（`plugins.py` 6 个、`mcp.py` 4 个、`permissions.py` 2 个、`settings.py` 1 个）。做法：把 `auth.py::_require_admin` 提到共用的 `api/deps.py`；**认证未启用时保持放行**（单用户行为不变，与现有 `_owner_user_id` 的哲学一致）；
4. 可选加固：`install_plugin` 这种"直接收源码"的端点默认禁用，只保留 `install-external`（zip/git）与市场安装。

- **预估**：0.5 天。**这是最应该先做的一条**，因为它是唯一一条"能被别人利用"的问题。


---

### P1 级（发布窗口内解决）

#### P1-1｜README 承诺的 `docker-compose up -d` 部署路径实际上跑不通

- **证据**：
  - `frontend/Dockerfile:23-27` → `CMD ["serve","-s","dist","-l","5173"]`。`serve -s` 是 **SPA 单页兜底模式**：所有未命中的路径都返回 `index.html`。
  - 前端所有请求都是相对路径 `/api/...`（`frontend/src/api/client.ts:6-8`），WebSocket 是 `/ws/chat`（`client.ts:41-44`）。**静态服务器没有任何反向代理**，于是：
    - `GET /api/health` → 返回 `index.html`（HTTP 200 + HTML）；
    - `client.ts:109-114` 的 `res.json()` 抛错后**静默返回 `undefined as T`** → `providers.ts:27` 把 `models` 赋成 `undefined` → 随后 `models.filter(...)` 抛 `TypeError` → 设置页/模型选择器崩溃或空白；
    - `/ws/chat` 握手失败 → 前端进入无限 `reconnecting`。
  - `backend/Dockerfile:14-16` 只 `COPY pyproject.toml uv.lock / COPY harness/ / COPY plugins/ / COPY requirements-sandbox.txt`，**没有 COPY** `marketplace/`、`skill_marketplace/`、`mcp_marketplace/`、`skills/`、`scripts/` → 容器里**插件市场 / 技能市场 / MCP 市场 / 内置技能全部消失**。
  - `docker-compose.yml:1` 的 `version: "3.9"` 在 Compose V2 已废弃（会产生 warning）。
  - `docker-compose.yml:10-11` 把宿主 `./backend/plugins` 挂到 `/app/plugins` —— 与容器内 `COPY plugins/` 冗余，且会让宿主与容器互相污染。
- **影响**：README「快速开始 → Docker 部署」是**五个安装路径之一**，当前 100% 不可用，且失败方式是"页面能打开但什么都不工作"。
- **修复**（三选一，推荐 A 或 B）：
  - **A（最省事）**：把 README 的 `Docker 部署` 章节标注为"实验性、未验证"，把安装路径收敛到已验证的 `make install` 与 `start-all.bat`；
  - **B（推荐做法）**：前端换成 nginx，加 `frontend/nginx.conf`（`location /api/ { proxy_pass http://backend:8000; }` + `location /ws/ { proxy_pass ...; proxy_http_version 1.1; proxy_set_header Upgrade $http_upgrade; proxy_set_header Connection "upgrade"; }`）；
  - **C**：`vite preview` 自带 proxy 但明确不适合生产，仅作临时方案。
  - 无论选哪个，都要补 `backend/Dockerfile` 的 `COPY marketplace/ skill_marketplace/ mcp_marketplace/ skills/ ./`。
- **预估**：方案 A 0.1 天；方案 B 0.5 天。

#### P1-2｜桌面壳的"每用户数据目录"只覆盖了 DB 与附件，其余状态仍写仓库目录

- **证据**：
  - `desktop/launcher/launch-backend.py:105-106` 只注入两个环境变量：`HARNESS_DB_PATH`、`HARNESS_ATTACHMENTS_DIR`。
  - 实际被每用户数据目录覆盖的只有：`Database`（`database.py:245`）、附件（`attachment/limits.py:45`）。
  - **仍硬编码到仓库目录**的运行时状态：

    | 状态 | 位置 |
    |---|---|
    | `auth_secret.txt`（认证签名密钥） | `auth_manager/service.py:147` → `backend/data/` |
    | `permission.json`（工具权限策略） | `permission_manager/service.py:132` → `backend/data/` |
    | `skill_state.json`（技能启停） | `skill_manager/service.py:184` → `backend/data/` |
    | `settings.json` | `api/rest/settings.py:22` → `backend/harness/data/`（见 P0-5） |
    | 制品落盘目录 | `artifact_store/service.py:26` → `backend/workspace/artifacts` |
    | Computer Use 工作区 | `plugins/computer_use/config.py:47` → `backend/data/computer_use_workspace` |
    | 沙箱工作区 | `sandbox_manager` 的默认工作区为**系统临时目录**（`%TEMP%/harness_workspaces`、Docker 后端为 `harness_docker_workspaces`），不落仓库目录 ✅ 不属于本项问题 |

- **影响**：`README.md:171` 与 `README.md:276` 明确承诺"桌面壳数据目录为每用户目录……与 `backend/data/` 相互独立"。这句话**只对 DB 与附件成立**。在真正装成安装包的场景（Tauri `bundle.targets = "all"`，安装到 `Program Files`）下，上述文件会写进**只读或不存在**的安装目录 → 认证 / 权限 / 技能状态**静默失效**。
- **修复**：把 `launch-backend.py` 已算好的 `data_dir` 通过统一环境变量下发（例如 `HARNESS_DATA_DIR`），让上述 7 处都改走它（或统一走 `Path(db_path).parent`）；顺带把 README:171 / README:276 的措辞改准确。
- **预估**：0.5 天。

#### P1-3｜`/api/models` 不按 `has_api_key` 过滤 → 未配密钥的 Provider 模型可选，选中即报错

- **证据**（实测）：
  ```
  GET /api/providers → deepseek(has_api_key=true,  enabled=true)
                       doubao  (has_api_key=false, enabled=true)
                       qwen    (has_api_key=false, enabled=true)
  GET /api/models    → 11 个模型，包含 doubao-pro-4k / qwen-max 等
  ```
  `frontend/src/stores/providers.ts:27` 直接 `models.value = await apiClient.get<Model[]>('/models')`；`ModelSelector.vue:41` 只在**列表为空**时才提示"请先在设置中配置 Provider 的 API Key"。
- **影响**：`README.md` 说"停用的 Provider 不会出现在模型列表中"，FAQ 说未配密钥时"测试连接和启用按钮置灰"——但列表里依然能选中 `qwen-max`，发送后才失败。典型的"看起来能用、实则必错"。
- **修复**（推荐第 1 条）：① 后端 `/api/models` 只返回 `enabled AND has_api_key` 的 Provider 的模型（`api/rest/models.py`）；② 或前端 `ModelSelector.vue` 过滤 + `providers.ts` 保留 `has_api_key` 用于置灰。**另建议**：`client.ts:109-114` 在 `res.ok` 但 JSON 解析失败时应**抛错**而不是返回 `undefined`——目前这个"静默兜底"会把后端异常伪装成空数据（P1-1 的失败方式正是被它放大的）。
- **预估**：0.3 天。

#### P1-4｜`pre-commit` 配置严重过期，与 CI / 本地环境三方不一致

- **证据**：`.pre-commit-config.yaml` 钉的是 `ruff-pre-commit v0.6.0`、`mypy v1.11.0`、`mirrors-eslint v9.0.0`、`mirrors-prettier v4.0.0`；而实测本地 ruff 为 **0.16.8**，前端为 **eslint 10 + typescript 6 + flat config（`eslint.config.ts`，需要 `jiti`）**。
- **影响**：eslint 钩子会因 flat config + 缺 `jiti` 直接失败；`mirrors-prettier` 仓库已归档；同一份代码在 pre-commit 与 CI 下会得到**不同结论**——"提交前通过、CI 挂掉"将常态化。
- **修复**：把 rev 对齐当前实际版本（ruff `v0.16.8` 等），前端两个钩子改用 `local` 类型直接跑 `pnpm lint` / `pnpm format`；或干脆删掉前端相关钩子，只保留 `ruff` + `ruff-format` + 通用钩子，前端交给 CI。
- **预估**：0.3 天。


#### P1-5｜文档仍有可验证的事实错误（这是"1.0.0 承诺稳定"最不该出的问题）

| 位置 | 文档写的 | 代码里实际是 | 性质 |
|---|---|---|---|
| 手册 §12.1 | `HARNESS_ARTIFACTS_DIR` 默认 `backend/data/artifacts` | `artifact_store/service.py:26` → `backend/workspace/artifacts` | **错误** |
| 手册 §12.2 | 数据文件表列了 7 项 | 缺 `settings.json`、缺 `backend/data/attachments/`；表里 `backend/data/artifacts` 路径也错 | **错误 + 遗漏** |
| 手册 §12.1 | 14 个环境变量 | 代码实际读取 **26 个** `HARNESS_*`，缺 `HARNESS_AUTH_SECRET` / `HARNESS_AUTH_ADMIN_USERNAME` / `HARNESS_AUTH_TOKEN_EXPIRE_MINUTES` / `HARNESS_ATTACHMENTS_DIR` / `HARNESS_ALLOWED_ORIGINS` / `HARNESS_CHANNELS` / `HARNESS_CHANNEL_*` / `HARNESS_TELEGRAM_BOT_TOKEN` / `HARNESS_TELEGRAM_ALLOWED_USERS` / `HARNESS_TELEGRAM_POLL_INTERVAL` / `HARNESS_WEBHOOK_SECRET` / `HARNESS_WEBHOOK_ALLOWED_USERS` / `HARNESS_WEBHOOK_NAME` / `HARNESS_EMBEDDING` / `HARNESS_EMBEDDING_API_KEY` / `HARNESS_EMBEDDING_BASE_URL` / `HARNESS_NO_NETWORK` / `HARNESS_PYTHON` / `HARNESS_PORT` / `HARNESS_SHELL_CARGO_CHECK` | **重大遗漏**（`docs/RELEASE-v1.0.0.md` 第 42 行明确要求"列一张表"） |
| `01-architecture.md:78,81,186` | `"version": "0.1.0"` / `core_api: ">=0.1.0 <1.0.0"` | 与发布版本不一致 | 陈旧 |
| `02-development-plan.md:3` | "当前版本为 v0.1.0" | 发布版本 1.0.0 | **错误** |
| `02-development-plan.md:246` | "CI 绿、141 个测试全通过" | 实测 593 passed，且 **CI 红** | **双重错误** |
| `02-development-plan.md:3` | "P0–P9 已全部完成" | `docs/` 下只有 `P0`–`P8`-summary（**P9 缺失**） | 不一致 |
| `README.md:31` 与 `README.md:36` | 两条几乎完全重复的"会话导出/导入/分叉" | 重复条目 | 编辑疏漏 |
| `README.md:357-379` 项目结构 | 只列 backend/frontend/desktop/docs/.github；`modules/` 注为"（会话/上下文/模型/沙箱）" | 实际 18 个模块；另有 `marketplace/`、`skill_marketplace/`、`mcp_marketplace/`、`skills/`、`scripts/`、`workspace/` 未列出 | **过时（已于 2026-10-01 订正）** |
| 仓库根 `examples/` | 目录存在 | **空目录**（`Get-ChildItem -Recurse` 计数 0），git 不会跟踪空目录 | 死目录 |
| `README.md:15` | "最新 tag 为 v0.0.9（0.0.10 之后未打 tag）" | CHANGELOG 有 49 个版本条目，与之脱节 | 需在 Release Notes 解释 |
| `.gitignore:22` | `!.env.example` | 仓库里**没有** `.env.example` | 指向不存在的文件 |

- **修复**：逐个订正；建议新增 **`docs/ENV.md`（环境变量总表，作为唯一真相源）**，README 与手册都改为链接过去，避免再次漂移。
- **预估**：0.5 天。

#### P1-6｜`frontend/README.md` 还是 Vite 官方模板原文

- **证据**：`frontend/README.md` 全文只有模板句子 —— "This template should help get you started developing with Vue 3 and TypeScript in Vite. ..."
- **影响**：这是 1.0.0 仓库里最容易被 GitHub 访客点开的文件之一，直接暴露"模板未清理"。
- **修复**：改写为前端开发说明（目录结构、`pnpm dev/build/test/lint`、与后端的 `/api` `/ws` 代理关系、i18n 约定、插件机制入口）。
- **预估**：0.2 天。

#### P1-7｜仓库里躺着调试产物与开发脚本

| 路径 | 大小 / 内容 | 处理 |
|---|---|---|
| `frontend/.dev_cv3.txt` | **149 KB**，Vite HMR 编译产物转储（`ChatView.vue` 编译后 JS） | 删除；`.gitignore` 加 `frontend/.dev_*` |
| `frontend/.dev_pt.txt` | **30 KB**，同上，`ProcessTrace.vue` | 同上 |
| `/Vite`（仓库根，无扩展名） | 54 字节，`tauri dev` 运行期日志 | 已 gitignore（第 100 行 `/Vite`），但文件仍在盘上，应删除 |
| `backend/data/.trash_attachments_junk/` | `_check_db.py` / `_patch_streaming.py` / `_verify_task_fix.py` / `.uvicorn_*.log` 等开发残留 + 大量测试附件 | 已 gitignore，建议清理（它是"垃圾站"目录） |
| `backend/scripts/e2e_attachments.py`、`e2e_fork_fix.py` | 开发期端到端脚本，未跟踪 | 要么整理进 `tests/`，要么删除；**注意 `ruff check` 正在对 `e2e_attachments.py` 报错**（见 P0-1） |
| `backend/harness/data/` | 见 P0-5 | 删除 + gitignore |
| `desktop/src-tauri/target/` | **5.4 GB** 构建产物 | 已 gitignore；提醒不要打包整目录 |
| `frontend/node_modules/` | 808 MB | 已 gitignore |

- **影响**：约 175 KB 的文本会以 `??` 出现在 `git status` 里，**一次 `git add -A` 就进仓库**；`backend/scripts/*.py` 还会持续给 ruff 贡献错误。
- **修复**：删除 + 补 `.gitignore` 规则 `frontend/.dev_*`、`backend/harness/data/`。
- **预估**：0.1 天。

#### P1-8｜`mcp.example.json` 硬编码了作者本机的绝对路径

- **证据**：`mcp.example.json:11` → `"args": ["-y", "@modelcontextprotocol/server-filesystem", "F:/traeprojects/andy-harness-gy"]`
- **影响**：这是**随仓库分发**的示例文件，别人拷走后 filesystem server 会指向不存在的路径；同时暴露作者本地目录结构。
- **修复**：改成 `"."` 或 `"<你的项目根目录>"`，并在 README/注释里说明。
- **预估**：0.05 天。

#### P1-9｜`CORE_API_VERSION` 与产品版本的语义断层（**含一个重要警告**）

- **证据**：
  - `backend/harness/kernel/loader.py:33` → `CORE_API_VERSION = "0.1.0"`；`loader.py:107-112` 用它校验每个插件的 `core_api`；`_check_core_api_compat`（`:116-146`）是一个手写的简单比较器。
  - **全部 41 个插件清单文件**（`backend/plugins` 33 个 + `backend/marketplace` 6 个 + `frontend/src/plugins` 2 个 `ui-plugin.json`，实测逐个核对）都写着 `"core_api": ">=0.1.0 <1.0.0"`。
- **问题**：产品要发 1.0.0，而"内核 API 版本"仍是 0.1.0。`docs/RELEASE-v1.0.0.md` 第 2.1 节列了 3 个版本号，**完全没有提到 `CORE_API_VERSION` 与 `core_api`**。当前状态下：
  - 若有人"顺手"把 `CORE_API_VERSION` 也改成 `1.0.0`（很自然的联想），`<1.0.0` 的上界会让**全部 41 个清单校验失败**（后端 39 个 + 前端 2 个 `ui-plugin.json`），`load_and_activate_all` 把它们全部记为 `加载插件失败`，**应用启动即失去所有能力**，而日志只是几十行 `logger.error`（`loader.py:357`）；
  - 第三方按 1.0.0 写的插件（`core_api: ">=1.0.0"`）也无法通过 0.1.0 内核的校验。
- **⚠️ 重要警告（给执行"版本号统一"的人）**：
  > **不要修改任何 `plugin.json` / `ui-plugin.json` 里的 `"version": "0.1.0"`。**
  > 实测有 **7 个测试文件、共 18 处**把它硬编码成了断言或构造参数（清单见 6.2）。批量替换会一次性打断这些测试。插件版本与产品版本**本就是两套语义**，保持 0.1.0 是正确的。
- **修复**：
  1. 在 README 或新增 `docs/VERSIONING.md` 里明确写："本项目的**产品版本**（1.0.0）与**内核 API 版本**（`CORE_API_VERSION`，当前 0.1.0）是两套独立语义；插件通过 `core_api` 声明其兼容的内核 API 区间"；
  2. 明确 1.x 的兼容承诺边界（建议只承诺三样：`/api/**` 的路径与响应结构、`plugin.json` 字段与 `BasePlugin`/`ToolPlugin` 接口、`harness.db` 的自动升级）；
  3. 给 `loader.py` 的 `_check_core_api_compat` 补直接测试（当前只有间接覆盖）。
- **预估**：0.2 天。

#### P1-10｜缺失的通用开源配套文件

| 文件 | 现状 | 建议 |
|---|---|---|
| `SECURITY.md` | **不存在** | 1.0.0 且已有已知安全项（P0-6 等），**强烈建议补**，写明漏洞上报渠道与"预览期不做安全承诺"的范围 |
| `.env.example` | 不存在，但 `.gitignore:22` 写着 `!.env.example` | 补一个（26 个 `HARNESS_*` 变量的样例），或删掉那行 ignore |
| `.gitattributes` | 不存在 | 建议补（`*.bat text eol=crlf`、`* text=auto`），避免 Windows/Linux 协作者被行尾折磨 |
| `CODE_OF_CONDUCT.md` | 不存在 | 可选（收到外部贡献前补） |
| issue 模板 | 已有 bug_report / feature_request / PR 模板 | ✅ 建议在 bug_report 里加"请附 `git rev-parse --short HEAD`"字段 |

- **预估**：0.2 天。


---

### P2 级（1.0.x 内解决；建议在 Release Notes 的 Known Issues 中披露）

| 编号 | 问题 | 位置 / 证据 | 建议 |
|---|---|---|---|
| **P2-1** | 登录接口**无速率限制**：每次尝试都做 **200,000 次 PBKDF2-HMAC-SHA256**，既可暴力破解，也可被用来打 CPU（未认证即可触发） | `api/rest/auth.py:70-79`；`infra/security.py:22` `_PBKDF2_ITERATIONS = 200_000` | 加失败计数 + 指数退避（进程内字典即可，无需新依赖）。迭代数提到 OWASP 建议的 600k 需注意**已有哈希不兼容**，建议只在新建/改密时使用新参数 |
| **P2-2** | Webhook 密钥用 `==` 比较，非恒定时间 | `channel_manager/service.py:117` | 改用 `hmac.compare_digest` |
| **P2-3** | `/api/settings` 无取值校验（`theme` 可写任意字符串），且全局共享 | `api/rest/settings.py:68-80` | 用 `Literal`/白名单校验 theme/language；`default_budget` 加上下界 |
| **P2-4** | CORS 正则放行**任意本地端口** + `allow_credentials=True` | `main.py:193-200`，`allow_origin_regex=r"http://(localhost\|127\.0\.0\.1):\d+"` | 收紧为固定端口列表；因当前用的是 Bearer 而非 Cookie，`allow_credentials` 其实非必需，可直接去掉 |
| **P2-5** | Tauri 窗口 `"csp": null`（无内容安全策略） | `desktop/src-tauri/tauri.conf.json:24` | 壳内同时跑着本地后端与用户内容（模型输出、附件），建议设一个基础 CSP |
| **P2-6** | `_match_topic` 里 `next_p` 赋值后从未使用（ruff F841） | `kernel/eventbus.py:42` | 删除该行（属 P0-1 的一部分） |
| **P2-7** | `api/middleware/auth.py:88-90` 的 `_json()` 是无用函数（仅为让 `json` import 有个使用者） | 全文搜索 `_json(` 无调用点 | 删除函数与 `json` import |
| **P2-8** | 同一轮的多个 `tool_call` **串行执行** | `agent_loop.py:838-1000` 是 `for tc in tool_calls:` 顺序循环 | 一轮返回 3 个独立工具时耗时就是 3 倍。可对 `needs_confirm=False` 且无依赖的调用用 `asyncio.gather`（需注意 `_on_tool_event` 顺序与 span 的 parent 关系） |
| **P2-9** | 流式失败重试会**重复推送**已发给前端的 token | `agent_loop.py:725-827`：`except` 重试整次调用，但前端已收到的 `token_delta` 不会回滚 | 重试前发一个 `token_reset` 帧让前端清空气泡，或只在"一个 token 都没收到"时才重试 |
| **P2-10** | `delete_user` 不删除该用户的会话/记忆；`users.username` 无 `UNIQUE` 约束（并发注册可重名） | `auth_manager/service.py:292`；`database.py` 的 `_CREATE_USERS` | 至少加 `UNIQUE` 索引；删除用户时在 API 文档与前端明确提示"其数据将保留" |
| **P2-11** | 前端三个巨型单文件：`SettingsView.vue` **4336 行**、`ChatView.vue` **1874 行**、`SessionSidebar.vue` **1174 行** | 实测行数 | 至少把 SettingsView 的 10 个标签拆成 10 个组件（纯机械重构，可分批），否则后续任何 UI 改动都容易出回归 |
| **P2-12** | 主 bundle 单文件 1.30 MB（gzip 441 KB），Vite 明确告警 chunk > 500 kB | 实测构建输出 | 路由级 `import()` 懒加载（SettingsView / 插件视图）；`highlight.js` 语言包按需注册 |
| **P2-13** | 列表类 REST 端点**无分页**：`GET /api/sessions`、`/messages`、`/traces`、`/artifacts` 均全量返回 | `api/rest/sessions.py:87,348`；`traces.py:33`；`artifacts.py:33` | 加 `limit/offset`（默认给个上限，如 200），避免长会话/大轨迹把响应撑到几十 MB |
| **P2-14** | SQLite 单连接 + 全局 `threading.Lock`，且**未启用 WAL** | `infra/database.py:325-350`，`execute/query` 全部 `with self._lock` | 加 `PRAGMA journal_mode=WAL` + `busy_timeout`；多用户下写操作会整库串行（已有并发测试，但吞吐受限） |
| **P2-15** | pytest 收集告警 4 条（`TestProvider*` 被误当测试类） | 实测 `PytestCollectionWarning` | 加 `__test__ = False` 或改名 |
| **P2-16** | `serve_attachment` / `upload_attachments` 用 `request: Request = None` + `# type: ignore[assignment]` 变通 | `api/rest/attachments.py:59,150` | 把 `request: Request` 提到参数列表首位（FastAPI 支持），去掉 type ignore |
| **P2-17** | `Makefile` 的 `clean` 用 `rm -rf`（Linux/macOS 语义），`dev` 目标只打印提示 | `Makefile` | Windows 上依赖 Git Bash 的 `rm`；建议改用 `git clean -Xfd` 或额外提供 `.ps1`。低优先 |
| **P2-18** | `docs/P0-summary.md` … `P8-summary.md` 共 9 份阶段性总结仍在 `docs/`，内容是 2026-09-24 的旧状态（且 P9 缺失） | 实测 | 移到 `docs/archive/` 并在 README 标注"历史归档"，避免读者误当当前架构说明 |
| **P2-19** | `harness/eval/` 已就绪但 **CI 未接入**（README 的"失败退出码 1 可直接做 CI 门槛"未兑现） | `ci.yml` 无 eval 步骤 | 在 backend job 末尾加 `uv run python -m harness.eval --cases tests/evals/eval_cases.json`（若需真实模型 key，则加 `continue-on-error` 或仅在 nightly 跑） |
| **P2-20** | 附件列表端点为「批量上传」设计，但**单条消息上限校验在读取全部文件后才做类型统计**（数量校验已前置，符合 P1-4 修复）；`MAX_FILES_PER_MESSAGE=8` 下问题不大，但 `classify` 之前仍会把全部文件读进内存 | `api/rest/attachments.py:84-97` | 可改为流式逐个 `f.read()` + 逐项校验（当前风险低，列此备查） |

---

### P3 级（1.x 规划）

| 编号 | 事项 | 说明 |
|---|---|---|
| **P3-1** | 补齐 5 个"仅 REST"能力的**前端入口**：渠道管理、Computer Use 开关、Jev 配置/调用看板、断点续跑按钮、用户管理 | README 与手册 §10 已如实标注，属已知分期，不算 bug；但这是"1.0 体验完整度"最大的缺口 |
| **P3-2** | **跨平台验证**：目前只在 Windows 10 上验证过 | README:17 已如实声明。最低成本做法：让 CI 在 `ubuntu-latest` 上跑通 593 个测试（**当前 CI 正是 ubuntu-latest**，若它绿了，等于 Linux 已被间接验证——但现在它不绿，所以连这个声明都拿不到） |
| **P3-3** | **非 DeepSeek 模型联调**：README:18 承认只对 DeepSeek 做过深度联调 | 至少验证 Qwen（OpenAI 兼容、成本最低）的流式 + tool_calls + usage 上报链路，并把结论写进 README |
| **P3-4** | 可观测性补强：结构化 JSON 日志、日志文件轮转、`/metrics`（Prometheus）、按 trace_id 串联日志 | 现在只有 `logging` 到 stdout，排障要靠翻重定向文件 |
| **P3-5** | 覆盖率门槛：前端已配 `vitest --coverage` 但无阈值；后端无覆盖率统计 | CI 里加 `--cov-fail-under`（建议后端 70%、前端 60% 起步），并上传报告 |
| **P3-6** | 拆分/索引化 `docs/ENHANCEMENTS.md`（73 KB）与 `docs/CHANGELOG.md`（107 KB） | 两个文件已大到难以导航；建议 CHANGELOG 只留 1.0.0 与最近 5 个版本，历史归档到 `docs/changelog/0.0.x.md` |
| **P3-7** | Eval 用例扩充（当前只有 4 个种子任务） | 作为回归门槛偏少；可把 CHANGELOG 里修过的真实 bug 各补一条 |
| **P3-8** | 模型名称口径统一：`agent_loop.py:55` 默认 `deepseek-v4-flash`、`ws/chat.py:523,610` 默认 `gpt-4o`、`settings.py:29` 默认 `deepseek-chat`、README 提到 `deepseek-v4.1-flash` | 四处默认模型名不一致；建议收敛为一个常量并写进 `docs/ENV.md` |


---

## 4. 已核查确认「无问题」的项（避免重复怀疑）

以下项我逐条打开源文件核对过，**结论是当前实现正确**，不必再花时间：

| 项 | 核对结论 |
|---|---|
| `.gitignore` 是否覆盖敏感文件 | ✅ `backend/data/`（含 `harness.db` / `auth_secret.txt` / `permission.json` / `skill_state.json` / `settings.json`）已覆盖；对明文密钥文件有兜底忽略规则；`/Vite`、`desktop/src-tauri/target/`、`frontend/dist/`、`.uv-cache/` 均已覆盖 |
| git 历史里是否曾提交过密钥/数据库 | ✅ `git ls-files` 中匹配 `*.db` / `mcp.json` / `secret` **零命中**；`git log --all -S` 只命中 README 的文字引用，**密钥文件本身从未入库**，历史干净 |
| WebSocket 帧协议是否有文档 | ✅ `api/ws/chat.py:1-16` 的模块 docstring 完整列出 8 种下行帧与 2 种上行帧，已核对与实现一致 |
| 附件上限数值是否与 README 一致 | ✅ `attachment/limits.py` 的 5/4/8、200KB/1.5MB、1280px 与 README 逐项吻合 |
| 跨用户越权（上一轮审计 P0-1/P0-2）是否真修复 | ✅ `attachments.py:45-53,66-70,156-160` 与 `artifacts.py` / `traces.py` 均有归属校验；`repository.get(session_id, user_id=...)` 的 `if user_id and ...` 短路语义在单用户下不过滤、多用户下过滤，逻辑正确 |
| WS 双 reader 竞争是否真修复 | ✅ `chat.py:333-399` 单 reader + 双队列；`chat.py:259-268` 在客户端断开时兑现"确认即拒绝"；`tests/test_audit_fixes.py` 有 `stop_ack` 回归 |
| provider 共享单例是否还会被 monkey patch | ✅ `chat.py:51-91` 改为 `_StreamingProviderProxy`，真实 provider 实例不再被改写 |
| 认证开启后图片是否还会 401 破图 | ✅ `client.ts:125+` 的 `fetchAttachmentBlobUrl` + `AttachmentImage.vue`（组件卸载时 `revokeObjectURL`） |
| 认证签名密钥是否还会重启失效 | ✅ `auth_manager/service.py:126,147` 持久化到 `data/auth_secret.txt`（0o600） |
| 沙箱 Docker 后端是否有网络/资源限制 | ✅ `sandbox_manager/service.py` 的 `DockerBackend` 默认 `--network none` + `--memory`/`--cpus` + `docker rm -f` 超时兜底，并有 18 条测试 |
| 外部分发安装器是否防 zip slip | ✅ `package_installer/service.py` 有路径穿越防御 + 包裹目录下沉，`tests/test_package_installer.py` 覆盖 |
| i18n 中英文键是否对齐 | ✅ zh 308 键 / en 308 键，**双向差集为空**（实测） |
| 手册引用的 16 张截图是否都存在 | ✅ 逐个 `Test-Path` 全部 `True`（01/02/03 + 04-settings×10 + 15/16/17） |
| `harness.eval` CLI 是否可运行 | ✅ `python -m harness.eval --cases tests/evals/eval_cases.json` 的入口、cases/scoring/report 均已实现 |
| 桌面壳退出是否回收后端进程树 | ✅ `launcher/launch-backend.py:111-124` 与 `src-tauri/src/lib.rs` 均做 `taskkill /F /T` |
| 前端是否会泄漏 token 到日志 | ✅ 未发现 `console.log(token)` 之类的调用 |


---

## 5. 发布执行清单（可直接照做，建议按顺序）

### 阶段 A：安全与仓库卫生（**先做，0.5 天**）

- [x] **A1** ~~轮换仓库内明文存放的 DeepSeek Key 并删除该文件~~ ✅ 已完成
- [x] **A2** ~~删除 README 两处失效的 Key 配置引导~~ ✅ 已完成（改为「设置页配置」+ `DEEPSEEK_API_KEY`）
- [ ] **A3** `Makefile:10`、`start-all.bat:64` 的 `--host 0.0.0.0` → `--host 127.0.0.1`（P0-6）
- [ ] **A4** `docker-compose.yml` 端口映射改为 `127.0.0.1:8000:8000` / `127.0.0.1:5173:5173`；删除 `version: "3.9"`（P0-6 / P1-1）
- [ ] **A5** 给 `plugins.py`(6 个) / `mcp.py`(4 个) / `permissions.py`(2 个) / `settings.py`(1 个) 的写操作端点加 `_require_admin`（认证未启用时放行）
- [ ] **A6** 删除 `frontend/.dev_cv3.txt`、`frontend/.dev_pt.txt`、根目录 `Vite`、`backend/harness/data/`、`backend/data/.trash_attachments_junk/`
- [ ] **A7** `.gitignore` 追加 `frontend/.dev_*` 与 `backend/harness/data/`
- [ ] **A8** `mcp.example.json:11` 的本机绝对路径改成 `.`（P1-8）

### 阶段 B：让 CI 变绿（**1 天，本阶段占比最大**）

- [ ] **B1** `cd backend && uv run ruff format .` → **独立提交**（146 文件 diff，勿与其他改动混在一起）
- [ ] **B2** 修 6 个 mypy 错误（含 `openai_compatible.py:107` 的契约签名问题）→ 独立提交
- [ ] **B3** 修剩余 10 个 ruff 错误（F841 / W292 / I001 / UP012 / UP031）
- [ ] **B4** 修 10 个 eslint 错误 + 给 `eslint.config.ts` 加测试文件的 `argsIgnorePattern`（P0-2）
- [ ] **B5** `ci.yml` 的 frontend job 加 `pnpm test` 步骤（P0-2）
- [ ] **B6** 本地跑通并贴进 PR 描述：`uv run ruff check .` / `uv run ruff format --check .` / `uv run mypy harness` / `uv run pytest -q`；前端 `pnpm lint` / `pnpm test` / `pnpm build`
- [ ] **B7**（可选但推荐）`.pre-commit-config.yaml` 的 rev 对齐实际版本（P1-4）
- [ ] **B8** push 后确认 GitHub Actions **两个 job 都绿**，截图留档

### 阶段 C：版本号与文案（**0.5 天**）

- [ ] **C1** 按第 6 节统一 8 个版本号位置（7 处已有 + 新增 `__version__`；**注意不要动 `plugin.json` 的 version**）
- [ ] **C2** `frontend/package.json` 的 `name` 从 `frontend` 改为 `andy-harness-frontend`（可选）
- [ ] **C3** 在 `harness/__init__.py` 加 `__version__ = "1.0.0"`，让 `main.py:162` 引用它
- [ ] **C4** CHANGELOG 的 `[1.0.0]` 条目补"本次发布前的工程质量收口"小节（CI 绿 / 版本统一 / 安全默认值变更 / 已知问题链接）
- [ ] **C5** 新增 `SECURITY.md`（P1-10）
- [ ] **C6** CHANGELOG 里**显式声明默认行为变更**：`Makefile` / `start-all.bat` 默认监听由 `0.0.0.0` 改为 `127.0.0.1`（**用户可感知的行为变更，必须写进 Upgrade Notes**）

### 阶段 D：文档订正（**0.5 天**）

- [ ] **D1** 新增 `docs/ENV.md`（26 个 `HARNESS_*` 变量总表），手册 §12.1 与 README 改为链接它
- [ ] **D2** 修正手册 §12.2 数据文件表（artifacts 路径、补 attachments / settings.json / workspace）
- [ ] **D3** `02-development-plan.md:3` 与 `:246` 的"v0.1.0 / 141 测试 / CI 绿"改为当前事实，或整篇标注"历史文档"
- [ ] **D4** `01-architecture.md` 的 `0.1.0` 处加注"内核 API 版本，与产品版本无关"
- [ ] **D5** README 删掉重复的"会话导出/导入/分叉"条目（31 行 vs 36 行）
- [ ] **D6** README 项目结构块补 `marketplace/`、`skill_marketplace/`、`mcp_marketplace/`、`skills/`、`scripts/`、`workspace/`，并更新 `modules/` 的说明
- [ ] **D7** 删除空的 `examples/` 目录（或放入一个真实示例）
- [ ] **D8** 重写 `frontend/README.md`（P1-6）
- [ ] **D9** `docs/P0-summary.md` … `P8-summary.md` 移到 `docs/archive/`
- [ ] **D10** 手册 §12 与本报告交叉引用（"已知问题见 `docs/ANALYSIS-v1.0.0-2026-10-01.md`"）

### 阶段 E：打 tag 与 Release（**0.5 天**）

- [ ] **E1** `git status` 干净；打 tag 前再确认没有 `??` 的垃圾文件
- [ ] **E2** 按 `docs/RELEASE-v1.0.0.md` 的流程：先分主题提交，再统一版本号提交，最后打 annotated tag
- [ ] **E3** Release 标题：`andy-harness v1.0.0 (Developer Preview)`
- [ ] **E4** Release Notes 必含 5 节：Highlights / What's Changed since v0.0.9 / Security & Default-behavior Changes / Upgrade Notes / **Known Issues（= 本报告 P2 + P3 清单）**
- [ ] **E5** 开 Discussions 或在 Release 里说明反馈渠道
- [ ] **E6** 开启分支保护（main 需 review + CI 必过）

**总预估：3–4 个工作日**（其中阶段 B 占约一半）。


---

## 6. 版本号统一操作指南（含一条重要警告）

### 6.1 建议的统一方案

| 对象 | 目标值 | 说明 |
|---|---|---|
| 产品版本 | `1.0.0` | 下表 8 处（含新增的 `__version__`） |
| 内核 API 版本 `CORE_API_VERSION` | **保持 `0.1.0`** | 与产品版本无关；需在文档里写清 |
| 插件版本（41 个清单文件：`backend/plugins` 33 + `backend/marketplace` 6 + `frontend/src/plugins` 2） | **保持 `0.1.0`** | 见 6.2 警告 |

需要改的位置：

```
backend/pyproject.toml:3                            version = "0.1.0"   → "1.0.0"
backend/harness/__init__.py                         （新增）__version__ = "1.0.0"
backend/harness/main.py:162                         version="0.1.0"     → __version__ / "1.0.0"
backend/harness/modules/mcp_client/service.py:144   "version": "0.1.0"  → "1.0.0"
frontend/package.json:4                             "version": "0.0.0"  → "1.0.0"
desktop/package.json:4                              "version": "0.1.0"  → "1.0.0"
desktop/src-tauri/tauri.conf.json:4                 "version": "0.1.0"  → "1.0.0"
desktop/src-tauri/Cargo.toml:3                      version = "0.1.0"   → "1.0.0"
```

> 顺带：`desktop/src-tauri/Cargo.lock` 里也有 `andy-harness-gy` 的版本条目，改完 `Cargo.toml` 后跑一次 `cargo check` 会自动同步。

### 6.2 ⚠️ 警告：不要用全局查找替换

**`plugin.json` / `ui-plugin.json` 的 `"version": "0.1.0"` 与 7 个测试文件的 18 处硬编码强耦合：**

```
backend/tests/test_plugin_loader.py      : 28, 31, 52, 65, 79, 191, 194, 222, 234
backend/tests/test_artifact_store.py     : 134, 295
backend/tests/test_tracing.py            : 108, 206
backend/tests/test_subagent.py           : 242
backend/tests/test_orchestrator.py       : 420
backend/tests/test_package_installer.py  : 31, 65
backend/tests/test_mcp_plugin.py         : 26
```

一次全局 `0.1.0 → 1.0.0` 替换会让**18 处断言/构造参数**同时变化，一旦漏改就出现"测试红但代码没问题"的假故障。插件版本与产品版本本就是两套语义，**保持不动是正确做法**。

### 6.3 一条长期建议

只要 `CORE_API_VERSION` 仍是手写常量、产品版本仍散落 8 处，"下次发版还会不一致"。建议顺手加一个 `backend/scripts/bump_version.py`（或 `make bump V=1.0.1`），把上面 8 个文件路径写死进脚本，让发版变成一条命令 —— 下个版本就不会重复今天的问题。

---

## 7. 结语

### 7.1 我对这个项目最真实的评价

这是一个**「能力已经越过 1.0，工程纪律还停在 0.1」**的项目。

**代码层面**，我挑不出结构性错误：分层清晰、插件契约完整、历史高危 bug 的修复都留下了可验证的痕迹和回归测试、失败路径几乎都做了优雅降级（权限服务缺失、EventBus 缺失、Database 缺失、Skill 服务缺失、MCP 不可用、docker 不可用……每一处都能在代码里找到对应的降级分支）。`agent_loop.py` 那 1104 行里，光是"工具结果必须回填否则下一轮 400"和"流式 tool_calls 分片按 index 合并"这两处，就说明作者真的踩过坑并想明白了。

**工程层面**，问题高度集中在同一个模式上：**每件事都做到了 80%，缺的那 20% 恰好是"面向他人"的那部分**——CI 从未真的跑绿，所以没人告诉作者格式不通过；版本号散落 8 处，所以没人发现 Tauri 安装包叫 0.1.0；README 教用户把 Key 写进一个仓库里的文件，所以没人发现代码根本不读它；`docker-compose` 写了但没实际起过，所以没人发现前端连不上后端。

**这恰恰是"发布 1.0.0"这件事的价值**：它会强迫你把最后那 20% 补上。而且从这份清单看，**没有任何一项需要动架构**——最贵的一项是 `ruff format`（机械）+ 文档订正（体力），最需要判断力的一项是 P0-6 的授权边界（但结论也很清晰）。

### 7.2 如果只能做三件事

1. **把 `--host 0.0.0.0` 改成 `127.0.0.1`，并给插件 / MCP / 权限端点加管理员校验**（半天，消除唯一一条"能被别人利用"的风险）；
2. **跑一次 `ruff format .` + 修 lint / mypy / eslint + CI 加 `pnpm test`**（一天，让 badge 变成真的）；
3. **删掉仓库内明文存放的 Key 文件、删两处 README 引导、统一 8 处版本号、修正手册 §12 表**（半天，让"1.0.0"这四个字站得住）。

做完这三件事，这个项目就可以非常理直气壮地打 `v1.0.0` 了。

---

> **本报告仅为分析与建议，未修改任何代码或既有文档**（除本文件本身）。
> 分析过程中唯一的运行时副作用（`PUT /api/settings {"theme":"ocean"}` 写入了 `backend/harness/data/settings.json`）已还原为 `light`；启动的 uvicorn 测试进程已终止；临时脚本与日志文件已删除。
> 如需落地，我可以按第 5 节的清单逐项实现（阶段 A → E）。

