# 四大功能模块（洞察看板 / 开发者工作台 / 评测实验室 / 模板中心）验证与修复报告

- **验证日期**：2026-10-06
- **验证对象**：
  - `backend/harness/modules/insights/`（洞察看板，SQL 聚合服务 + REST）
  - `backend/harness/modules/devkit/`（开发者工作台，插件脚手架 + 热重载 + 配置）
  - `backend/harness/modules/evals_lab/`（评测实验室，复用 `harness.eval` 包）
  - `backend/harness/modules/templates_hub/`（模板中心，session/prompt/workflow 模板）
  - `backend/harness/kernel/{contracts/base.py, loader.py}`（插件清单与加载器，被 DevKit 依赖）
  - `frontend/src/views/{InsightsView,DevKitView,EvalsLabView,TemplateHubView}.vue`（前端四视图）
- **验证方法**：① 后端全量 `pytest`；② 针对四个模块的专项校验脚本（临时，已删除）；③ `ruff check` 增量；④ 前端 `vue-tsc -b --force` 类型检查 + 静态核对 i18n 文案（88 个键全部可解析）；⑤ 逐文件代码走查业务逻辑。
- **验证局限**：沙箱无浏览器，**前端交互未做浏览器实测**；但类型检查、构建、API 契约与后端逻辑均已实测通过。

---

## 一、结论摘要（TL;DR）

1. **共发现并直接修复 4 个后端 Bug**（均为影响数据正确性或可用性的实质缺陷）：
   - **[Insights]** 时间序列图（token/工具调用）在「非 UTC 时区机器」上与本地消息轴错位；且 `tokens_total` 取数来源错误（累加工具 span，约恒为 0）。
   - **[DevKit]** 插件配置（config）在热重载 / 重启后丢失——加载器只用 schema 默认值重建，不读持久化配置。
   - **[DevKit]** 后端未拦截核心插件热重载（前端已禁用按钮，但 API 无校验），热重载核心插件会破坏系统能力。
   - **[TemplateHub]** 工作流模板「实例化」写入了已废弃的 `steps_json` 列（真实图结构列是 `graph_json`），导致生成的 workflow 图为空、可编辑但无节点。
2. **评测实验室（EvalLab）经逐行核对，契约与 `harness.eval` 完全对齐，未发现需修复的缺陷**。
3. **前端四视图经类型检查 + i18n 文案核对，无阻断问题**；仅修正 `TemplateHubView.confirmUse()` 实例化工作流后的跳转路由（`/workflows`）。
4. **全量回归：725 passed / 2 skipped / 7 warnings**，无新增失败（warning 均为历史既有弃用提示，与本次改动无关）。

> 详细修复位置与轻重缓急见第五章。

---

## 二、各功能校验与修复明细

### 2.1 洞察看板（Insights）

**定位**：纯 SQL 聚合 `sessions` / `messages` / `spans` / `permission_audit`，输出概览、按日趋势、token 消耗、工具调用分布等。

**发现的问题（已修复）**：

| # | 问题 | 根因 | 修复 |
|---|---|---|---|
| I-1 | 按日趋势图（token、tool 调用）与消息/会话轴在「非 UTC 时区」机器上错位 | `spans.created_at` 以 **UTC** 存储（`tracing/service.py` 用 `datetime.now(UTC).isoformat()`），而图表 day-axis 用 `datetime(created_at)` 直接 `substr(...,1,10)` 走的是本地时间；日期分组与范围过滤混用 UTC/local，导致早期数据被裁掉或日期分桶错一天 | 所有 spans 聚合统一改用 `datetime(created_at, 'localtime')` 做「按日分组」与「范围过滤」（service.py:133/138/153/180/192） |
| I-2 | 概览卡片 `tokens_total` 长期≈0 / 与图表不符 | 原 `tokens_total` 累加的是 **tool span** 的 `total_tokens`（工具调用 span 不携带 token 字段，恒为 0）；且 `model_tokens_total` 本就正确 | 令 `tokens_total` 与 `model_tokens_total` 取同一口径——仅 `model` span 的 `total_tokens`（service.py:195-196）；并删除无用的 `_utc_cutoff` 辅助方法（ruff F841） |

**修复后行为**：时间轴与本地消息/会话轴一致；token 总量取自 model span，与趋势图同源，数值自洽。

### 2.2 开发者工作台（DevKit）

**定位**：插件脚手架生成（`backend/plugins/<id>/`），热重载（停用→卸载→清 `sys.modules`→重载→激活），插件配置读写。

**发现的问题（已修复）**：

| # | 问题 | 根因 | 修复 |
|---|---|---|---|
| D-1 | 插件配置（如用户改过的开关/密钥）在热重载或进程重启后丢失，回退成默认值 | `PluginLoader.load()` 仅用 schema 默认值重建 `PluginContext.config`，不读取 `plugin.json` 中已持久化的 `config` 字段；而 `PluginManifest` 也无 `config` 字段 | ① 给 `PluginManifest` 增加 `config: dict` 字段并在 `from_dict` 读取（`contracts/base.py:41/58`）；② 加载时先应用 schema 默认，再叠加 manifest 中已持久化的 config（`loader.py:190`）；③ `set_plugin_config()` 保存后写回 `plugin.json` 的 `raw["config"]`（新增 `_persist_config`，`loader.py:329/331`） |
| D-2 | 后端 API 未拦截核心插件热重载 | 前端按钮已禁用，但 `reload_plugin()` 仅校验插件存在性，未校验 `core` 标志；绕过前端直接调 API 可热重载核心插件，停用会破坏系统能力 | 在 `reload_plugin()` 读取 manifest 后、停用前增加 `if raw.get("core"): raise ValueError("核心插件不支持热重载")`（`devkit/service.py:342-344`），并复用已读取的 `raw` 避免重复 IO |

**修复后行为**：配置跨重启持久化；核心插件热重载被前后端双重拦截。

### 2.3 评测实验室（EvalLab）

**定位**：复用 `harness.eval` 包（`EvalRunner` / `EvalReport` / `load_cases` / `CaseResult.to_dict`）实现用例 CRUD、运行、报告。

**校验结论**：逐行比对 `evals_lab/service.py` 与 `evals_lab.py` 的调用契约，与 `harness.eval` 的公开 API **完全一致**；`create_run` 通过 `asyncio.create_task` 调度（FastAPI 进程内存在运行事件循环，OK）。**未发现需修复的缺陷**。

**仅有的设计级观察（未改动，见第四章 P2）**：运行中任务句柄未持久化，若服务在评测中途重启，`runs` 行可能停留在 `running` 状态。

### 2.4 模板中心（TemplateHub）

**定位**：session / prompt / workflow 三类模板的 CRUD，与「实例化」（渲染 `{{var}}` 并落地为新资产）。

**发现的问题（已修复）**：

| # | 问题 | 根因 | 修复 |
|---|---|---|---|
| T-1 | 工作流模板「实例化」生成的 workflow 图为空（无节点/边），打开编辑器是空白画布 | 实例化 INSERT 写入了 **已废弃的 `steps_json` 列**；而 `workflows` 表经 DDL 迁移后，真实图结构列是 **`graph_json`**，故生成的 workflow 图结构全丢 | 实例化分支改为写入 `graph_json`，图来源解析为 `payload.get("graph") or payload.get("steps") or {"nodes":[],"edges":[]}`（`templates_hub/service.py:203-207`）；同时 `create_template` 的长 INSERT 拆行（ruff E501） |

**同步前端修复**：

- `TemplateHubView.confirmUse()`：当响应含 `res.workflow_id` 时跳转 `/workflows`（原逻辑未处理工作流类模板落地后的去向）；并将响应类型补全为 `{ type; session_id?; workflow_id?; pending_message?: string|null }`，避免 `pending_message` 误读。

**修复后行为**：从工作流模板实例化得到的是「带完整节点/边」的可编辑 workflow，而非空画布。

---

## 三、校验与健壮性验证

| 场景 | 期望 | 结果 |
|---|---|---|
| Insights：在 +8 时区机器上，spans 与 messages 同日数据落入同一 day 桶 | 对齐 | ✅（`'localtime'` 统一转换） |
| Insights：`tokens_total` == 趋势图 token 之和 | 一致 | ✅（同取自 model span） |
| DevKit：改插件 config → 热重载 → config 保留 | 保留 | ✅（manifest.config 叠加 + 写回 plugin.json） |
| DevKit：进程重启后 config 仍生效 | 生效 | ✅（load 时合并 manifest.config） |
| DevKit：对 `core:true` 插件调 `reload` | 拒绝并 400 | ✅（`ValueError` → 前端提示） |
| TemplateHub：从工作流模板实例化 | 生成非空 workflow | ✅（写入 `graph_json`） |
| EvalLab：用例 CRUD + 运行 + 报告 | 全通 | ✅（契约对齐，无改动） |
| 前端 `vue-tsc -b --force` | 0 错误 | ✅ |
| 后端全量 `pytest` | 无回归 | ✅ 725 passed / 2 skipped |

---

## 四、设计层面的其他观察（未改动，待决策）

下列为走查中发现的**非阻断、但值得在后续迭代关注**的设计点，本次未擅自修改（涉及 schema/行为变更，需你确认方向）。

- **[I-设计] 洞察看板跨租户聚合**：`insights.service` 当前跨所有 session 聚合，未带 tenant 过滤。当前 harness 为单用户定位，可接受；若后续引入多租户隔离，需补 `tenant_id` 过滤。
- **[I-设计] 无缓存/分页**：概览每次请求全表聚合，数据量极大时可能偏慢；可加按天增量缓存（低优先级）。
- **[D-设计] 热重载的 `sys.modules` 残留**：重载时清的是插件包自身模块；若其他模块在顶层 `import` 了该插件的符号，仍持有旧引用。插件式架构下影响有限，属 Python 固有约束。
- **[E-设计] 评测任务句柄未持久化**：`create_run` 用 `asyncio.create_task` 且未存句柄；服务中途重启会让 `runs` 行卡在 `running`。可加「启动时把 `running` 修正为 `failed/interrupted`」的补偿逻辑（P2）。
- **[T-设计] 模板实例化是否应「复制为草稿」**：当前 workflow 模板实例化直接落库为正式 workflow；是否需要「先存为草稿/可命名」由你定。

---

## 五、轻重缓急清单（P0 / P1 / P2）

评级说明：**P0**=影响数据正确性或可用性的实质缺陷（本次已修复）；**P1**=明显能力缺口或体验硬伤，下个迭代应修；**P2**=体验增强/健壮性补全，可排期。

### P0 — 已直接修复（本次）

- **[P0-1][Insights] token/工具时间序列轴 UTC↔local 错位 + `tokens_total` 取数错误** → 已修（`service.py` 统一 `'localtime'` 转换 + 改取 model span）。
- **[P0-2][DevKit] 插件配置热重载/重启后丢失** → 已修（`manifest.config` 字段 + 加载叠加 + 写回 `plugin.json`）。
- **[P0-3][DevKit] 核心插件热重载无后端拦截** → 已修（`reload_plugin` 校验 `core`）。
- **[P0-4][TemplateHub] 工作流模板实例化图结构落入废弃列** → 已修（写入 `graph_json`）。

### P1 — 高优先级（建议下迭代）

- **[P1-1][Insights] 多租户隔离下的聚合过滤**：补 `tenant_id` 过滤（若启用多用户）。
- **[P1-2][EvalLab] 运行中断补偿**：服务启动时将遗留 `running` 状态修正，避免「卡死」的评测记录。

### P2 — 中低优先级（健壮性/体验）

- **[P2-1][Insights] 聚合缓存**：按天增量缓存概览，缓解大表全聚合开销。
- **[P2-2][DevKit] 热重载引用一致性说明**：在插件开发文档中注明「顶层 import 符号不会随重载更新」的约束。
- **[P2-3][TemplateHub] 实例化交互增强**：工作流/会话模板实例化后可命名 + 存为草稿选项。

---

## 六、验证方法与可执行证据（复现）

1. **专项校验脚本（已运行后删除）**：临时 `backend/verify_fixes.py` 以临时库/临时插件目录分别验证 test_insights / test_devkit_config / test_templates_workflow / test_evals_crud，四节均打印「全部校验通过 ✅」。
2. **后端全量回归**：
   `cd backend && CODEBUDDY_SAFE_DELETE_ENABLED=0 .venv/Scripts/python.exe -m pytest -q --basetemp="C:/Users/Administrator/AppData/Local/Temp/harness_pytest_bt"`
   → **725 passed, 2 skipped, 7 warnings**（2m23s，无新增失败）。
3. **增量 lint**：`ruff check .` 对改动文件无新增错误（仅保留历史既有 `UP017`/`F841` 风格提示，其中 `F841` 已借本次清理）。
4. **前端校验**：`cd frontend && CI=true pnpm exec vue-tsc -b --force` → 退出码 0；并静态核对四视图 i18n 键（88 个）全部可解析，`chat.ts` 的 `pendingAutoSend` 消费正常。
5. **代码走查**：逐文件确认 `evals_lab` 契约与 `harness.eval` 对齐；`templates_hub`/`devkit`/`insights` 修复点落盘（见第二章引用行号）。

---

## 七、总体评价

四大功能模块**业务逻辑基本正确、可正常使用**，本次校验聚焦「设计不合理 / 业务逻辑有误 / 有 bug」三类问题，**直接修复了 4 个实质后端缺陷**（洞察看板时间轴与 token 口径、开发者工作台配置持久化与核心插件保护、模板中心工作流图结构落库列），评测实验室经核无需改动，前端仅修了工作流模板实例化后的路由跳转。全量回归 **725 通过、无回归**，类型检查与构建均干净。

无阻塞性缺陷；建议下一迭代按 P1 处理「多租户聚合过滤」与「评测中断补偿」，并按 P2 排期体验增强。
