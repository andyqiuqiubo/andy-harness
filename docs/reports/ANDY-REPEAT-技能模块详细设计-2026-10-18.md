# 操作录制与自动化回放技能模块（andy-repeat）详细设计

> **文档性质**：工程实施蓝本（v1.0）。上游产品方案见《ANDY-REPEAT-方案设计-2026-10-18.md》（下称"v2 方案"），本文不重复产品论证与竞品分析，只回答"怎么建、建成什么样、怎么验"。
> **适用范围**：`H:/jsd-andyharness` 仓库，Windows 10 及以上，Python 3.12+。
> **落地形态**：`backend/harness/modules/desktop_repeat/`（核心，可整体抽包）+ `backend/plugins/andy_repeat/`（插件与工具）+ `SKILL.md` 技能封装 + 独立包 `andy-repeat`。
> **日期**：2026-10-18 · **状态**：待评审 → 评审通过即作为 M1 施工依据。

---

## 目录

| 章节 | 内容 | 主要读者 |
|---|---|---|
| [0](#0-摘要) | 摘要与关键指标 | 全部 |
| [1](#1-定位范围与约束) | 定位、范围、与 v2 方案的增量、术语 | 产品 / 架构 |
| [2](#2-总体架构) | 分层、进程线程模型、模块划分、依赖规则、ADR | 后端 |
| [3](#3-录制子系统) | 事件采集、UIA 快照、IME 归并、关键帧、隐私屏蔽 | 后端 |
| [4](#4-数据格式与存储策略) | bundle 布局、事件/步骤 Schema、版本迁移、容量 | 后端 / 测试 |
| [5](#5-核心流程与状态机) | 录制/回放/步骤三级状态机、编译管线、时序 | 后端 / 前端 |
| [6](#6-定位与执行引擎) | 定位链、坐标模型、动作矩阵、节拍与断言 | 后端 |
| [7](#7-异常处理设计) | 错误码、处理矩阵、14 个边界场景、失败阶梯 | 后端 / 测试 |
| [8](#8-可扩展性设计) | 10 个扩展点、注册表、平台适配、Schema 演进 | 架构 |
| [9](#9-安全隐私与合规) | 三层防护、风险分级、审计、告知 | 产品 / 安全 |
| [10](#10-集成接口) | Python API、工具、REST/SSE、CLI、前端 | 全栈 |
| [11](#11-性能与容量预算) | 资源预算、长流程优化 | 后端 |
| [12](#12-测试与验收) | 测试分层、故障注入用例、验收判据 | 测试 |
| [13](#13-实施计划) | WBS 任务级拆解 | 项目管理 |
| [14](#14-风险登记册) | 风险与缓解 | 全部 |
| [15](#15-附录) | JSON Schema、错误码全表、配置参考 | 后端 |

---

## 0. 摘要

**一句话**：把"人操作一遍"变成"机器可确定性重放的一段结构化操作序列"，并以技能（SKILL.md）形态交给 Agent 调用，回放过程零模型调用。

三条核心结论：

1. **回放的可靠性来自数据，不来自模型**。录制阶段采集三类证据——事件流（时序/坐标/输入）、点击时刻的 UIA 语义快照、关键区域图像锚点；回放阶段按有序定位链逐级降级，主路径不依赖任何 LLM。
2. **失效是常态，设计目标是把失效变成可预测、可定位到步骤、可低成本修复**。为此本模块定义 37 个错误码、三级状态机与"重试→降级→放宽→挂起人工→中止取证"的失败阶梯，坐标兜底受环境闸门控制，默认拒绝"盲点"。
3. **可扩展性靠注册表而非分支**。定位器、动作、等待、断言、解释规则、存储后端、平台适配共 10 个扩展点，全部以装饰器注册，核心执行器只遍历注册表，新增能力零侵入。

关键指标（目标值，M2 末以真机数据校准）：

| 指标 | 目标 | 说明 |
|---|---|---|
| 录制事件丢失率 | ≤ 0.1%（move 除外） | 背压下优先丢弃移动摘要 |
| 钩子回调耗时 | p99 < 1 ms | 系统阈值 300 ms，留 300 倍余量 |
| 同机回放成功率 | ≥ 95% | 记事本 / 资源管理器 / Chrome 三场景各 20 次 |
| 窗口位移后回放成功率 | ≥ 90% | 相对定位生效 |
| 同比例分辨率变化后成功率 | ≥ 80% | 图像多尺度 + 相对定位 |
| 单步执行开销 | 中位 < 80 ms | 不含显式等待 |
| 回放 LLM 调用次数 | 0（主路径） | 断网可跑 |
| 标准留证档体积 | 6–12 MB / 10 min | 精简档 0.5–1.5 MB |

---

## 1. 定位、范围与约束

### 1.1 与 v2 方案的增量

v2 方案解决"做什么、为什么这样做"；本文解决"接口长什么样、状态怎么走、失败怎么办"。差异如下，**冲突时以本文为准**（本文更新）。

| 维度 | v2 方案 | 本文（v1.0 详细设计） |
|---|---|---|
| 状态机 | 未显式定义 | 录制 / 回放 / 步骤三级状态机 + 转移表 + 不变式 |
| 事件格式 | 单个 JSON 示例片段 | 完整字段表 + 19 种事件类型 + 机器可校验 Schema |
| 坐标模型 | 提到"逻辑坐标 + 缩放因子" | 三坐标空间 + 换算公式 + 环境漂移 L0–L3 分级 + 兜底闸门 |
| 异常处理 | 失败阶梯示意 | 37 个错误码 + 处理矩阵 + 14 个边界场景逐一展开 |
| 输入法 | "type 默认剪贴板粘贴" | 增加**录制期 IME 归并器**（值差分识别已上屏文本）+ 回放期 Unicode 注入 |
| 注入实现 | pyautogui | 直连 `SendInput`（`VIRTUALDESK` + `KEYEVENTF_UNICODE`），pyautogui 降为可选回退 |
| 运行记录 | 复用 `workflow_runs` | 新增 `repeat_runs` / `repeat_step_hits`，避免污染通用运行模型（ADR-06） |
| 扩展性 | "注册表范式"一句 | 10 个扩展点 + 契约 + 平台适配清单 + Schema 演进规则 |
| 实施 | 里程碑 | 任务级 WBS（含依赖与人日） |

### 1.2 能力范围

**范围内（In Scope）**

| 编号 | 能力 | 具体内容 | 验收见 |
|---|---|---|---|
| C1 | 操作事件录制 | 鼠标点击/双击/右键/拖拽/滚轮、键盘按键与组合键、文本上屏、窗口切换/移动/关闭 | §3.1–3.4 |
| C2 | 界面语义取证 | 点击时刻 UIA 最小属性集 + 祖先链 + 结构路径，300 ms 硬超时降级 | §3.5 |
| C3 | 关键帧留证 | 事件前/后裁剪帧、窗口变化全帧、三档留证策略 | §3.6 |
| C4 | 事件序列化 | JSONL 追加写、双时间轴、版本化、完整性清单、崩溃截断恢复 | §4 |
| C5 | 编译为步骤 | 9 阶段确定性管线：去噪→归并→窗口绑定→定位链→等待→断言→循环→参数→风险 | §5.4 |
| C6 | 自动化回放 | 定位链降级、UIA Pattern 优先、节拍与三级超时、断言校验、断点续跑 | §6 |
| C7 | 异常处理 | 窗口未找到 / 分辨率与 DPI 变化 / 焦点失败 / 弹窗遮挡 / 提权窗口等 | §7 |
| C8 | 技能封装 | 工作流 → SKILL.md（参数表 + 固定版本），对话中零 token 回放 | §8.5 |
| C9 | 可视化与可观测 | REST + SSE 实时步骤流、干跑体检报告、命中率统计 | §10 |
| C10 | 暂停与退出梯度 | 暂停/长暂停/丢弃/自动暂停四档，暂停区间零采集；回放侧紧急停止与接管暂停 | §3.10 |
| C11 | 回放参数化 | 录制值作默认值，回放时经对话框/Agent/CLI/调度四通道换值；格式前置校验；敏感参数强制人工 | §6.6 |

**范围外（Out of Scope，明确不做）**

1. **跨平台**：仅 Windows 实现；macOS/Linux 只保留接口边界（§8.3），不写实现。
2. **视频逐帧回放**：视频仅可作为可选审计附件（M3，默认关闭），永不作为回放依据。
3. **绕过 UIPI / 反自动化对抗**：提权窗口不可操作是系统硬限制，检测到即拒绝并引导，不做规避。
4. **全自动语义理解**：步骤切分以规则版为准；LLM 只改写标签与元信息，不增删步骤、不改定位链。
5. **多人协同录制 / 云端存储**：数据全部本地，默认不上传。

### 1.3 形态与命名

| 形态 | 路径 | 说明 |
|---|---|---|
| 核心模块 | `backend/harness/modules/desktop_repeat/` | 纯 Python，禁止 import fastapi / harness.api |
| 插件 | `backend/plugins/andy_repeat/` | 装配服务、注册 10 个工具、内置 1 个 SKILL.md |
| REST | `backend/harness/api/rest/desktop_repeat.py` | 12 个端点 + 1 个 SSE |
| 前端 | `frontend/src/views/RepeatStudioView.vue` | 路由 `/repeat`，三栏工作台 |
| 独立包 | `andy-repeat`（GitHub，MIT） | `repeat record\|compile\|replay\|dry-run\|skill` |
| 生成技能 | `~/.andy-harness/skills/repeat-<slug>/` | 每个已发布流程一个技能 |

### 1.4 术语

| 术语 | 英文/代码 | 定义 |
|---|---|---|
| 录制会话 | session / bundle | 一次录制的自包含目录，含事件流、帧、锚点、清单 |
| 事件 | event | 采集层输出的原子操作记录，一行一条 |
| 步骤 | step | 编译后的可执行单元，含动作、目标、时序、策略 |
| 定位器 | locator | 一种"找到目标"的策略实现（UIA/图像/相对/坐标） |
| 定位链 | locator chain | 有序定位器列表，首个命中即用 |
| 回放运行 | run | 一次步骤序列的执行实例 |
| 干跑 | dry run | 只解析不注入的体检执行 |
| 环境基线 | env baseline | 录制时刻的显示器拓扑、分辨率、缩放、会话状态 |
| 降级 | degraded | 运行中使用了非首位定位器，仍成功 |
| 留证档 | frame tier | minimal / standard / forensic 三档截图策略 |

---

## 2. 总体架构

### 2.1 分层视图

依赖方向严格单向：上层依赖下层，下层不得反向引用。

```
L6 接口层
├── service.py                  DesktopRepeatService（唯一对外门面）
├── plugins/andy_repeat/tools.py 10 个 Agent 工具
├── api/rest/desktop_repeat.py   REST + SSE
├── cli.py                       命令行
└── RepeatStudioView.vue         前端工作台
        │
L5 治理层
├── safety_gate.py        动作前置门（复用 SafetyPolicy 语义）
├── approval.py           危险步骤审批与会话授权
├── audit.py              追加式审计事件
├── redaction.py          日志/参数脱敏
└── notify.py             用户提示与回执（托盘/横幅/系统通知，见 §9.5）
        │
L4 执行层
├── executor/runner.py    RepeatRunner（asyncio 任务）
├── executor/actions.py   动作动词实现矩阵
├── executor/pacing.py    节拍、三级超时、输入租约
├── executor/recovery.py  失败阶梯
├── executor/assertions.py 断言
├── executor/envcheck.py  环境校验与漂移分级
└── locators/*            定位器注册表与四种实现
        │
L3 语义层
├── interpreter/pipeline.py   9 阶段编译管线
├── interpreter/merge.py      去噪归并
├── interpreter/waits.py      等待点推断
├── interpreter/locator_builder.py 定位链装配与打分
├── interpreter/risk.py       风险词库与标注
└── interpreter/semantic.py   LLM 可选增强
        │
L2 存储层
├── store/bundle.py       BundleWriter / BundleReader
├── store/jsonl.py        追加写、截断恢复
├── store/manifest.py     SHA256 清单与封版
└── store/migrate.py      Schema 迁移
        │
L1 采集层（仅录制器子进程内运行）
├── recorder/runner.py    子进程入口
├── recorder/normalizer.py 归一化与事件合成
├── recorder/ime.py       IME 上屏归并
├── recorder/frames.py    关键帧策略
├── recorder/overlay.py   tkinter 覆盖层
├── recorder/privacy.py   前置屏蔽
└── recorder/control.py   控制文件与心跳
        │
L0 平台适配层
└── platform/base.py + platform/win32/*（钩子/UIA/截图/注入/窗口/DPI/会话）
```

### 2.2 进程与线程模型

```
┌ backend 进程（FastAPI / Tauri 壳拉起的长活进程）
│
│  ├─ 主事件循环：Service / REST / SSE / RepeatRunner（asyncio）
│  ├─ 输入租约：全局命名互斥体 Global\andy_repeat_input
│  └─ 热键线程：紧急停止（RegisterHotKey，失败则 100 ms 轮询）
│
└─ Popen（detached）──► recorder 子进程（仅录制期间存在）
   ├─ 主线程：控制文件轮询（150 ms）+ 心跳写（500 ms）
   ├─ 钩子线程：pynput mouse/keyboard 低级钩子（只归一化 + 入队）
   ├─ worker 线程：UIA 查询 / 截图 / IME 值差分（重活）
   ├─ 写线程：events.jsonl 追加 + 定期 flush
   └─ UI 线程：tkinter 覆盖层（倒计时/状态胶囊/热键）
```

**为什么录制器必须是独立进程**（ADR-01）：

1. 崩溃隔离——钩子回调中的任何异常只终止 recorder，不影响 backend 与其他会话；
2. 延迟隔离——低级钩子回调超过 300 ms 会被系统静默摘除，必须避开 backend 的事件循环抖动与 GC；
3. DPI 隔离——recorder 进程启动即声明 `PER_MONITOR_V2`，保证采集坐标与 UIA 坐标同一空间；
4. 回收可控——沿用 `browser_daemon` 已验证的"Popen + 状态复核"范式。

**为什么执行器是 backend 内 asyncio 任务**：需要与审批、SSE、断点、调度器共享状态；且注入动作本身是同步短调用，放进线程池即可，无需独立进程。

### 2.3 模块划分与职责

| 模块 | 职责（单一） | 输入 | 输出 | 可独立抽包 |
|---|---|---|---|---|
| `platform/*` | 屏蔽 OS 差异，提供 5 个 Protocol | 系统调用 | 原始数据 | 是（需平台实现） |
| `recorder/*` | 事件与证据采集 | 用户操作 | bundle | 是 |
| `store/*` | bundle 读写、校验、迁移 | 事件/元信息 | 文件 | 是 |
| `interpreter/*` | 事件流 → 步骤序列 | bundle | `CompileResult` | 是 |
| `locators/*` | 目标定位 | 定位规格 + 执行上下文 | `Hit` / `Miss` | 是 |
| `executor/*` | 步骤执行、恢复、断点 | 步骤序列 | `RunRecord` | 是 |
| `skill/*` | 工作流 → SKILL.md | 工作流 | 技能目录 | 是 |
| `service.py` | 编排与对外 API | 上层请求 | DTO | 是（注入依赖） |
| `plugins/andy_repeat/*` | 装配、工具注册、配置 | 内核上下文 | 工具 | 否（薄适配） |
| `api/rest/*` | HTTP 契约 | HTTP | JSON/SSE | 否 |

**硬约束**：`desktop_repeat/` 内不得 import `fastapi`、`harness.api`、`plugins.*`。允许依赖 `harness.infra`（atomic_write、subproc、security）、`harness.modules.artifact_store`（仅路径解析）、`harness.modules.tracing`（可选 span）。这保证 M1 末可原样平移为独立包。

### 2.4 三条数据流

**录制流**

```
用户操作
  → 钩子线程（<1 ms：时间戳/坐标/按键/修饰键）
  → 有界队列（4096）
  → worker：窗口上下文 → UIA 快照（≤300 ms，超时置空）→ 关键帧裁剪
  → 写线程：events.jsonl（追加）+ frames/*.jpg
  → 停止：摘钩子 → flush → manifest 封版 → session.json 状态置 ready
```

**编译流**

```
events.jsonl
  → S1 去噪 → S2 归并 → S3 窗口绑定 → S4 定位链 → S5 等待推断
  → S6 断言建议 → S7 循环检测 → S8 参数建议 → S9 风险标注
  → CompileResult{steps[], warnings[], stats, draft_graph}
  → （人工在画布确认/编辑）→ 发布为工作流版本
```

**回放流**

```
POST /runs
  → 输入租约 → 环境预检（L0–L3 分级）→ 应用就绪检查
  → 逐步：resolve（定位链）→ pre_wait → act（Pattern 优先）→ settle → assert
  → 失败：重试 → 降级 → 放宽 → 挂起人工 → 中止取证
  → 每步 emit SSE + 落 repeat_runs / repeat_step_hits
  → 释放租约 → run_done
```

### 2.5 与既有件的集成

| 既有件 | 用法 | 注意 |
|---|---|---|
| `plugins/computer_use/desktop.py` | 复用 `DesktopController` 作为**坐标级**注入回退；`RealDesktopController.available=False` 时整个面板显示"桌面操控未就绪" | 主路径不走它，走 `executor/actions.py` 的 Pattern 与 `SendInput` |
| `plugins/computer_use/safety.py` | 复用 `SafetyPolicy.evaluate` 的语义（risk 分级 / 白名单 / 审批），在 `safety_gate.py` 内做适配 | 不得绕过；拦截即中止 |
| `harness/modules/workflows` | 产物落 `workflows` 表；`GraphEngine._run_node` 增加 `gui` 分派 | 仅新增一个分支，不改既有节点 |
| `harness/modules/reverse_engineering/progress.py` | SSE 直接复用 `ProgressStore/ProgressReporter` 范式 | 必须保留两个已修坑：回放用 `list(...)` 快照；终止事件立即 return |
| `harness/modules/artifact_store` | bundle 根目录 = `artifacts_dir()/repeat/sessions` | 复用其清理与配额策略 |
| `harness/modules/scheduler` | 新增任务 kind `repeat`，执行前自动干跑 | 干跑失败不盲跑 |
| `harness/modules/skill_manager` | 生成技能写入用户级技能目录，自动 reload | frontmatter 走项目自有极简解析器（不引入 YAML 依赖） |
| `harness/modules/tracing` | span：`repeat.run` / `repeat.step` | 属性含 sid、action、hit_index、elapsed_ms |
| `harness/modules/todo_manager` | 挂起人工介入时创建待办 | 附带诊断包路径 |
| `harness/modules/model_manager` | 仅 §5.4 管线产出之后的语义命名（`interpreter/semantic.py`） | 无 Provider 静默跳过 |
| `harness/infra/database.py` | 新增 3 张表 + 迁移 | `CREATE TABLE IF NOT EXISTS` + `_migrate` 补列 |

### 2.6 架构决策记录（ADR）

| ID | 决策 | 备选 | 理由 |
|---|---|---|---|
| ADR-01 | 录制器独立子进程 | 线程 | 崩溃/延迟/DPI 三重隔离 |
| ADR-02 | 子进程控制用「控制文件 + 心跳文件」 | localhost socket / 命名管道 | 无端口冲突、崩溃可诊断、易调试；150 ms 轮询足够；升级路径明确 |
| ADR-03 | 事件存 JSONL 而非 SQLite | SQLite 单表 | 追加写天然抗崩溃、可 grep、可流式读、git 友好；查询需求由编译期一次性扫描满足 |
| ADR-04 | 以**物理坐标**为准，逻辑坐标按需换算 | 只存逻辑坐标 | Windows PerMonitorV2 下钩子与 UIA 都给物理坐标；逻辑坐标随缩放变化，存储会引入歧义 |
| ADR-05 | 坐标兜底加**环境闸门** | 无条件兜底（v2 原文） | 盲点一次可能造成不可逆误操作；默认拒绝、显式放行更符合"可预测失效" |
| ADR-06 | 回放运行用专用 `repeat_runs` 表 | 复用 `workflow_runs` | 步骤级 attempts/hit/截图字段多，塞进 `steps_json` 无法统计；专用表可聚合命中率 |
| ADR-07 | 关键帧而非视频 | 全程录屏 | 体积差 10–30 倍；帧可直接做锚点；视频无法参与定位 |
| ADR-08 | UIA Pattern 优先，坐标注入兜底 | 一律坐标点击 | Pattern 不抢前台、不受遮挡影响、成功率高 |
| ADR-09 | 文本注入三通道：SetValue → 剪贴板 → Unicode SendInput | 逐字符 VK 键入 | VK 依赖键盘布局且被 IME 劫持；Unicode 注入绕过 IME |
| ADR-10 | 编译产物是工作流，不是私有宏 | 私有步骤文件 | 直接获得画布编辑、版本、导入导出、调度 |

---

## 3. 录制子系统

### 3.1 为什么"屏幕录制"指事件而非视频

需求中的"屏幕录制"在本模块定义为**操作事件录制 + 关键帧取证**。视频不作为回放依据，原因：

1. 视频无法给出可定位的目标，回放只能靠像素比对，界面轻微变化即失效；
2. 10 分钟 1080p 视频约 100–300 MB，而标准事件+帧约 10 MB；
3. 视频无法参数化（"把客户名换成 B 公司"无从表达）；
4. 视频不可审计差异，失败时无法回答"第几步、哪个定位器、为什么"。

可选：M3 提供 2 fps 低码率 MP4 作为**审计附件**（默认关闭），只用于人工复盘，不进入回放链路。

### 3.2 采集流水线

```
[钩子线程]  OS 事件
   ├─ 时间戳：t = monotonic_ns() - t0；ts = time_ns()//1e6
   ├─ 坐标：GetCursorPos → 物理像素（虚拟桌面坐标系，可为负）
   ├─ 按键：pynput key + vk + scan + 修饰键位图
   └─ 入队（容量 4096，满则按 §3.9 背压策略）
        │
[worker 线程]
   ├─ 窗口上下文：GetForegroundWindow / GetWindowRect / GetClientRect
   │              / GetWindowThreadProcessId / QueryFullProcessImageName
   ├─ 事件合成：双击、拖拽、滚轮累计、移动摘要
   ├─ UIA 快照（仅关键事件，300 ms 硬超时）
   ├─ IME 归并（值差分，见 §3.4）
   └─ 关键帧裁剪（pre / post）
        │
[写线程]
   ├─ events.jsonl：紧凑 JSON + '\n'，每 32 条或 200 ms flush
   └─ frames/<seq:06d>-<pre|post>.jpg
```

**钩子线程纪律**：只做 5 件事——读时间、读坐标、读键码、读修饰键、入队。禁止任何系统查询、日志格式化、JSON 序列化。违者会导致钩子超时被系统摘除（表现为"录到一半没事件了"）。

### 3.3 事件类型与采集口径

| type | 触发 | 关键字段（data） | 备注 |
|---|---|---|---|
| `session_start` | 倒计时结束 | `env`、`config` | 携带环境基线 |
| `pointer_down` / `pointer_up` | 按键按下/抬起 | `button`、`modifiers` | 原始事件，编译期可丢弃 |
| `click` | down+up 同点（≤4 px，≤600 ms） | `button`、`count`、`down_up_ms`、`modifiers` | `count` 恒为 1；双击见下一行 |
| `double_click` | 第二次 up 距首次 ≤500 ms 且 ≤4 px | `button`、`modifiers`、`supersedes` | 追加事件，`supersedes` 指向首次 click 的 seq；编译期丢弃被取代的 click |
| `right_click` / `middle_click` | 同上，非左键 | 同上 | 独立类型便于编译 |
| `drag` | down → move* → up，位移 >8 px | `button`、`path`（≤16 采样点）、`dur_ms` | 路径抽稀，不存逐像素 |
| `scroll` | 滚轮事件，400 ms 窗口内累计 | `dx`、`dy`、`notches`、`hwnd_under` | 方向按系统设置归一 |
| `pointer_move` | 仅作为点击前的"接近摘要" | `from`、`to`、`dur_ms`、`samples` | 不逐条记录 |
| `key` | 单键按下/抬起 | `key`、`vk`、`scan`、`down`、`repeat`、`modifiers` | 组合键在编译期合成 `hotkey` |
| `text_input` | IME/粘贴上屏 | `text`、`source`（ime/paste/typed）、`len_delta` | 见 §3.4 |
| `clipboard` | 复制/剪切/粘贴 | `op`；**不含内容**（除非显式开启） | 默认只记"发生过" |
| `window_change` | 前台窗口变化/移动/关闭 | `subtype`、`prev`、`curr` | 编译为 `window_focus` 步骤 |
| `marker` | F10 | `label`、`note` | 用户分段注释 |
| `pause` / `resume` / `discard` | 热键或 API | — | 控制事件 |
| `session_end` | 停止 | `reason`、`counts` | 最后一条 |

**窗口上下文 `win`**（每条关键事件携带）：

```json
{
  "hwnd": 132456,
  "title": "订单管理 - ERP",
  "cls": "Chrome_WidgetWin_1",
  "pid": 12044,
  "exe": "erp.exe",
  "rect": [200, 120, 1400, 900],
  "client": [208, 158, 1392, 862],
  "minimized": false,
  "elevated": false,
  "cloaked": false
}
```

- `exe` 只存文件名，不存完整路径（隐私与可迁移性）；
- `hwnd` 仅作录制期关联，**回放不得依赖 hwnd**（句柄会被复用）；
- `elevated` 由 `GetTokenInformation(TokenElevation)` 对目标进程判断，用于提前告警 UIPI。

### 3.4 输入法归并（IME Aggregator）

问题：低级钩子拿到的是**物理按键**。中文输入时用户敲 `nihao` + 空格 + 候选数字，事件流里是一串拉丁字母；直接回放会打出 `nihao` 而不是"你好"。

方案：**上屏值差分**。

```
前置条件：焦点元素 is_editable && !is_password && UIA 可读 Value/Text
采样时机：最后一次按键后 150 ms 触发一次（防抖），最多 20 Hz
读取内容：ValuePattern.Value 或 TextPattern 全文（限 4 KB）
差分逻辑：
    appended = v_now[len(v_prev):]        # 仅当 v_now 以 v_prev 为前缀
    若 appended 含非 ASCII 或长度与按键序列不匹配：
        发射 text_input{text: appended, source: "ime"}
        并将区间内的 key 事件标记 flags.consumed_by_text = true
    否则保留原始按键（英文直接输入，编译期聚合为 type）
失败降级：读取超时/不支持 → flags.ime_risk = true，编译期该步 needs_review
密码框：永不读取值，只记 key_count 与 flags.masked = true
```

回放侧配合：`type` 动作优先 `ValuePattern.SetValue`；不支持时用剪贴板粘贴；最后才用 `KEYEVENTF_UNICODE` 直接注入字符（不依赖键盘布局、不触发 IME）。因此**只要录制期拿到了正确的上屏文本，回放就不会重现拼音**。

### 3.5 UIA 惰性快照

仅在 `click / right_click / double_click / key(enter|tab|esc) / window_change` 时刻触发。

```
ElementFromPoint(x, y)                    # 物理坐标
  → 取最深元素
  → TreeWalker.GetParent 回溯 ≤ 8 层
  → 抽取最小属性集（下表）
  → 300 ms 硬截止（独立线程 + 取消标志；超时即放弃，不阻塞事件流）
  → 失败/超时：uia = null，flags.uia_timeout = true
```

最小属性集：

| 字段 | 来源 | 用途 |
|---|---|---|
| `control_type` | `ControlType` 程序化名称 | 定位链 `name_type` |
| `name` | `Name` | 定位链 `name_type` |
| `automation_id` | `AutomationId` | 首选定位（最稳） |
| `class_name` | `ClassName` | 老 Win32 应用 |
| `framework_id` | `FrameworkId` | 诊断（WPF/Win32/Chromium） |
| `path` | 祖先链拼接 `Window[标题]/Pane/Document/Edit[3]` | 结构路径定位 |
| `depth` | 祖先层数 | 置信度评估 |
| `is_password` | `IsPassword` | 隐私屏蔽（强制） |
| `is_enabled` / `is_offscreen` | 对应属性 | 可用性判断 |
| `patterns` | 可用 Pattern 列表 | 动作选择（Invoke/Value/Toggle/Scroll） |
| `rect` | `BoundingRectangle` | 元素中心点、锚点裁剪 |
| `ancestors` | 每层 `ControlType:Name`（≤8） | 结构容错匹配 |

**Chromium/Electron 预热**：录制开始时对前台窗口做一次 `ElementFromPoint` 空查询，触发渲染进程构建无障碍树；否则首次真实查询可能达秒级。仍超时则整段降级为图像/坐标定位，并在 `session.json.counters.uia_timeout` 计数，编译期统一提示。

### 3.6 关键帧策略

| 触发 | 内容 | 用途 |
|---|---|---|
| 每个语义事件前 | 目标周围裁剪（含 96 px 上下文） | 生成图像锚点、失败对比 |
| 事件后 400 ms | 同区域裁剪 | 判断是否有界面变化 → 断言/无效点击 |
| `window_change` | 全屏帧（长边归一 1920，JPEG q80） | 窗口级定位与复盘 |
| 运行失败（回放期） | 全屏 + 目标裁剪 | 诊断包 |

三档留证（会话级配置）：

| 档位 | 帧策略 | 10 分钟体积 | 适用 |
|---|---|---|---|
| `minimal` | 不存帧，仅编译期生成锚点 | 0.5–1.5 MB | 隐私敏感、批量流程 |
| `standard`（默认） | 裁剪帧 pre/post + 窗口全帧 | 6–12 MB | 日常 |
| `forensic` | 每个语义事件 pre/post 全屏帧 | 150–300 MB | 审计、排障，7 天自动过期 |

裁剪框计算：`pad = clamp(0.5 * min(w, h), 24, 120)`；裁剪结果最小 64×48，最大 480×360；超出则等比缩放。锚点图从裁剪帧中心再裁 60%（去掉边缘噪声），存 PNG 保真。

### 3.7 覆盖层与操作体验

- **启动**：REST/工具 → Popen recorder → 覆盖层全屏半透明遮罩 **3-2-1 倒计时**（可配 0/3/5 秒）；
- **遮罩不吞事件**：`WS_EX_TRANSPARENT | WS_EX_LAYERED | WS_EX_NOACTIVATE`，保证倒计时期间用户的操作不被拦截，但也不被记录；
- **状态胶囊**：右上角，红点 + 已录时长 + 事件数 + 队列水位；暂停变黄并显示"已暂停"；
- **热键**（可配）：`Ctrl+Alt+R` 开始/结束、`Ctrl+Alt+P` 暂停/恢复（**长按 ≥0.8 s = 长暂停"跳过段落"**，§3.10）、`Ctrl+Alt+X` 丢弃、`F10` 打点；
- **结束**：摘钩子 → flush → 校验（事件数 > 0、无钩子中断告警）→ 写 `session.json` → **呈现结果横幅（成功/失败，§9.5.1，驻留 8 s）** → 退出；
- **提权告警**：录到 `elevated=true` 的窗口时，胶囊显示橙色警示并写 marker，提示"该窗口可能无法回放"。

### 3.8 隐私屏蔽（前置，非事后）

| 面 | 机制 | 落点 |
|---|---|---|
| 密码框 | `is_password=true` 时：不读值、不截该控件区域、只记 `key_count` | `privacy.py` 在 worker 层拦截 |
| 屏蔽窗口 | 会话配置 `mask_windows[]`（标题正则 / 进程名）：命中则整窗不截图、不查 UIA，事件只记 `flags.masked` | 同上 |
| 剪贴板 | 默认只记"发生过复制/粘贴"，不取内容；`capture_clipboard=true` 才取文本（仍受 `mask_keys` 约束） | `capture.py` |
| 按键黑名单 | `mask_keys[]`（如 `ctrl+v`）：该组合键的后续文本不入库 | 同上 |
| 自动暂停 | 会话配置 `auto_pause_mask[]`：前台窗口命中即转 paused（§3.10）——`mask_windows` 是"采了但脱敏"，自动暂停是"根本不采"，两者互补 | `runner.py` + `privacy.py` |
| 路径 | `win.exe` 只存 basename | `windows.py` |

**编译期强制**：含 `masked=true` 或密码框的步骤，一律生成 `manual_confirm` 节点，禁止自动回放（`allow_password_replay` 默认 false）。

### 3.9 性能预算与背压

| 环节 | 预算 | 超限动作 |
|---|---|---|
| 钩子回调 | p99 < 1 ms | 若连续 100 次超 5 ms，写 `hook_slow` 告警 |
| 队列 | 4096 | 满时按序丢弃：`pointer_move` → `scroll` 中间态 → `key` 重复态；**永不丢** click / window_change / marker |
| UIA 查询 | ≤300 ms | 硬超时置空，计 `uia_timeout` |
| 帧裁剪 | ≤60 ms | 跳过该帧，计 `frame_drop` |
| 写线程 | 200 ms flush | 磁盘慢时增大缓冲，不阻塞 worker |
| recorder RSS | <150 MB | 超限触发帧缓存回收 |
| recorder CPU | <5%（i7-8550U 基线） | 超限降采样 move、延长 UIA 超时 |

**崩溃容忍**：最多丢失最后 200 ms 事件；读取端遇到不完整末行即丢弃并置 `truncated=true`，不阻塞后续编译。

### 3.10 暂停与退出（"现在不想被录"的一等能力）

录制中途必然遇到"接下来的操作不该被记录"（输密码、切私人窗口、反悔整个会话）。为此提供三档退出梯度 + 自动兜底：

| 档位 | 触发 | 语义 | 数据效果 | 恢复方式 |
|---|---|---|---|---|
| **暂停** | `Ctrl+Alt+P` / 面板按钮 / API | 暂时不想录，稍后继续 | 暂停区间内**零采集**（见下"强保证"）；再按一次恢复 | 同热键 / 面板 |
| **跳过段落** | 按住 `Ctrl+Alt+P` ≥0.8 s 进入"长暂停" | 明确"这段不要"，黄色胶囊显示"长暂停中" | 同暂停，且结束横幅中明示"已排除 X 段 / 共 Y 秒" | 同热键 |
| **丢弃** | `Ctrl+Alt+X` / 面板 | 整个会话不要了 | 二次确认 → `status=discarded`、bundle 软删（30 天可回收，见 §4.7） | 不可恢复，重新开始 |
| **自动暂停** | 前台窗口命中会话配置 `auto_pause_mask[]`（正则） | 隐私兜底：进入敏感窗口即停 | 胶囊变黄 + 横幅"已进入敏感窗口，自动暂停"；**不自动恢复**（防抖动），等用户手动 resume 或结束 | 手动 |

**强保证（采集层短路，非"记了再删"）**：

1. 暂停指令经控制文件生效（≤150 ms），worker/写线程丢弃当前积压的未入库事件；
2. 生效同时，钩子回调切到"只响应恢复/丢弃热键"的直通模式——**暂停期间的鼠标键盘事件不入队、不截图、不查 UIA**，即数据从未产生，而非落盘后删除；这是对密码等敏感场景成立的关键前提（与 §3.8 前置屏蔽同一原则）；
3. 热键生效前 ≤150 ms 的微小窗口内事件可能已入队：worker 收到 pause 生效时刻前、时间戳晚于 `control.pause_at - 150ms` 的未写事件一并丢弃（宁可少录，不可多录）；
4. `pause` / `resume` 控制事件本身入库（`src=system`），记录区间边界与时长——这是 S1/S5 正确扣除暂停区间的依据；
5. 暂停期间的 `marker`（F10）保留，用于"从这里开始继续"的分段注记。

**状态机与编译管线联动**：录制状态机见 §5.1 T07/T08/T10/T11/T12；编译期 S1 丢弃暂停区间事件，**S5 等待推断的 gap 必须扣除暂停时长**（见 §5.4 注 ①），避免把 30 秒暂停误判成"页面加载慢"而插入无谓的 `pre_wait`；S7 循环检测的连续性判断同样以"有效时间轴"（扣除暂停）为准。

**无人值守互斥**：回放运行期间 `Ctrl+Alt+P` 无意义（不持有 recorder）；紧急停止仍为 `Ctrl+Alt+.`（§5.2 P17）。

---

## 4. 数据格式与存储策略

### 4.1 Bundle 目录

```
<artifacts>/repeat/sessions/<session_id>/
├── session.json           # 元信息 + 环境基线 + 配置 + 计数 + 完整性
├── events.jsonl           # 原始事件流（追加写，唯一真相）
├── frames/
│   ├── 000143-pre.jpg
│   ├── 000143-post.jpg
│   └── 000160-full.jpg
├── anchors/               # 编译期产物（图像锚点小图）
│   └── s07.png
├── manifest.json          # 全部文件 {path,size,sha256} + root_sha256
└── runtime/               # 瞬时文件，不入清单
    ├── control.json       # 主进程 → recorder 指令
    ├── heartbeat.json     # recorder → 主进程心跳
    └── recorder.log
```

`session_id` 格式：`r-YYYYMMDD-HHMMSS-<6hex>`（可按时间排序）。

### 4.2 版本与兼容策略

| 载体 | 版本字段 | 当前值 | 兼容规则 |
|---|---|---|---|
| 事件行 | `v` | 1 | 读取端支持 `[1]`；遇到更大值拒绝并报 RP-6002 |
| session.json | `schema` | 1 | 同上，迁移器逐级升版 |
| manifest.json | `schema` | 1 | 同上 |
| 步骤 / 工作流节点 | `step_schema` | 1 | 工作流 `data.step_schema` 缺省视为 1 |
| SKILL.md | frontmatter `metadata.workflow_version` | 整数 | 技能绑定具体版本号 |

演进规则：

1. **加字段不升版**：读取端忽略未知字段，写入端统一放进 `ext` 袋子保留；
2. **改语义/删字段升版**：必须提供 `migrate_N_to_N+1`，读取时自动升级并回写 `.migrated` 副本；
3. **读取端窗口**：只保证 `current` 与 `current-1`；更旧版本提示"请先用旧版打开另存"。

### 4.3 事件 Schema（events.jsonl 单行）

| 字段 | 类型 | 必填 | 约束 | 说明 |
|---|---|---|---|---|
| `v` | int | 是 | =1 | Schema 版本 |
| `seq` | int | 是 | 从 0 单调 +1 | 单写者保证无空洞 |
| `t` | int | 是 | ≥0，单调不减 | 会话内毫秒（monotonic），**回放节拍唯一依据** |
| `ts` | int | 是 | epoch ms | 与日志/截图对时 |
| `type` | string | 是 | 见 §3.3 | 事件类型 |
| `src` | string | 是 | hook/derived/marker/system | 来源 |
| `pos` | object\|null | 否 | 见下 | 坐标（物理空间） |
| `win` | object\|null | 否 | 见 §3.3 | 窗口上下文 |
| `uia` | object\|null | 否 | 见 §3.5 | 语义快照 |
| `data` | object | 是 | 按 type | 类型专属负载 |
| `flags` | object | 否 | bool 值 | `uia_timeout` / `masked` / `ime_risk` / `consumed_by_text` / `synthetic` |

`pos` 结构：

```json
{
  "x": 812, "y": 377,
  "space": "phys",
  "monitor": 1,
  "scale": 1.25,
  "rel": [0.432, 0.351]
}
```

- `x/y`：虚拟桌面物理像素（多显示器可为负）；
- `space`：预留 `logical`，当前恒为 `phys`；
- `monitor`：显示器序号（1 基）；
- `scale`：该显示器录制时的有效缩放（`dpi/96`）；
- `rel`：相对目标窗口**客户区**的归一化比例（0–1），用于窗口移动后的相对定位。

事件示例（带 UIA 的点击）：

```json
{"v":1,"seq":143,"t":182340,"ts":1760790315234,"type":"click","src":"hook",
 "pos":{"x":812,"y":377,"space":"phys","monitor":1,"scale":1.25,"rel":[0.432,0.351]},
 "win":{"hwnd":132456,"title":"订单管理 - ERP","cls":"Chrome_WidgetWin_1","pid":12044,
        "exe":"erp.exe","rect":[200,120,1400,900],"client":[208,158,1392,862],
        "minimized":false,"elevated":false,"cloaked":false},
 "uia":{"control_type":"Edit","name":"客户名称","automation_id":"txtCustName",
        "class_name":"Chrome_RenderWidgetHostHWND","framework_id":"Chromium",
        "path":"Window[订单管理]/Pane/Document/Edit[3]","depth":6,
        "is_password":false,"is_enabled":true,"is_offscreen":false,
        "patterns":["ValuePattern"],"rect":[700,360,980,398],
        "ancestors":["Window:订单管理 - ERP","Pane:","Document:","Edit:客户名称"]},
 "data":{"button":"left","count":1,"down_up_ms":88,"modifiers":[]},
 "flags":{}}
```

### 4.4 session.json

```json
{
  "schema": 1,
  "session_id": "r-20261018-204512-a1b2c3",
  "status": "ready",
  "created_at": "2026-10-18T20:45:12+08:00",
  "ended_at": "2026-10-18T20:47:03+08:00",
  "label": "ERP 每日订单导出",
  "operator": {"host": "DESKTOP-8F2K", "user": "Administrator", "elevated": false},
  "env": {
    "os": "Windows 10 22H2 19045",
    "session_id": 1,
    "input_desktop": "Default",
    "virtual_screen": [-1920, 0, 3840, 1080],
    "monitors": [
      {"index": 1, "name": "\\\\.\\DISPLAY1", "primary": true,
       "rect": [0, 0, 1920, 1080], "work": [0, 0, 1920, 1040],
       "dpi": 120, "scale": 1.25}
    ]
  },
  "config": {
    "frame_tier": "standard",
    "countdown_s": 3,
    "uia_enabled": true,
    "uia_timeout_ms": 300,
    "capture_clipboard": false,
    "mask_windows": [],
    "mask_keys": [],
    "auto_pause_mask": ["\\\\微信\\\\|个人微信", "banking\\..*\\.exe"],
    "hotkeys": {"toggle": "ctrl+alt+r", "pause": "ctrl+alt+p", "discard": "ctrl+alt+x", "marker": "f10"}
  },
  "counters": {"events": 1043, "clicks": 87, "keys": 612, "frames": 121, "drops": 0, "uia_timeouts": 2},
  "integrity": {"events_sha256": "…", "manifest_sha256": "…", "truncated": false},
  "error": null
}
```

`status` 为**持久化状态**，只有四个取值：`draft`（录制中或未封版）/ `ready` / `failed` / `discarded`。运行期的 `arming`/`countdown`/`recording`/`paused`/`finalizing`（§5.1）属于内存态，通过心跳文件 `runtime/heartbeat.json` 暴露，避免高频改写 `session.json`。

### 4.5 步骤与工作流节点

步骤是唯一执行契约；工作流 `gui` 节点是它的投影（`data` 即步骤体）。

```json
{
  "sid": "s07",
  "step_schema": 1,
  "action": "type",
  "label": "在【客户名称】填入 {{param.cust_name}}",
  "target": {
    "scope": "window",
    "app_hint": {"exe": "erp.exe", "window_title_regex": "订单管理.*", "cls": "Chrome_WidgetWin_1"},
    "locators": [
      {"kind": "uia", "by": "automation_id", "value": "txtCustName", "timeout_ms": 3000, "confidence": 0.95},
      {"kind": "uia", "by": "name_type", "value": {"name": "客户名称", "control_type": "Edit"}, "timeout_ms": 2000, "confidence": 0.82},
      {"kind": "uia", "by": "structure_path", "value": "Window[订单管理]/Pane/Document/Edit[3]", "timeout_ms": 2000, "confidence": 0.62},
      {"kind": "image", "anchor_ref": "anchors/s07.png", "threshold": 0.82, "scales": [0.8, 0.9, 1.0, 1.1, 1.25], "confidence": 0.55},
      {"kind": "relative", "anchor": "window", "ratio": [0.432, 0.351], "confidence": 0.45},
      {"kind": "coord", "pos": [812, 377], "scale": 1.25, "screen": [1920, 1080], "monitor": 1, "confidence": 0.25}
    ]
  },
  "params": {"mode": "clipboard_paste", "value": "{{param.cust_name}}"},
  "timing": {"pre_wait": {"kind": "element_appear", "locator_index": 0, "timeout_ms": 8000},
             "settle_ms": 150, "step_timeout_ms": 12000},
  "assertion": {"kind": "value_equals", "locator_index": 0, "value": "{{param.cust_name}}", "optional": true},
  "risk": "write",
  "policy": {"on_fail": "ladder", "retries": 2, "backoff_ms": 800, "idempotent": true},
  "evidence": {"event_range": [141, 156], "anchor": "anchors/s07.png", "frames": ["000141-pre.jpg"]},
  "meta": {"confidence": 0.91, "needs_review": false, "hit_stats": {"runs": 12, "primary": 11}},
  "ext": {}
}
```

`action` 允许值：`click`、`double_click`、`right_click`、`type`、`hotkey`、`scroll`、`drag`、`window_focus`、`wait_window`、`wait_element`、`assert_element`、`assert_text`、`manual_confirm`、`marker`、`launch_app`。

### 4.6 manifest 与完整性

```json
{
  "schema": 1,
  "session_id": "r-20261018-204512-a1b2c3",
  "created_at": "2026-10-18T20:47:04+08:00",
  "files": [
    {"path": "session.json", "size": 2140, "sha256": "…"},
    {"path": "events.jsonl", "size": 412033, "sha256": "…"},
    {"path": "frames/000143-pre.jpg", "size": 48211, "sha256": "…"}
  ],
  "root_sha256": "…"
}
```

`root_sha256 = sha256(concat(sorted("path:sha256\n")))`。校验时机：编译前、回放前、技能导出前。任一不符 → RP-6001，拒绝编译/回放。

**原子写**：`session.json` 与 `manifest.json` 一律写 `*.tmp` + `os.replace`；`events.jsonl` 只追加。

### 4.7 容量估算与保留

| 项目 | 单件 | 10 分钟典型量 | 体积 |
|---|---|---|---|
| click/drag/scroll 事件 | 0.7–1.2 KB | 120 | 0.12 MB |
| key 事件 | 0.2–0.3 KB | 800 | 0.20 MB |
| 其他事件 | 0.2 KB | 120 | 0.02 MB |
| 裁剪帧（standard） | 40–60 KB | 120 | 6.0 MB |
| 窗口全帧 | 180–350 KB | 10 | 2.6 MB |
| 锚点（编译期） | 8–20 KB | 40 | 0.6 MB |
| **合计** | | | **≈ 9 MB** |

保留策略：

| 对象 | 默认保留 | 触发 |
|---|---|---|
| `forensic` 帧 | 7 天 | 定时 GC |
| `standard` 帧 | 30 天 | 定时 GC（事件与锚点保留） |
| `events.jsonl` / `session.json` | 180 天 | 超过提示归档 |
| 单 bundle 上限 | 500 MB | 达到即停止存帧并告警 |

GC 由 `scheduler` 每日执行，只删 `frames/`，不动事件与清单（清单同步更新并重算 `root_sha256`，写入 `integrity.gc_at`）。

---

## 5. 核心流程与状态机

### 5.1 录制状态机

```
idle ──start──► arming ──ready──► countdown ──3s──► recording ⇄ paused
arming ──error──► failed                            （pause / resume）
countdown ──abort──► discarded
recording ──stop──────────► finalizing ──verify_ok──► ready
paused ──stop────────────► finalizing ──verify_fail─► failed
paused ──discard─────────► discarded
recording ──heartbeat_lost──► failed
```

转移表（权威）：

| # | 当前 | 触发 | 守卫 | 目标 | 副作用 |
|---|---|---|---|---|---|
| T01 | idle | `start` | 依赖可用 && 无活跃会话 && 输入租约可获取 | arming | 创建 bundle、写 `session.json(draft)`、Popen recorder、呈现首次授权说明（仅首次） |
| T02 | arming | `recorder_ready` | 钩子安装成功 && 覆盖层就绪 && DPI 声明成功 | countdown | 启动倒计时、启动心跳监视 |
| T03 | arming | `recorder_error` | — | failed | 写 error、保留 `recorder.log`、清理进程 |
| T04 | arming | `abort` | — | discarded | 杀进程、软删 bundle |
| T05 | countdown | `countdown_done` | — | recording | 写 `session_start`（环境基线）、胶囊转红、开始接受事件 |
| T06 | countdown | `abort` | — | discarded | 同上 |
| T07 | recording | `pause` | 热键（短按）/ API / `auto_pause_mask` 命中 | paused | 写 `pause`（含生效时刻）、胶囊转黄、钩子切直通模式（区间内零采集，§3.10） |
| T08 | recording | `stop` | 热键或 API | finalizing | 摘钩子、写 `session_end`、flush |
| T09 | recording | `heartbeat_lost` | 心跳 >2 s 未更新 | failed | 标记 `truncated`、保留已落盘数据 |
| T10 | paused | `resume` | 热键或 API；`auto_pause` 后**不自动恢复** | recording | 写 `resume`、胶囊转红、钩子恢复采集 |
| T11 | paused | `stop` | — | finalizing | 同 T08 |
| T12 | paused | `discard` | — | discarded | 软删 bundle |
| T13 | finalizing | `verify_ok` | 事件数 >0 && 清单可生成 && 无未 flush 缓冲 | ready | 写 manifest、`status=ready`、**呈现结果横幅（§9.5.1）**、退出子进程 |
| T14 | finalizing | `verify_fail` | — | failed | 保留证据、写诊断、**呈现错误横幅**、退出 |
| T15 | failed / ready / discarded | — | 终态 | — | 释放租约、回收子进程、广播状态 |

**不变式**

- **INV-R1** `seq` 严格 +1，无空洞（单写线程保证）；
- **INV-R2** `recording` 之外不得产生输入类事件；
- **INV-R3** `paused` 期间只允许 `marker` / `system` 事件；
- **INV-R4** 同一 bundle 只有一个写者（文件锁 + pid 校验，第二个写者直接报错）；
- **INV-R5** `finalizing` 之后 `events.jsonl` 只读。

### 5.2 回放状态机

```
pending ──start──► precheck
precheck ──ok──────────────► running ──step_ok───► running（进入下一步）
precheck ──incompatible────► suspended / failed
precheck ──dry_run─────────► dry_run ──finished─► succeeded / failed
running  ──step_fail───────► retrying ──预算内──► running
running  ──need_human──────► suspended ──resume─► resuming ──► running
running  ──all_done────────► succeeded
running  ──fatal───────────► failed
任意非终态 ──abort─────────► aborted
```

转移表：

| # | 当前 | 触发 | 守卫 | 目标 | 副作用 |
|---|---|---|---|---|---|
| P01 | pending | `start` | 取得输入租约 | precheck | 写 run 记录、发 `run_started`、**呈现执行前告知横幅（§9.5.2；无人值守时跳过）** |
| P02 | pending | `abort` | — | aborted | 释放租约 |
| P03 | precheck | `env_ok` | 级别 ∈ {L0,L1,L2} && 目标应用就绪 | running | 记录 env 快照、发 `precheck_done` |
| P04 | precheck | `env_degraded` | 级别 = L3 且策略 = suspend | suspended | 发 `run_suspended` + 原因 |
| P05 | precheck | `env_incompatible` | 缺应用且无 `launch` 且策略 = abort | failed | 写 RP-1002/1003 |
| P06 | precheck | `dry_run` | — | dry_run | 只解析不注入 |
| P07 | running | `step_ok` | 还有步骤 | running | 写步骤记录、发 `step_done` |
| P08 | running | `step_fail_retryable` | 重试预算未尽 && 策略 = ladder | retrying | 退避、发 `step_retry` |
| P09 | retrying | `retry` | — | running | 递增 attempt、可降级定位器 |
| P10 | running | `step_fail_human` | 策略 = suspend_manual 或预算耗尽 | suspended | 生成诊断包、创建待办、发 `run_suspended` |
| P11 | suspended | `resume(retry)` | 人工确认 | resuming | 重跑当前步 |
| P12 | suspended | `resume(skip)` | 步骤 `idempotent` 或人工显式允许 | resuming | 标记 skipped，前进 |
| P13 | suspended | `abort` | — | aborted | 留证、释放租约 |
| P14 | resuming | `continue` | 轻量环境复检通过 | running | 从断点继续 |
| P15 | running | `all_done` | 末步成功 && 断言通过 | succeeded | 汇总统计、发 `run_done`、**呈现完成回执（横幅/通知，无人值守必发通知）** |
| P16 | running | `fatal` | 不可恢复 | failed | 诊断包、发 `run_error`、**呈现失败回执** |
| P17 | running / retrying / suspended | `emergency_stop` | — | aborted | 立即停注入、留证、RP-7001 |
| P18 | dry_run | `finished` | — | succeeded / failed | 输出体检报告 |

**不变式**

- **INV-P1** 全局同一时刻只有一个 run 持有输入租约（命名互斥体 `Global\andy_repeat_input`）；
- **INV-P2** 任一时刻至多一个步骤处于非终态；
- **INV-P3** 状态图无环，唯一回路是 `suspended → resuming → running`；
- **INV-P4** 终止态之后不再发业务 SSE 事件（只允许一次 `run_done`/`run_error`）；
- **INV-P5** 每次进入 `running` 前必须已完成环境预检（初次全量，恢复时轻量）。

### 5.3 步骤微状态机

```
resolve ──hit──► pre_wait ──ready──► act ──sent──► settle ──stable──► assert ──pass──► done
   │                 │                 │              │                  │
   └──miss───────────┴──timeout────────┴──error───────┴──unstable────────┴──fail──► failed
```

| 阶段 | 超时（默认） | 失败错误码 | 说明 |
|---|---|---|---|
| `resolve` | 链内各定位器 timeout 之和上限 6 s | RP-2001 | 逐个尝试，记录 attempts |
| `pre_wait` | 8 s | RP-3001 | 元素出现/窗口出现/界面稳定 |
| `act` | 3 s | RP-5001/5003 | 注入动作 |
| `settle` | 2 s | RP-3004 | 界面稳定或值变化到位 |
| `assert` | 1 s | RP-3002 | 可选手动断言 |

### 5.4 编译管线（事件流 → 步骤）

| 阶段 | 输入 | 规则 | 输出 | 失败降级 |
|---|---|---|---|---|
| S1 去噪 | 原始事件 | 丢 `pointer_move`（保留点击前 500 ms 摘要）；丢组合键中的单修饰键；**按 pause/resume 边界扣除暂停区间**（正常情况下区间内本无事件，此处为兜底）；合并重复 `key_down` | 清洗事件 | 不失败 |
| S2 归并 | 清洗事件 | key 段 → `type`（边界：焦点变化/非键事件/静默 >1200 ms）；down+up → `click`；`double_click` 按 `supersedes` 取代首次 click；位移 >8 px → `drag`；400 ms 内滚轮 → `scroll` | 语义事件 | 不失败 |
| S3 窗口绑定 | 语义事件 | 前台窗口变化 → `window_focus`；跨进程自动分组 `app_hint` | 带窗口归属的事件 | 缺窗口信息则 `scope=desktop` |
| S4 定位链 | 语义事件 + UIA | 按 §6.1 打分装配，得分 ≥0.35 入链；`uia=null` 只保留 image/relative/coord | locator 列表 | 无任何定位器 → 整步 `needs_review` |
| S5 等待推断 | 事件间隔 + 帧差分 | 间隔 >1500 ms → 插入 `pre_wait`，`min_delay = min(gap×0.6, 5000)`；post 帧与 pre 帧差异 >2% → `wait_idle` | timing | 无帧则用固定 gap |
| S6 断言建议 | post/pre 帧 + 值变化 | 点击后无任何变化 → `suspect_noop` 警告；输入后值变化 → `assert_value`（默认 optional） | assertion + warnings | 跳过 |
| S7 循环检测 | 连续子序列 | ≥3 次「同 path 前缀 + 索引递增」→ 标记 `loop_candidate`，给参数化建议 | meta.loop | 跳过 |
| S8 参数建议 | `type` 文本 | 命中日期/数字/金额/邮箱/手机号/路径/URL 正则 → 建议提升为参数，**录制值填入 `default`**（参数体系与输入通道见 §6.6） | params 候选 | 跳过 |
| S9 风险标注 | 目标名 + 窗口标题 + 动作 | 命中危险词库 → risk 升级、`on_fail=suspend_manual`、`needs_review=true` | risk | 默认 write |

> **注 ①（有效时间轴）**：S5 的 gap 与 S7 的连续性判断一律使用**有效时间轴** `t_eff = t - Σ已结束的暂停区间`——30 秒人工暂停不应被推断成"页面加载慢"而插入无谓的 `pre_wait`，也不应打断循环检测。暂停区间由 `pause`/`resume` 控制事件的边界还原（§3.10）。

危险词库（可配）：`删除|移除|清空|提交|确认支付|支付|转账|发送|发布|卸载|格式化|覆盖|重置|注销|退出登录|关闭`。

**输出 `CompileResult`**：

```python
@dataclass(slots=True)
class CompileResult:
    session_id: str
    steps: list[StepDef]
    warnings: list[Warning]        # code + sid + message + severity
    stats: CompileStats            # 步数/低置信数/循环候选/参数候选/耗时
    draft_graph: dict[str, Any]    # 可直接存 workflows 的图
```

低置信步骤（`uia=null`、锚点过小、循环/参数推断）在画布上高亮。**"明确指出哪里需要改"优先于"全自动"**。

### 5.5 端到端时序（回放）

```
前端/Agent   Service       Runner        Locator      Action      SSE
   │ POST /runs │             │              │            │          │
   │───────────►│ 取租约      │              │            │          │
   │            │────────────►│ precheck     │            │          │
   │            │             │──env/应用───►│            │          │
   │            │             │◄──ok─────────│            │          │
   │            │             │ step 1..n    │            │          │
   │            │             │──resolve────►│            │          │
   │            │             │◄──Hit────────│            │          │
   │            │             │──act──────────────────────►│          │
   │            │             │◄──result──────────────────│          │
   │            │             │──settle/assert            │          │
   │            │             │──emit────────────────────────────────►│
   │◄──SSE──────┤◄────────────│              │            │          │
   │            │             │ 全部完成      │            │          │
   │            │◄──RunRecord─│              │            │          │
   │◄──run_done─┤             │              │            │          │
```

### 5.6 并发与互斥

| 资源 | 机制 | 冲突行为 |
|---|---|---|
| 录制会话 | 进程内字典 + bundle 文件锁 | 第二次 start 返回 RP-5004 |
| 回放运行 | 命名互斥体 + 进程内 `asyncio.Lock` | 第二次 run 排队或直接拒绝（可配） |
| 输入设备 | 同一互斥体覆盖录制与回放 | 录制中禁止回放，反之亦然 |
| 工作流定义 | 版本快照（`graph_sha256`） | 运行中定义被改不影响本次运行 |
| bundle 写 | 单写者文件锁 | 第二个写者拒绝 |

---

## 6. 定位与执行引擎

### 6.1 定位策略链

**契约**：

```python
@dataclass(slots=True)
class Hit:
    rect: tuple[int, int, int, int]
    center: tuple[int, int]
    element: Any | None          # UIA 元素引用（可能为 None）
    kind: str
    score: float
    elapsed_ms: int
    diagnostics: dict[str, Any]

@dataclass(slots=True)
class Miss:
    kind: str
    reason: str                  # not_found / timeout / disabled / occluded
    elapsed_ms: int
    diagnostics: dict[str, Any]

class Locator(Protocol):
    kind: str
    def find(self, ctx: ExecContext, spec: LocatorSpec) -> Hit | Miss: ...
    def describe_failure(self, miss: Miss) -> str: ...
```

**注册表**：

```python
_LOCATORS: dict[str, type[Locator]] = {}

def register_locator(kind: str):
    def deco(cls: type[Locator]) -> type[Locator]:
        if kind in _LOCATORS:
            raise ValueError(f"locator 重复注册: {kind}")
        cls.kind = kind
        _LOCATORS[kind] = cls
        return cls
    return deco
```

**打分规则**（编译期装配链时使用）：`confidence = clamp(基准分 + 加成, 0, 1)`，同一策略的多条加成可叠加，但不超过 1.0。

| 优先级 | kind / by | 基准分 | 加成条件 | 加成 | 示例结果 |
|---|---|---|---|---|---|
| 1 | `uia / automation_id` | 0.85 | 非空且非纯数字/GUID；`name` 亦非空 | +0.10 | 0.95 |
| 2 | `uia / name_type` | 0.70 | name 非空且非通用词；control_type 明确 | +0.12 | 0.82 |
| 3 | `uia / structure_path` | 0.50 | 深度 ≥3 且祖先名稳定（兄弟序号容差 ±1） | +0.12 | 0.62 |
| 4 | `uia / class_index` | 0.45 | 老 Win32 控件，class 稳定 | +0.10 | 0.55 |
| 5 | `image` | 0.40 | 锚点尺寸 ≥64×48 且纹理丰富 | +0.15 | 0.55 |
| 6 | `relative` | 0.40 | 有稳定窗口锚且客户区尺寸与录制时偏差 <50% | +0.05 | 0.45 |
| 7 | `coord` | 0.25 | 恒存在（兜底项） | — | 0.25 |

链装配：按得分降序，取 ≥0.35 者；`relative` 与 `coord` 始终追加在尾部（作为降级项）。`uia=null` 时整步 confidence ≤0.5 且强制 `needs_review=true`。

### 6.2 坐标模型与换算

三个空间：

| 空间 | 定义 | 用途 |
|---|---|---|
| `phys` | 虚拟桌面物理像素，原点为主屏左上，副屏可为负 | 存储与注入的唯一真值 |
| `logical` | 每显示器 `phys / scale` | 跨缩放迁移 |
| `rel` | 相对目标窗口客户区的 0–1 比例 | 窗口移动/缩放后定位 |

换算：

```
logical_x = (phys_x - monitor.left) / scale
phys_x'   = logical_x * scale' + monitor'.left
rel_x     = (phys_x - client.left) / client.width
phys_x''  = client'.left + rel_x * client'.width
```

**注入坐标归一化**（`SendInput` 绝对模式）：

```
nx = round((x - vleft) * 65535 / (vwidth  - 1))
ny = round((y - vtop ) * 65535 / (vheight - 1))
flags = MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK
```

**环境漂移分级**：

```python
def classify_env(base: Env, cur: Env) -> str:
    if [m.rect for m in base.monitors] != [m.rect for m in cur.monitors]:
        if same_aspect_and_count(base, cur):
            return "L2"          # 分辨率变但比例同
        return "L3"              # 拓扑/比例变
    if any(abs(b.scale - c.scale) > 1e-6 for b, c in zip(base.monitors, cur.monitors)):
        if logical_geometry_equal(base, cur):
            return "L1"          # 缩放变但逻辑分辨率同
        return "L2"
    return "L0"
```

| 级别 | 含义 | coord | relative | image | UIA |
|---|---|---|---|---|---|
| L0 | 完全一致 | 可用 | 可用 | 可用 | 可用 |
| L1 | 逻辑分辨率一致，缩放不同 | 换算后可用 | 可用 | 可用 | 可用 |
| L2 | 同比例分辨率变化 | **禁用** | 可用 | 多尺度可用 | 可用 |
| L3 | 拓扑/比例变化 | **禁用** | 降权（仅同尺寸窗口） | 多尺度可用 | 可用 |

**兜底闸门**：`coord_allowed = level in {L0, L1}`。L2/L3 下若定位链走到 `coord`，不再点击，直接 RP-2006 并给出"环境已变化，建议重新录制或补充图像锚点"。显式传入 `allow_blind_coord=true` 才放行，且运行记录标记 `blind=true` 计入审计。

### 6.3 动作实现矩阵

| action | 主实现 | 二级回退 | 末级回退 |
|---|---|---|---|
| `click` | `InvokePattern` | `TogglePattern` / `SelectionItemPattern` / `LegacyIAccessible.DoDefaultAction` | 元素中心 `SendInput` 点击 |
| `double_click` | 元素中心 `SendInput` 双击（Pattern 无双击语义） | — | 记录坐标双击 |
| `right_click` | 元素中心 `SendInput` 右键 | — | — |
| `type` | `ValuePattern.SetValue`（非只读） | 剪贴板粘贴（Ctrl+V，自动恢复原剪贴板） | `KEYEVENTF_UNICODE` 逐字符注入 |
| `hotkey` | `SendInput` VK 序列（按下逆序释放） | pyautogui.hotkey | — |
| `scroll` | 元素中心滚轮（WHEEL_DELTA 累计） | `ScrollPattern.Scroll` | 窗口中心滚轮 |
| `drag` | `SendInput` down → 插值 move → up | pyautogui.dragTo | — |
| `window_focus` | `SetForegroundWindow` + 激活校验 | `AttachThreadInput` + ALT 键技巧 + 重试 | 点击标题栏坐标 |
| `wait_window` | 250 ms 轮询 `EnumWindows`（pid + 标题正则） | 按 class 匹配 | 超时 RP-1003 |
| `wait_element` | 100 ms 轮询定位链 | — | 超时 RP-3001 |
| `assert_*` | UIA 属性 / 值 / 文本 | 图像差分 | OCR（M2 可选） |
| `manual_confirm` | 挂起等待 API 确认 | — | 超时按 `on_fail` |
| `launch_app` | `CreateProcess`（仅录制时显式授权） | — | RP-1002 |

**注入安全细节**：

- `SendInput` 返回值为实际插入事件数；返回 0 且 `GetLastError()==5` → UIPI 拦截，报 RP-4001；
- 所有注入事件带 `LLMHF_INJECTED`/`LLKHF_INJECTED` 标记，executor 据此区分"自己的输入"与"人的输入"（见 §7.3 场景 12）；
- 文本注入使用 UTF-16 code unit，代理对（emoji）拆两次 `KEYEVENTF_UNICODE`；
- 密码框：`allow_password_replay=false` 时一律走 `manual_confirm`。

### 6.4 节拍、等待与超时

| 参数 | 默认 | 说明 |
|---|---|---|
| 执行前告知 | 横幅 + 3 s 准备 | 进入首步前呈现"即将自动执行，请勿操作鼠标键盘；紧急停止 Ctrl+Alt+."（§9.5.2；无人值守跳过） |
| 步骤间基础间隔 | 300 ms | 上一目标命中且界面稳定则提前结束 |
| 稳定判定 | 帧差异 <0.2%，连续 2 次（间隔 100 ms） | 复用录制期的差分算法 |
| 动作后 settle | click 150 / type 值匹配 / hotkey 120 / drag 250 ms | 按动作类型 |
| 步骤超时 | 12 s | 可由 `timing.step_timeout_ms` 覆盖 |
| 运行超时 | 30 min | 超出即 abort |
| 应用无响应 | 5 s | `SendMessageTimeout(WM_NULL)` 探活 |
| 干跑单步预算 | 6 s | 只解析 |

三级超时职责：**步骤**兜住单点卡死；**运行**兜住整体漂移；**应用探活**识别目标挂死（与超时区分：挂死报 RP-1006，超时报 RP-3003）。

### 6.5 断言

| kind | 判据 | 默认启用 |
|---|---|---|
| `element_exists` | 定位链命中且 `is_offscreen=false` | 是（隐式） |
| `element_absent` | 定位链全部 miss | 否 |
| `value_equals` / `value_regex` | `ValuePattern.Value` 比对 | 输入类步骤为 optional |
| `text_contains` | UIA Name / 文本，OCR 兜底 | 否 |
| `window_title_matches` | 正则匹配前台窗口标题 | 切换窗口后 |
| `image_similar` | 目标区域 SSIM ≥0.98 或像素差 <2% | 否 |

断言失败不改变已发生的副作用，按 `policy.on_fail` 处理；`optional=true` 的断言失败只记警告不中断。

### 6.6 回放参数：定义、用户输入与解析

设计原则：**录制时输入的值只是"默认值"，不是"最终答案"**。编译期 S8 把命中日期/数字/邮箱/路径等模式（以及用户手工圈定的输入）提升为参数；发布期由用户决定每个参数的"回放时输入方式"；**无人值守运行的前提是每个参数都能静态解析**。用户问"回放时能不能把录制里的 `a` 换成 `b`"——答案是本节，四条通道全部覆盖。

#### 6.6.1 参数定义（start 节点 `data.params[]`）

```json
{
  "id": "n0", "type": "start",
  "data": {
    "params": [
      {"name": "cust_name", "type": "string", "required": true,
       "default": "ACME", "description": "客户名称",
       "sensitive": false, "source": "literal", "pattern": "^.{1,50}$"},
      {"name": "report_date", "type": "string", "required": false,
       "source": "derived", "expression": "today('YYYY-MM-DD')",
       "description": "筛选日期，默认今天"},
      {"name": "login_pwd", "type": "secret", "required": true,
       "source": "runtime_only", "sensitive": true,
       "description": "登录口令，每次人工输入"}
    ]
  }
}
```

| 字段 | 规则 |
|---|---|
| `source` | `literal`（常量/默认值）/ `derived`（运行期表达式求值，供定时任务，仅白名单函数 `today/now/uuid/env`）/ `runtime_only`（每次回放必须提供；敏感参数**强制**此值） |
| `default` | 编译期自动填入录制值——这是"录制即示范"与"参数化"的衔接点；用户可在画布上清空 |
| `sensitive=true` | 值不回显、不进任何日志与运行记录（`vars_json` 存 `***`）、不进 SSE 载荷、不进审计事件；只允许运行时注入 |
| `pattern` | 可选正则；**前置校验**用（见 6.6.4），避免第 30 步才发现格式错 |

步骤 DSL 里以 `{{param.x}}` 占位（§4.5），**固化在工作流定义中的只有占位符，永远不是明文值**。

#### 6.6.2 取值解析优先级

```
显式传入（API/工具/CLI --params）
  > derived 表达式求值
  > start default
  > 交互获取（仅有人值守时存在这一级）
```

任一 required 参数走完前四级仍无值 → RP-8001（拒绝运行）；值不过 `pattern`/类型校验 → RP-8002；`unattended=true` 且存在 `runtime_only` 未注入 → **RP-8003 直接不启动并发通知**（不挂起——挂起会占住调度队列与输入租约）。

#### 6.6.3 用户输入通道（四条 + 步骤内人工输入）

| 通道 | 时机 | 形态 |
|---|---|---|
| 前端运行对话框 | `POST /runs` 之前 | 按 `start.params` 渲染表单：必填标记/默认值/描述/格式提示；`secret` 字段用密码框且禁用"记住"；非敏感值可"记住上次输入"（本地草稿，`repeat.params_save_last`） |
| Agent 对话 | `repeat_run` 缺参时 | **两步协议**：首次调用缺参 → 工具不启动运行，返回"缺失参数清单+说明"文本 → 模型据此向用户提问 → 带 `params` 再调用。模型全程不接触回放执行，仅收集参数值 |
| CLI | `repeat replay` | `--params p.json` / `--param k=v`（可多次）/ 两者皆无时交互提示（`secret` 走隐藏输入） |
| 定时无人值守 | scheduler | 只接受"调度任务携带值 + derived + default"三种来源；否则 RP-8003，绝不静默用录制旧值跑敏感参数 |

**步骤内人工输入**（第五种形态）：`manual_confirm` 步骤（密码框、验证码等，§3.8 编译期强制生成）挂起时向用户展示 `params.prompt` 文案，由**人亲自输入**——值全程不经程序注入与存储，适合"连参数都不想经 Agent 过一遍"的场景。

#### 6.6.4 前置校验与注入时机

1. `POST /runs` / `repeat_run` 先做**静态干校验**：所有 `{{param.*}}` 占位符可解析 + 类型/`pattern` 通过 + `runtime_only` 已注入，任一失败返回 422 与缺失/非法清单，状态机根本不进入（避免跑一半才发现参数错，此时副作用已发生）；
2. 占位符渲染发生在**每步 act 开始时**（运行时替换），不在编译期展开——保证定义文件中无明文敏感值、同一工作流可多参数复用；
3. 渲染后的实际值只在内存中传递给 `actions.py`；`type` 动作读回校验（RP-5003）比对的是渲染值。

---

## 7. 异常处理设计

### 7.1 错误码体系

`RP-<域><序号>`，域：1 环境、2 定位、3 时序、4 权限、5 输入、6 数据、7 系统、8 参数。完整表见附录 B。参数域错误码的语义与拦截时机见 §6.6。

```python
@dataclass(slots=True)
class RepeatError(Exception):
    code: str                 # RP-2001
    message: str              # 面向用户的中文
    step_sid: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)
    recoverable: bool = False
```

### 7.2 异常处理矩阵

| 错误码 | 检测点 | 判据 | 默认动作 | 可续跑 | 用户可见提示 | 留证 |
|---|---|---|---|---|---|---|
| RP-1001 | precheck / 每步 | `OpenInputDesktop` 失败或会话非活动 | 中止 | 否 | "当前无图形会话（锁屏/远程断开），请解锁后重试" | 环境快照 |
| RP-1002 | precheck / wait_window | 进程枚举无此 exe | 若工作流有 `launch` 则启动并等待，否则挂起 | 是 | "未找到应用 X，请先启动" | 进程列表 |
| RP-1003 | wait_window | 进程在但无匹配窗口 | 重试 2 次 → 挂起 | 是 | "窗口『订单管理』未出现，可能被最小化到托盘" | 窗口树 |
| RP-1004 | precheck | 显示器数量/矩形与基线不同 | L2 继续 / L3 挂起 | 是 | "显示器配置已变化" | 前后拓扑 |
| RP-1005 | precheck | 缩放变化导致逻辑分辨率不一致 | 禁用 coord，降级运行 | 是 | "系统缩放已变化，坐标定位已停用" | 前后 DPI |
| RP-1006 | 探活 | `SendMessageTimeout` 5 s 无响应 | 中止 + 快照 | 是（应用恢复后） | "目标应用无响应" | 全屏帧 + 窗口树 |
| RP-1007 | 每步前 | 出现未预期的可见模态窗口 | 挂起等人工 | 是 | "检测到弹窗『…』，请处理后继续" | 弹窗截图 |
| RP-1008 | window_focus | 窗口 `cloaked` 或在其他虚拟桌面 | 挂起 | 是 | "窗口在另一个虚拟桌面" | 桌面信息 |
| RP-2001 | resolve | 链内全部 miss | 重试 → 挂起 | 是 | "第 N 步找不到目标『客户名称』" | 每策略诊断 + 新旧截图 |
| RP-2002 | resolve | UIA 查询超时 | 跳过该策略继续降级 | 是 | 仅日志 | 耗时 |
| RP-2003 | resolve | 图像匹配 < 阈值 | 降级下一策略 | 是 | 仅日志 | 匹配分值 |
| RP-2004 | resolve | 元素中心 `WindowFromPoint` 不属于目标 | 降级 / 挂起 | 是 | "目标被其他窗口遮挡" | 遮挡窗口信息 |
| RP-2005 | resolve / act | `is_enabled=false` 或 `is_offscreen=true` | 等待 2 s 再判 → 挂起 | 是 | "控件当前不可用" | 元素属性 |
| RP-2006 | resolve | 坐标策略被环境闸门禁用 | 挂起 | 是 | "环境变化较大，已阻止坐标兜底" | 漂移级别 |
| RP-3001 | pre_wait | 超时未出现 | 重试 → 挂起 | 是 | "等待『…』超时" | 超时时刻截图 |
| RP-3002 | assert | 断言不满足 | 按 `on_fail` | 视策略 | "结果校验失败：期望 X，实际 Y" | 值对比 |
| RP-3003 | pacing | 步骤累计耗时 > 预算 | 中止 | 是 | "第 N 步超时" | 阶段耗时 |
| RP-3004 | settle | 界面持续变化 2 s | 继续执行但标记 `unstable` | 是 | 仅日志 | 差分序列 |
| RP-4001 | act | `SendInput` 返回 0 且 err=5 | 中止 | 否 | "目标窗口以管理员权限运行，无法操作" | 完整性级别 |
| RP-4002 | safety_gate | 策略拒绝 | 中止 | 否 | "该操作被安全策略拦截：…" | 策略签名 |
| RP-4003 | approval | 审批拒绝/超时 | 中止 | 是 | "未获得执行授权" | 审批记录 |
| RP-4004 | act | 密码字段未授权回放 | 挂起 | 是 | "密码字段需人工输入" | 步骤 sid |
| RP-5001 | window_focus | `GetForegroundWindow` 校验失败 3 次 | 挂起 | 是 | "无法切换到目标窗口" | 前台窗口链 |
| RP-5002 | type | 剪贴板打开/写入/恢复失败 | 降级 Unicode 注入 | 是 | 仅日志 | 错误码 |
| RP-5003 | type | 输入后值校验不一致 | 重试 1 次 → 挂起 | 是 | "输入内容与预期不一致（可能被输入法或控件过滤）" | 期望/实际摘要 |
| RP-5004 | start_run | 命名互斥体已存在 | 拒绝 | 是 | "已有录制或回放在进行" | 持有者 pid |
| RP-6001 | 编译/回放前 | manifest 校验失败 | 拒绝 | 否 | "录制数据完整性校验失败" | 差异文件 |
| RP-6002 | 读取 | `schema > CURRENT` | 拒绝 | 否 | "数据版本过新，请升级程序" | 版本号 |
| RP-6003 | 读取 | 末行截断/JSON 解析失败 | 丢弃末行继续，标记 truncated | 是 | "事件流末尾不完整，已忽略最后一条" | 行号 |
| RP-6004 | 加载工作流 | 步骤校验失败 | 拒绝运行 | 否 | "流程定义非法：…" | 校验路径 |
| RP-7001 | 热键 | 用户按下紧急停止 | 中止 | 是 | "已按用户要求停止" | 最后动作 |
| RP-7002 | 运行中 | 检测到非注入输入 | 暂停 → 挂起 | 是 | "检测到人工介入，已暂停" | 输入事件 |
| RP-7003 | 全局 | 未捕获异常 | 中止 + 堆栈 | 是 | "内部错误，已生成诊断包" | 堆栈 + 快照 |
| RP-7004 | 录制 | 心跳丢失 >2 s | 结束录制并标记 truncated | 是 | "录制器异常退出，已保留已录部分" | recorder.log |
| RP-8001 | POST /runs（静态干校验） | required 参数无值（无显式传入/derived/default） | 拒绝启动 | 是 | "缺少参数：cust_name（客户名称）" | 缺失清单 |
| RP-8002 | POST /runs（静态干校验） | 参数类型/`pattern` 校验失败 | 拒绝启动 | 是 | "参数 report_date 格式不符：应为 YYYY-MM-DD" | 非法值与规则 |
| RP-8003 | 无人值守启动前 | `runtime_only`/`sensitive` 参数未注入 | 拒绝启动 + 通知 | 是 | "本流程含每次须人工提供的参数，无法无人值守运行" | 参数名（不含值） |

### 7.3 关键边界场景

#### 场景 1：回放时窗口未找到

三种子情形必须区分，处理路径不同：

```
步骤需要窗口 W
  ├─ 目标 exe 不在进程列表
  │    ├─ 工作流含 launch 块（录制时显式授权）→ 启动 → 等窗口 ≤ launch_timeout_s
  │    └─ 否则 → RP-1002，挂起，提示"请先启动 X"
  ├─ 进程在，但 EnumWindows 无匹配（pid + title_regex）
  │    ├─ 放宽到 (pid + class) → 命中则继续（标题已改）
  │    ├─ 放宽到 (exe + title_regex, 任意 pid) → 命中则继续（进程重启）
  │    ├─ 命中但 minimized → ShowWindow(SW_RESTORE) 后继续
  │    ├─ 命中但 cloaked / 其他虚拟桌面 → RP-1008 挂起
  │    └─ 全不中 → RP-1003，重试 2 次（800 ms 退避）→ 挂起
  └─ 进程在但无响应 → RP-1006 中止 + 快照
```

匹配优先级固定为：`(pid, title)` → `(pid, class)` → `(exe, title)` → `(exe, class)`。每次放宽都写入 `StepRun.attempts`，供后续优化 `app_hint`。

#### 场景 2：窗口位置/大小变化

- `relative` 策略使用录制时的 `rel` 比例；只要客户区尺寸变化 <50%，比例映射误差通常在 ±8 px，可接受；
- 客户区尺寸变化 ≥50%，`relative` 降权到链尾并标 `degraded`；
- 窗口最小化时**先恢复再定位**（最小化窗口的 UIA `BoundingRectangle` 为 0，直接定位会得到 (0,0)）；
- 窗口最大化状态变化不影响 `rel`，但会影响 `coord`（已由闸门处理）。

#### 场景 3：分辨率/DPI/缩放变化

分四级处理（§6.2）。补充规则：

- 录制时 `scale=1.25`、回放 `scale=1.0` 且逻辑分辨率一致 → L1，`coord` 按 `logical` 换算后使用；
- 只要有一台显示器矩形变化，整链的 `coord` 全部禁用（不做"猜哪台变了"）；
- `image` 锚点启用多尺度 `[0.8, 0.9, 1.0, 1.1, 1.25]`，命中后按尺度反算真实坐标；
- 变化写入运行记录 `env.drift`，并在 UI 顶部显示横幅"环境已变化，本步骤靠图像定位完成"。

#### 场景 4：多显示器与负坐标

- 记录 `virtual_screen` 四元组，注入时使用 `MOUSEEVENTF_VIRTUALDESK`；
- 目标显示器被拔掉 → 该显示器上的窗口会被系统迁移，`monitor` 校验失败 → 视为 L3；
- 主屏切换 → 虚拟桌面原点变化，`coord` 全部失效（L3），但 `rel` 仍可用。

#### 场景 5：控件消失/界面改版

- UIA 四类策略全 miss → 图像匹配（多尺度）→ 相对 → 坐标（受闸门）；
- 若图像与相对均 miss，进入失败阶梯：重试 2 次（期间放宽 `name` 编辑距离至 ≤2、结构路径兄弟序号容差 ±1）→ 环境为 L0/L1 时允许 coord 兜底一次 → 仍失败则挂起；
- 挂起时生成**并排对比**：左"录制时的目标"（锚点图 + 属性），右"现在的界面"（当前帧 + 命中区域候选项 top3），用户点选即重写链首（M2 提供基础版，M5 提供引导式界面）。

#### 场景 6：前台激活失败

Windows 限制非前台进程调用 `SetForegroundWindow`。处理顺序：

1. 优先使用 UIA Pattern（`Invoke`/`SetValue`）——**不需要前台**；
2. 必须前台时：`AttachThreadInput` 到目标线程 + 模拟 ALT 按下 + `SetForegroundWindow` + 校验；
3. 校验失败重试 3 次（间隔 200 ms）；
4. 仍失败 → 点击标题栏坐标（坐标来自 `win.rect` 顶部中央）；
5. 再失败 → RP-5001 挂起。

#### 场景 7：输入法/文本不一致

- 编译期：`ime_risk=true` 的步骤标 `needs_review`，参数化后由用户确认文本；
- 回放期：`type` 三通道依次尝试；`ValuePattern.SetValue` 成功后读回校验；
- 校验不一致 → 重试一次（改用剪贴板）→ 仍不一致 → RP-5003 挂起；
- 密码框：永不校验值，只校验"输入了 N 个字符"的长度（若可读长度）。

#### 场景 8：意外弹窗/模态框

每步 `resolve` 前做一次轻量扫描：枚举目标 pid 的可见顶层窗口，若出现不在预期集合（本步 `app_hint` 允许的窗口 + 录制期该步前后出现过的窗口）内的窗口，且该窗口 `IsWindowEnabled` 且尺寸 >100×80：

- 若弹窗标题/类命中"已知对话框"白名单 → 记录并继续（下一步通常会处理它）；
- 否则 RP-1007 挂起，展示弹窗截图，让用户决定"继续/跳过/中止"。

#### 场景 9：目标应用无响应

`SendMessageTimeout(hwnd, WM_NULL, 0, 0, SMTO_ABORTIFHUNG, 5000)` 返回 0 → RP-1006。处理：立即停止注入（继续注入会堆积消息）、保存全屏帧、枚举窗口树、转待办。**不做"强制结束进程"**——那是用户的决定。

#### 场景 10：权限提升窗口（UIPI）

- 录制期：`elevated=true` 时写 marker + 胶囊警示；
- 编译期：该步 `risk=dangerous`、`on_fail=suspend_manual`、`needs_review=true`；
- 回放期：`precheck` 比较自身与目标完整性级别；自身较低则 RP-4001 中止，提示"请以管理员身份运行 harness"；
- 产品文案不承诺覆盖提权窗口。

#### 场景 11：锁屏/远程桌面断开

`OpenInputDesktop` 失败、`WTSQuerySessionInformation` 显示会话断开、或 `GetForegroundWindow()==0` 且桌面名为 `Winlogon` → RP-1001。运行中检测到则立即中止（注入会失败并可能污染状态）。

#### 场景 12：运行中被用户接管

- 所有注入事件带 `INJECTED` 标记；executor 同时挂一个轻量钩子（仅运行期）监听非注入输入；
- 检测到非注入的鼠标移动 >30 px 或任意按键 → 立即暂停注入 → RP-7002 挂起；
- 恢复时要求用户确认"已松手"，并重新做一次环境复检；
- 紧急停止热键 `Ctrl+Alt+.` 独立于前台窗口，任何时候可用。

#### 场景 13：数据损坏/版本不兼容

- 编译前校验 manifest；失败 RP-6001，展示差异文件清单；
- 末行截断 RP-6003：丢弃末行、`truncated=true`、允许继续编译（UI 明示"最后 0.2 秒可能缺失"）；
- `schema` 高于当前程序 RP-6002：拒绝，提示升级；
- `schema` 较低：自动迁移，迁移结果写 `*.migrated` 副本，原文件不动。

#### 场景 14：参数缺失、非法或无人值守遇到敏感参数

回放 ≠ 重演录制内容——同一流程换数据跑才算参数化（参数体系见 §6.6）。三类典型分支：

```
启动前（静态干校验，任何副作用发生之前）
  ├─ required 参数无显式值且无 derived/default 可解析
  │    → RP-8001 拒绝启动，返回缺失清单（参数名+描述；绝不回显已录旧值当"答案"）
  ├─ 值类型不符或不过 pattern
  │    → RP-8002 拒绝启动，指明"哪个参数、期望什么格式"——
  │       在第 30 步控件拒绝输入暴露问题之前拦截
  └─ unattended=true 且含未注入的 runtime_only/sensitive 参数
       → RP-8003 拒绝启动 + 通知（不回退用录制旧值跑敏感参数；也不挂起占住调度队列）
```

运行中的相关分支：`manual_confirm` 等待人工输入超时 → 按 `policy.on_fail`（默认转待办挂起，用户回来输入后续跑，而非中止整流程）；`type` 读回值与渲染值不一致（目标控件自动大写/长度截断等改写输入）→ RP-5003，诊断包展示期望值（脱敏后）与实际读回对比。

### 7.4 失败阶梯（决策树）

```
步骤失败
├─ 属于瞬态（未就绪/遮挡/焦点）
│    └─ 退避重试：800 ms → 1600 ms（默认 2 次）
├─ 属于定位失败
│    ├─ 链内还有未尝试的策略 → 降级下一策略（记录 hit_index）
│    ├─ 链已耗尽 且 coord 存在 且 环境 ∈ {L0,L1} → 坐标兜底一次
│    ├─ 链已耗尽 且 环境 ∈ {L2,L3} → RP-2006（禁止盲点）
│    └─ 仍失败 → 按 on_fail：suspend_manual（默认）/ abort
├─ 属于安全策略拦截 → 立即中止 + 审计（绝不绕过）
├─ 属于断言失败 → optional 记警告；否则按 on_fail
└─ 属于应用挂死/系统错误 → 中止 + 诊断包 + 待办
```

重试预算：单步 2 次；单次运行累计 20 次；超出则整体转 `suspend_manual`，防止"无限重试掩盖真实故障"。

### 7.5 断点续跑

每步结束写检查点：

```json
{
  "run_id": "run-…",
  "workflow_id": "wf-…",
  "graph_sha256": "…",
  "completed": ["s01", "s02", "s03"],
  "current": "s04",
  "vars": {"cust_name": "ACME"},
  "degraded": true,
  "attempts_total": 3,
  "updated_at": "2026-10-18T21:03:11+08:00"
}
```

恢复流程：加载检查点 → 轻量环境复检 → 对 `idempotent=false` 且处于 `current` 的步骤要求人工确认（可能已部分生效）→ 从 `current` 继续。检查点同时写 `repeat_runs.checkpoint_json` 与 `runtime/checkpoint.json`，后者用于进程崩溃后的兜底。

### 7.6 诊断包

失败时生成 `diagnostics/<run_id>/`：

```
diagnostics/<run_id>/
├── report.json          # 错误码、步骤、attempts、环境、耗时
├── expected.png         # 录制时的目标锚点
├── actual.png           # 失败时刻全屏
├── overlay.png          # 期望位置框 + 候选命中框
├── window_tree.txt      # 目标进程窗口树
└── locator_trace.json   # 每个策略的耗时/原因/分值
```

UI 提供"打开诊断包"与"以此为据重选目标"两个动作；后者进入引导式修复（M2 基础版，M5 完整版）。

---

## 8. 可扩展性设计

### 8.1 扩展点清单

| # | 扩展点 | 契约 | 注册方式 | 是否需要改核心 |
|---|---|---|---|---|
| E1 | 定位器 | `Locator.find/describe_failure` | `@register_locator("kind")` | 否 |
| E2 | 动作 | `Action.run(ctx, step) -> ActionResult` | `@register_action("name")` | 否 |
| E3 | 等待策略 | `WaitStrategy.wait(ctx, spec) -> bool` | `@register_wait("kind")` | 否 |
| E4 | 断言 | `Assertion.check(ctx, spec) -> AssertResult` | `@register_assert("kind")` | 否 |
| E5 | 解释规则 | `Rule.apply(events, state) -> events` | `@register_rule(order=…)` | 否 |
| E6 | 事件类型 | `EventNormalizer.normalize(raw) -> RecEvent` | `@register_event("type")` | 否 |
| E7 | 存储后端 | `BundleStore` Protocol | 服务装配时注入 | 否 |
| E8 | 平台适配 | 5 个 Protocol（§8.3） | 装配时注入 | 否 |
| E9 | 通知点 | 复用 `channel_manager` + `harness/infra/notify.py` | 触发时机：录制停止结果（T13/T14/T09）、编译完成、回放开始/完成/失败（P01/P15/P16）、挂起待办 | 否 |
| E10 | 语义增强 | `SemanticNamer.name(steps) -> steps` | 注入 model_manager | 否 |

### 8.2 注册表与扩展示例

```python
# locators/registry.py
_LOCATORS: dict[str, type[Locator]] = {}
_ORDER: list[str] = []

def register_locator(kind: str, *, after: str | None = None):
    def deco(cls):
        if kind in _LOCATORS:
            raise ValueError(f"重复注册: {kind}")
        cls.kind = kind
        _LOCATORS[kind] = cls
        if after and after in _ORDER:
            _ORDER.insert(_ORDER.index(after) + 1, kind)
        else:
            _ORDER.append(kind)
        return cls
    return deco

def build_chain(specs: list[LocatorSpec]) -> list[Locator]:
    out = []
    for spec in specs:
        cls = _LOCATORS.get(spec.kind)
        if cls is None:
            logger.warning("未知定位器 %s，已跳过", spec.kind)
            continue
        out.append(cls())
    return out
```

**新增一个定位器（5 步）**：

1. 新建 `locators/ocr_text.py`，实现 `find` 与 `describe_failure`；
2. 加 `@register_locator("ocr_text", after="image")`；
3. 在 `locator_builder.py` 的打分表中加一行（可选）；
4. 在 `locators/__init__.py` 中 import，触发注册；
5. 补单测（纯逻辑）与一条 e2e 用例。**核心执行器零改动。**

**新增一个动作（5 步）**：实现 `Action` → 注册 → 在 `actions.py` 的动词表登记 → 补 `risk` 分级 → 补 Schema 枚举。

### 8.3 平台适配层

```python
class InputCapture(Protocol):
    def start(self, on_event: Callable[[RawInput], None]) -> None: ...
    def stop(self) -> None: ...

class UiInspector(Protocol):
    def element_from_point(self, x: int, y: int, timeout_ms: int) -> UiaSnapshot | None: ...
    def element_by_spec(self, spec: LocatorSpec, timeout_ms: int) -> Hit | Miss: ...
    def patterns_of(self, element: Any) -> list[str]: ...

class ScreenGrabber(Protocol):
    def grab(self, rect: tuple[int, int, int, int] | None = None) -> bytes: ...
    def monitors(self) -> list[MonitorInfo]: ...

class InputInjector(Protocol):
    def move(self, x: int, y: int) -> None: ...
    def click(self, x: int, y: int, button: str, count: int) -> int: ...
    def type_text(self, text: str) -> int: ...
    def hotkey(self, keys: list[str]) -> int: ...

class WindowManager(Protocol):
    def foreground(self) -> WindowRef | None: ...
    def enumerate(self, pid: int | None = None) -> list[WindowRef]: ...
    def focus(self, win: WindowRef) -> bool: ...
    def restore(self, win: WindowRef) -> None: ...
```

Windows 实现放 `platform/win32/`；macOS/Linux 只建空实现，方法体 `raise NotImplementedError("平台未适配")`，由 `platform/__init__.py` 按 `sys.platform` 选择。这样"加平台"= 新增一个目录，不动采集/执行逻辑。

### 8.4 Schema 演进

1. **只加字段**：不升版；写入端把未知扩展放 `ext`，读取端忽略；
2. **语义变更**：升版 + `migrate_N_to_N+1`，迁移器按链式调用；
3. **删除字段**：先标记 `deprecated` 一个版本，读取端兼容，下一版移除；
4. **读取端窗口**：`current` 与 `current-1`，更旧版本提示另存；
5. **测试**：`tests/fixtures/schema/v1/*.jsonl` 作为金样例，每次升版必须新增 vN 样例并保留旧样例。

### 8.5 技能化扩展

工作流可一键导出为技能目录：

```
~/.andy-harness/skills/repeat-<slug>/
├── SKILL.md
└── references/
    └── params.md        # 参数详解（L3，按需读取）
```

`SKILL.md` 模板：

```markdown
---
name: repeat-daily-export
description: 执行「ERP 每日订单导出」桌面流程：打开订单管理 → 筛选今日 → 导出 CSV → 改名归档。Use when 用户要求跑每日订单导出、导出今日订单、执行 ERP 导出流程时。回放为本地确定性执行，不调用模型。
version: 1.0.0
license: MIT
allowed-tools: "repeat_run repeat_dry_run"
metadata:
  workflow_id: wf_ab12cd34
  workflow_version: 3
  graph_sha256: 9f2c…
  source_session: r-20261018-204512-a1b2c3
  last_verified_at: 2026-10-18T21:00:00+08:00
---

# ERP 每日订单导出

## 何时使用
用户要求导出当天订单、生成订单 CSV、跑每日导出流程时。

## 参数
| 参数 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `export_dir` | 是 | — | 导出目录绝对路径 |
| `date` | 否 | 今天 | 筛选日期，YYYY-MM-DD |

## 执行
调用 `repeat_run(workflow_id="wf_ab12cd34", params={...})`。
回放为确定性执行，不消耗模型 tokens。

## 前置条件
- ERP 客户端已登录且窗口未最小化到托盘；
- 屏幕缩放与录制时一致（当前记录 125%）。

## 失败处理
失败时返回 `run_id` 与诊断包路径；先看 `expected.png` / `actual.png` 对比，
再用 `repeat_dry_run` 复检定位链。
```

**版本绑定**：技能写死 `workflow_id + workflow_version + graph_sha256`；`repeat_run` 支持传入 `expected_graph_sha256`，不一致时拒绝执行并提示"流程已更新，请重新导出技能"。

---

## 9. 安全、隐私与合规

### 9.1 三层防护

| 层 | 机制 | 落点 |
|---|---|---|
| 采集授权 | 显式触发 + 3 秒倒计时 + 常驻红点 + 一键丢弃 + 全本地存储 | overlay |
| 数据最小化 | 密码永不出值、屏蔽窗口不截不查、剪贴板默认不取、exe 不存全路径 | privacy.py |
| 动作护栏 | `risk` 三级 + `app_hint` 作用域 + 审批 + 坐标闸门 + 提权拒绝 | safety_gate.py |

### 9.2 风险分级与审批

| risk | 判定 | 首次执行 | 后续 |
|---|---|---|---|
| `read` | 纯查询、无副作用 | 直接执行 | 直接执行 |
| `write` | 输入、点击普通按钮、切窗口 | 会话内首次需确认（可"本会话不再询问"） | 直接执行 |
| `dangerous` | 命中危险词库、提权窗口、删除/支付/发送 | 每次确认 | 每次确认 |

审批走 `permission_manager`，超时（默认 300 s）视为拒绝（RP-4003）。

### 9.3 审计与留证

- `audit.jsonl` 追加式：`{ts, run_id, sid, action, risk, decision, actor}`；
- 审计模式（可选）把 bundle 挂到 `evidence_v2` 哈希链；
- 运行记录保留 180 天，诊断包保留 30 天。

### 9.4 用户告知

首次使用弹出一次性说明（不重复打扰）：录制会捕获键鼠操作与局部截图、数据仅存本地、密码框自动屏蔽、可随时 `Ctrl+Alt+X` 丢弃。README 与插件描述同步声明边界（不覆盖提权窗口、不用于对抗反自动化系统）。

### 9.5 用户操作前后的提示节点（录制与回放）

设计原则：**状态转换对用户可见；控制权归属明确；无人值守时提示可降级但不静默**。录制与回放的开始/结束都必须有显式提示节点——录制侧由覆盖层承担（子进程自治，backend 崩溃提示不丢），回放侧由通知中心承担（无覆盖层，必须走系统/前端通道）。

#### 9.5.1 录制侧提示清单

| 节点 | 时机 | 呈现 | 可配 | 说明 |
|---|---|---|---|---|
| 首次授权说明 | 首次 `start_recording` | 弹窗（一次性） | 可重开 | 即 §9.4 |
| 开始确认 | 触发录制 | 遮罩 + 3-2-1 倒计时（`countdown_s` 可配 0/3/5） | ✅ | 倒计时期间操作不拦截也不记录 |
| 录制中常驻 | recording / paused | 红点胶囊（时长/事件数/队列水位）、系统托盘图标 + 任务栏着色 | ❌ 不可关 | 暂停变黄 |
| 实时告警 | 录到提权窗口 / UIA 大面积超时 | 胶囊橙色警示 + marker | ❌ | 见 §3.7 |
| **停止即时回执** | 停止指令生效 | 胶囊"处理中…"→ 结果横幅 | ❌ | 补齐原设计缺口（见 9.5.5） |
| **结果横幅（成功）** | T13 封版 | 非模态横幅 5 s：已保存 N 个事件 / 时长 / 体积 / bundle 路径 / 按钮〔立即解析〕〔在文件夹中显示〕 | ✅ | 与完成事件同刻由 recorder 呈现，不依赖 backend |
| **结果横幅（失败）** | T14 校验失败 | 错误横幅 + 原因 + 按钮〔重试封版〕〔放弃并保留原始数据〕 | ❌ | |
| 异常中断横幅 | T09 心跳丢失 / 子进程崩溃 | "录制器异常退出，已保留已录部分（truncated）" | ❌ | |
| 丢弃确认 | `Ctrl+Alt+X` 或 API | 二次确认框 → discarded | ✅ 免确认 | 确认后才软删 |
| 编译完成通知 | 编译结束 | 结果（步数 / 警告数 / `needs_review` 数）→ 前端通知 + `repeat_compile` 工具返回 | ❌ | 自动解析模式下必须发 |

#### 9.5.2 回放侧提示清单

| 节点 | 时机 | 呈现 | 默认（无人值守） | 说明 |
|---|---|---|---|---|
| 执行前告知 | 触发执行（P01 前） | 横幅"即将自动执行《X》（N 步），请勿操作鼠标键盘；紧急停止 Ctrl+Alt+." + 3 s 准备 | 跳过 | `prompt.pre_run_notice=true`、`pre_run_ready_s=3`、`skip_notice_unattended=true` |
| 环境漂移警示 | P03/P04 | 漂移级别 + 后果（如"系统缩放已变化，坐标定位已停用"） | 横幅 + 挂起 | |
| 危险步骤审批 | 每步前（P10 前） | permission_manager 审批请求 | `suspend_manual` 并转待办 | §9.2 |
| 接管暂停告知 | P17（RP-7002） | 横幅"检测到人工介入，已暂停，松手后点继续" | 仅日志 | |
| 挂起告知 | P10 / P12 | 通知中心（可配 channel_manager 外推）+ `todo_manager` 待办，附诊断包路径 | 待办 + 通知 | |
| **完成回执** | P15（`run_done`） | 结果横幅：成功 N/失败 M/降级 K/耗时；定时任务外推 channel_manager | **必发** | |
| **失败回执** | P16（`run_error`）/ aborted | 结果横幅 + 诊断包路径；失败时系统通知置顶 | **必发** | |
| 干跑报告 | P18 | 体检结果汇总 | 不适用 | |

#### 9.5.3 通知通道与实现

| 通道 | 用途 | 实现 |
|---|---|---|
| 系统通知（Toast） | 所有即时回执与结果 | `harness/infra/notify.py`（新建，L5 治理层）：`NotifyService` Protocol；win32 首选 `Shell_NotifyIcon` 常驻托盘（插件生命周期内常驻，承载录制胶囊与通知），回退 `powershell Toast`（**经 `curl` 无关，本地执行**，5.1 秒内可接受） |
| 覆盖层横幅 | 录制侧结果横幅（recorder 自治呈现） | tkinter，停止指令后驻留 8 s 再退出 |
| 前端通知中心 | 编译/回放结果、挂起 | SSE（`run_done`/`run_error`/`compile_done`）+ 顶栏铃铛，复用 `progress.py` 范式 |
| channel_manager | 无人值守定时任务的结果外推 | E9 扩展点，复用既有渠道（飞书/Telegram 等） |
| 对话文本 | Agent 调用工具时 | `repeat_*` 工具返回值即回执 |

#### 9.5.4 降级规则

1. **提示永不阻塞主流程**：横幅/通知全部异步、带投递超时（≤2 s），失败只记日志；
2. **优先级冲突**：用户正在全屏专注（前台为全屏应用）时，结果横幅不抢占，降级为仅托盘 + 通知中心；
3. **无人值守（`unattended=true`）**：不弹任何模态框；前置横幅跳过；结果横幅改为"必发通知 + 待办"；
4. **断链兜底**：backend 崩溃或重启时，录制侧横幅/结果由 recorder 自行呈现（覆盖层常驻至结果落定），避免"停止后无任何反馈"。

#### 9.5.5 原设计缺口与修补说明

| 缺口 | 修补 |
|---|---|
| 停止录制后无反馈（仅"写 session.json → 退出"） | T13/T14 副作用补"呈现结果/错误横幅"；§3.7"结束"补横幅说明 |
| 回放触发后"请勿动鼠标键盘"仅文字原则，无状态机落点 | P01 副作用补"执行前告知横幅"；§6.4 节拍表加行 |
| E9 通知点过于笼统 | 改为"通知点"并写明四类触发时机 |
| 托盘/常驻提示未入目录 | 新增 `harness/infra/notify.py`，见附录 D |

---

## 10. 集成接口

### 10.1 Python API（Service 门面）

```python
class DesktopRepeatService:
    # 录制
    async def start_recording(self, cfg: RecordConfig) -> SessionDTO: ...
    async def stop_recording(self, session_id: str, *, discard: bool = False) -> SessionDTO: ...
    async def get_session(self, session_id: str) -> SessionDTO | None: ...
    def list_sessions(self, *, limit: int = 50, offset: int = 0) -> list[SessionDTO]: ...

    # 编译
    async def compile_session(self, session_id: str, *, use_llm: bool = False) -> CompileResult: ...
    async def save_as_workflow(self, session_id: str, name: str, description: str = "") -> dict: ...

    # 运行
    async def dry_run(self, workflow_id: str, params: dict | None = None) -> DryRunReport: ...
    async def start_run(self, workflow_id: str, params: dict | None = None, *,
                        allow_blind_coord: bool = False,
                        expected_graph_sha256: str | None = None) -> RunDTO: ...
    async def resume_run(self, run_id: str, decision: str) -> RunDTO: ...
    async def abort_run(self, run_id: str, reason: str = "user") -> RunDTO: ...
    def get_run(self, run_id: str) -> RunDTO | None: ...
    async def subscribe(self, run_id: str) -> AsyncIterator[dict]: ...

    # 技能
    async def export_skill(self, workflow_id: str, slug: str | None = None) -> Path: ...
```

### 10.2 工具清单

| 工具 | 风险 | 参数 | 返回 |
|---|---|---|---|
| `repeat_record_start` | write | `label?`、`frame_tier?`、`countdown_s?`、`mask_windows?` | `session_id`、状态 |
| `repeat_record_stop` | write | `session_id?`、`discard?` | 统计摘要 |
| `repeat_record_status` | read | — | 当前会话状态/事件数/时长 |
| `repeat_compile` | read | `session_id`、`use_llm?` | 步数、警告数、草稿工作流 id |
| `repeat_list` | read | `kind`（sessions/workflows/runs）、`limit?` | 列表 |
| `repeat_dry_run` | read | `workflow_id`、`params?` | 每步命中情况 + 汇总 |
| `repeat_run` | dangerous | `workflow_id`、`params?`、`expected_graph_sha256?`、`allow_blind_coord?` | `run_id` + 最终状态 |
| `repeat_run_status` | read | `run_id` | 状态、当前步、进度 |
| `repeat_abort` | write | `run_id` | 终态 |
| `repeat_export_skill` | write | `workflow_id`、`slug?` | 技能目录路径 |

工具描述必须写明"回放不调用模型、需目标应用已启动、提权窗口不支持"，避免模型误用。

### 10.3 REST + SSE

| 方法 | 路径 | 请求 | 响应 |
|---|---|---|---|
| POST | `/api/desktop-repeat/sessions` | `RecordConfig` | `SessionDTO` |
| POST | `/api/desktop-repeat/sessions/{id}/stop` | `{discard?: bool}` | `SessionDTO` |
| GET | `/api/desktop-repeat/sessions` | `?limit&offset` | `{items, total}` |
| GET | `/api/desktop-repeat/sessions/{id}` | — | `SessionDTO` |
| DELETE | `/api/desktop-repeat/sessions/{id}` | — | `{ok}` |
| POST | `/api/desktop-repeat/sessions/{id}/compile` | `{use_llm?: bool, save?: bool, name?: str}` | `CompileResult` / `workflow_id` |
| POST | `/api/desktop-repeat/dry-run` | `{workflow_id, params?}` | `DryRunReport` |
| POST | `/api/desktop-repeat/runs` | `{workflow_id, params?, allow_blind_coord?}` | `RunDTO` |
| GET | `/api/desktop-repeat/runs/{id}` | — | `RunDTO` |
| POST | `/api/desktop-repeat/runs/{id}/resume` | `{decision: retry\|skip\|abort}` | `RunDTO` |
| POST | `/api/desktop-repeat/runs/{id}/abort` | `{reason?}` | `RunDTO` |
| GET | `/api/desktop-repeat/runs/{id}/events` | SSE | 事件流 |
| POST | `/api/desktop-repeat/workflows/{id}/skill` | `{slug?}` | `{path}` |

SSE 事件类型：

| type | 载荷 |
|---|---|
| `run_started` | `run_id, workflow_id, total_steps` |
| `precheck_done` | `env_level, drift, apps[]` |
| `step_started` | `sid, index, action, label` |
| `step_retry` | `sid, attempt, reason` |
| `step_done` | `sid, status, hit_index, kind, elapsed_ms` |
| `step_failed` | `sid, code, message` |
| `run_suspended` | `run_id, sid, code, diagnostics_path` |
| `run_resumed` | `run_id, decision` |
| `run_done` | `run_id, stats` |
| `run_error` | `run_id, code, message` |
| `notice` | `level(info/warn/error), title, body, actions[]`——§9.5 提示节点的 SSE 镜像（前端通知中心数据源） |
| `heartbeat` | `ts`（15 s 一次，防代理超时断流） |

**SSE 实现约束**（复用 `progress.py` 的两个已修坑）：历史回放必须用 `list(...)` 快照；遇到 `run_done`/`run_error` 立即 return，不能继续挂在队列上。

### 10.4 前端工作台

三栏布局：

```
┌ 左：会话列表（时间/标签/时长/事件数/状态徽标） ┬ 中：步骤时间线 ┬ 右：详情 ┬
│ 顶部：● 开始录制 / 停止 / 丢弃                  │ 步骤卡片        │ 定位链 │
│ 筛选：全部 / 已编译 / 失败                       │ 置信度色条      │ 截图   │
└──────────────────────────────────────────────┴────────────────┴────────┘
```

关键交互：

- 录制中顶部显示红点 + 计时 + 事件数（500 ms 刷新，走 SSE 或轻量轮询）；
- 步骤卡片颜色：绿（confidence ≥0.8）/ 黄（0.5–0.8）/ 红（<0.5 或 needs_review）；
- 点卡片右侧展开定位链，可拖动调整顺序、禁用某一项；
- "干跑"按钮在步骤卡片上直接显示命中徽标（第 1 项命中 / 第 4 项命中 / miss）；
- **运行对话框**：点"执行"弹出，按 `start.params` 渲染表单（必填标记/描述/格式提示；敏感参数密码框且禁"记住"；非敏感可记住上次）；缺参/格式错就地标红，提交前即可看到 RP-8001/8002 对应提示；
- 运行中时间线实时高亮当前步，失败步展开诊断包。

### 10.5 CLI

```
repeat record  --out <dir> [--tier standard] [--countdown 3] [--label "..."]
repeat compile <session_id|path> [--name "流程名"] [--llm]
repeat dry-run <workflow_id> [--params p.json]
repeat replay  <workflow_id> [--params p.json] [--allow-blind-coord] [--speed 1.0]
repeat skill   <workflow_id> [--slug name]
repeat inspect <session_id|run_id>
```

独立包形态下 `repeat` 为入口命令；harness 内通过 `python -m harness.modules.desktop_repeat.cli` 调用同一套实现。

---

## 11. 性能与容量预算

测试基线：i7-8550U / 16 GB / 1920×1080@125% / Windows 10 22H2（与开发机一致）。

| 指标 | 预算 | 测量点 | 超限处置 |
|---|---|---|---|
| 钩子回调 p99 | <1 ms | 回调入口/出口时间差 | 告警 `hook_slow` |
| 归一化吞吐 | >2000 ev/s | worker 计数 | 降采样 |
| UIA 查询 p95 | <300 ms | 单次调用 | 硬超时置空 |
| 帧裁剪 p95 | <60 ms | grab+crop+encode | 跳帧计数 |
| 录制 CPU（整机） | <5% | 进程 CPU 采样 | 降档留证 |
| 录制内存 | <150 MB | RSS | 回收帧缓存 |
| 回放单步开销 | 中位 <80 ms | resolve→settle | 优化定位链顺序 |
| 回放内存 | <200 MB | RSS | 帧不驻留，按需读 |
| 干跑 300 步 | <60 s | 端到端 | 并行解析（只读，可并发） |
| 事件流读取 | >50 MB/s | 编译耗时 | 流式逐行，不整体加载 |

长流程优化：

1. 事件流流式读取（生成器），编译期内存只保留窗口内的语义事件；
2. 帧按需读取，编译后立即释放；
3. 300 步以上的工作流分块编译（每 100 步一次落盘），失败可从中断处继续；
4. 干跑对只读定位器可并发（同一 UIA 线程内串行，跨窗口并行度 2）。

---

## 12. 测试与验收

### 12.1 测试分层

| 层 | 对象 | 手段 | 是否进 CI |
|---|---|---|---|
| 单元 | 归一化、归并、打分、坐标换算、环境分级、Schema 校验 | 纯函数 + 金样例 | 是 |
| 组件 | 编译管线、执行器、失败阶梯 | `FakeUiBridge` + `FakeDesktopController` | 是 |
| e2e | 录制→编译→回放闭环 | 自带 tkinter 样例应用（固定 AutomationId） | Windows runner |
| 真机回归 | 记事本 / 资源管理器 / Chrome | 脚本化冒烟，统计命中率 | 手动/定时 |
| 故障注入 | 异常路径 | 见 §12.2 | 部分 |

### 12.2 关键测试用例

| ID | 场景 | 注入方式 | 期望 |
|---|---|---|---|
| T-01 | 录制 10 分钟无丢事件 | 脚本化键鼠回放 | seq 连续率 100%，click/key 丢失 0 |
| T-02 | 钩子回调超时 | 人为在回调内 sleep 400 ms | 系统摘钩子被检测到，状态转 failed 且保留数据 |
| T-03 | 录制器崩溃 | kill -9 recorder | 心跳丢失 → failed + truncated，事件可读 |
| T-04 | 事件末行截断 | 手工截断 jsonl | 编译成功，`truncated=true`，警告 1 条 |
| T-05 | 密码框 | 样例应用密码输入 | bundle 内无明文，步骤为 `manual_confirm` |
| T-06 | IME 输入 | 样例应用中文输入 | 生成 `text_input`，回放输出一致 |
| T-07 | 窗口移动 200 px | 回放前移动窗口 | relative 命中，`degraded=true`，成功 |
| T-08 | 窗口最小化 | 回放前最小化 | 自动 restore 后成功 |
| T-09 | 分辨率变更（同比例） | 修改显示设置或 FakeEnv | coord 禁用、image 命中、成功 |
| T-10 | 缩放变更（逻辑分辨率同） | FakeEnv L1 | coord 换算后命中 |
| T-11 | 显示器拔插 | FakeEnv L3 | precheck 挂起，提示重新录制 |
| T-12 | 控件消失 | 样例应用改 AutomationId | 链降级到 image，`degraded=true` |
| T-13 | 全链失效 | 删除控件 | RP-2001，诊断包含 attempts |
| T-14 | 意外弹窗 | 回放中弹出模态框 | RP-1007 挂起，截图正确 |
| T-15 | 应用无响应 | 样例应用卡死主线程 | RP-1006，5 s 内中止 |
| T-16 | 提权窗口 | 以管理员启动样例应用 | RP-4001，提示明确 |
| T-17 | 用户接管 | 回放中移动真实鼠标 | RP-7002 暂停 |
| T-18 | 紧急停止 | 按 Ctrl+Alt+. | 1 s 内停止注入 |
| T-19 | 输入内容被过滤 | 控件限制长度 | RP-5003 挂起，期望/实际可见 |
| T-20 | 并发运行 | 同时发两个 run | 第二个 RP-5004 |
| T-21 | Schema 过新 | 手工改 v=99 | RP-6002 |
| T-22 | 断点续跑 | 第 5 步挂起后恢复 | 从第 5 步继续，前 4 步不重放 |
| T-23 | 干跑无副作用 | 运行 dry-run | 无任何输入注入（钩子计数为 0） |
| T-24 | 技能导出 | 导出后对话调用 | `repeat_run` 命中，零 LLM 调用 |
| T-25 | 暂停键不想录 | 录制中按 Ctrl+Alt+P 暂停 → 操作若干（含密码框）→ resume → 再按 Ctrl+Alt+R 结束 | 暂停区间无任何输入事件入库；帧不采集；resume 后正常采集；结束横幅正确显示事件数 |
| T-26 | 暂停中直接丢弃 | 暂停态按 Ctrl+Alt+X 并确认 | 状态转 discarded，bundle 软删，托盘提示"已丢弃" |
| T-27 | 提示节点全覆盖 | 脚本化"开始→录 10s→停止"与"执行→完成/失败"各一轮 | 每次转换都有对应提示：倒计时/胶囊/结果横幅/前置告知/完成回执（§9.5.1–9.5.2 清单逐条勾验）；断言提示失败不影响主流程（§9.5.4 规则 1） |
| T-28 | 参数换值回放 | 录制输入 `a`，发布为参数，回放传入 `b` | 目标控件实际读到 `b`；工作流定义与运行记录中不出现 `a`（除 start default 外）；缺参时 RP-8001 清单正确且未发生任何副作用 |
| T-29 | 敏感参数无人值守 | scheduler 运行含 secret 参数的流程 | RP-8003 拒绝启动 + 通知；日志/SSE/审计中该参数值为 `***`；`manual_confirm` 步骤人工输入后值不入库 |

### 12.3 验收判据

| 编号 | 判据 | 门槛 |
|---|---|---|
| A1 | 录制完整性 | 10 分钟会话 seq 连续率 100%，非 move 事件丢失 ≤0.1% |
| A2 | 同机回放成功率 | 三场景各 20 次，≥95% |
| A3 | 窗口位移鲁棒性 | 移动 200 px 后 ≥90% |
| A4 | 分辨率变化鲁棒性 | 同比例变化后 ≥80% |
| A5 | 失败可诊断 | 100% 失败产生诊断包，含 attempts 与对比图 |
| A6 | 数据最小化 | 密码明文出现次数 = 0（自动扫描 bundle） |
| A7 | 干跑安全 | 干跑期间注入事件数 = 0 |
| A8 | 零模型依赖 | 断网环境完成 10 步回放 |
| A9 | 长流程稳定性 | 300 步运行内存 <200 MB，无句柄泄漏 |
| A10 | 技能可用 | 对话中"跑一下我录的流程"可命中并执行 |
| A11 | 参数化换值 | 录制值 `a` → 回放输入 `b` 实际生效；缺参在零副作用前被 RP-8001 拦截（T-28） |
| A12 | 提示节点全覆盖 | §9.5.1–9.5.2 清单逐条可验证；敏感参数在日志/SSE/审计中恒为 `***`（T-27/T-29） |

---

## 13. 实施计划

### 13.1 里程碑

| 阶段 | 周期 | 出口判据 |
|---|---|---|
| M0 环境 Spike | 2 人日 | 真机验证 pynput / UIA / mss / SendInput 可用性，输出受限清单 |
| M1 录放最小闭环 | 10–13 人日 | 记事本场景：录一段输入 → 出工作流 → 回放成功；REST/SSE + 最简面板可用 |
| M2 定位链与干跑 | 10–12 人日 | 三场景回放成功率 ≥90%；干跑/失败阶梯/诊断包完成 |
| M3 编辑·参数·调度 | 9–11 人日 | 画布编辑、参数化、循环展开、scheduler 定时跑通 |
| M4 技能与语义 | 5–6 人日 | SKILL.md 生成 + 对话调用 + LLM 语义命名（可选） |
| M5 分发与自愈 | 8–10 人日 | 导入扫描、引导式修复、命中率看板、跨机告警 |

### 13.2 WBS

| ID | 任务 | 产出 | 依赖 | 人日 |
|---|---|---|---|---|
| T0.1 | Spike：pynput/UIA/mss/SendInput 真机验证 | 结论清单 | — | 1.0 |
| T0.2 | 沙箱与真实终端差异验证 | 验证记录 | T0.1 | 0.5 |
| T0.3 | 记事本最小录放 demo | 可运行脚本 | T0.1 | 0.5 |
| T1.1 | `models.py` / `errors.py` / `config.py` | 数据模型与错误码 | — | 1.0 |
| T1.2 | `store/` bundle 读写 + manifest + 截断恢复 | 存储层 + 单测 | T1.1 | 1.5 |
| T1.3 | `recorder/runner` + 钩子 + 归一化 + 队列背压 | 可录制 | T1.1 | 2.0 |
| T1.4 | 窗口上下文 + DPI + UIA 快照 | 语义取证 | T1.3 | 1.5 |
| T1.5 | 关键帧策略 | 帧与锚点 | T1.3 | 1.0 |
| T1.6 | 覆盖层 + 热键 + 控制/心跳文件 | 交互闭环 | T1.3 | 1.0 |
| T1.7 | 编译 v0（S1–S4，click/type/hotkey） | 步骤序列 | T1.2 T1.4 | 1.5 |
| T1.8 | 执行器 v0 + 动作 v0 + 节拍 | 可回放 | T1.7 | 1.5 |
| T1.9 | Service + DB 三表 + REST/SSE + 插件 + 最简面板 | 端到端 | T1.8 | 2.0 |
| T2.1 | 六类定位器 + 打分 + 多尺度图像（opencv） | 定位链 | T1.7 | 2.0 |
| T2.2 | 环境校验与漂移分级 + 坐标闸门 | envcheck | T2.1 | 1.0 |
| T2.3 | 失败阶梯 + 断点续跑 | recovery | T1.8 | 1.5 |
| T2.4 | 干跑 + 体检报告 | dry-run | T2.1 | 1.0 |
| T2.5 | 诊断包 + 并排对比（基础版） | 诊断 | T2.3 | 1.5 |
| T2.6 | 定位命中统计 + 埋点 | repeat_step_hits | T2.1 | 1.0 |
| T2.7 | tkinter 样例应用 + e2e 测试 | CI 闭环 | T1.9 | 1.5 |
| T3.1 | 画布 `gui` 节点属性面板 | 前端 | T1.9 | 2.0 |
| T3.2 | 步骤参数化 + 变量注入 | 参数 | T1.7 | 1.0 |
| T3.3 | 循环检测 → `condition` 展开 | 编译增强 | T3.2 | 1.5 |
| T3.4 | scheduler `repeat` kind + 自动干跑 | 定时 | T2.4 | 1.0 |
| T3.5 | 断点续跑 UI + 运行历史 | 前端 | T2.3 | 1.5 |
| T4.1 | SKILL.md 生成器 + 技能目录写入 | skill | T3.2 | 1.5 |
| T4.2 | `repeat_run` 工具接入对话 | 工具 | T4.1 | 1.0 |
| T4.3 | LLM 语义命名（可选降级） | semantic | T4.1 | 1.0 |
| T4.4 | 隐私屏蔽全量 + 密码流程 | privacy | T1.3 | 1.0 |
| T5.1 | 导入安全扫描 | 扫描器 | T4.1 | 1.5 |
| T5.2 | 引导式修复界面 | 前端 | T2.5 | 2.0 |
| T5.3 | 命中率看板 + 脆弱性告警 | insights | T2.6 | 1.5 |
| T5.4 | 跨机迁移检查 + 环境差异告警 | envcheck | T2.2 | 1.0 |
| T5.5 | 独立包切分 + CLI + README + 演示 GIF | `andy-repeat` | M1 末 | 2.0 |

合计 **44 人日**（T0–T5 逐项相加，不含评审、联调与真机回归；按 25% 缓冲计约 55 人日）。

### 13.3 排期建议

- 第 1 周：T0.1–T0.3 + T1.1–T1.3（先把"能录"跑通）；
- 第 2 周：T1.4–T1.6（把"录得准"跑通）；
- 第 3 周：T1.7–T1.9（闭环 + 界面）；
- 第 4–5 周：M2 全部；
- 第 6–7 周：M3；
- 第 8 周：M4 + M5 前半；
- 第 9 周：M5 后半 + 开源切分。

### 13.4 M1 完成定义（DoD）

1. 能在真实桌面录制一段包含点击、输入、切窗口的 2 分钟操作；
2. bundle 结构完整，manifest 校验通过；
3. 编译产出工作流草稿，含定位链与置信度；
4. 回放成功，且每步都有 `hit_index` 与耗时；
5. 面板可看会话列表、步骤时间线、运行状态；
6. 单测覆盖存储/编译/执行核心路径，CI 绿；
7. 文档：README 增补"操作录制"章节，CHANGELOG 记录，工作日志留痕。

---

## 14. 风险登记册

| # | 风险 | 概率 | 影响 | 缓解 |
|---|---|---|---|---|
| R1 | 智能体沙箱拦截钩子/注入（项目曾拦 Frida、句柄删除） | 中 | 高 | M0 首先实测；受限则 e2e 改在真实后端（start-all.bat / Tauri 壳）执行，沿用 REA 先例 |
| R2 | Chromium UIA 首查过慢导致大面积降级 | 高 | 中 | 预热 + 300 ms 超时 + 图像锚点兜底 + 命中率看板引导补锚点 |
| R3 | 杀软误判全局钩子 | 中 | 中 | 不驻留、仅录制期安装、文档声明、提供免安装独立包 |
| R4 | 密码/敏感信息泄露 | 低 | 极高 | 前置屏蔽 + 零明文验收（A6）+ 代码评审专项 |
| R5 | 坐标盲点造成误操作 | 中 | 高 | 环境闸门（ADR-05）+ 默认拒绝 + 危险词库升级 risk |
| R6 | 长流程帧体积膨胀 | 中 | 中 | 三档留证 + GC + 单 bundle 上限 |
| R7 | UIPI 限制导致关键流程不可用 | 高 | 中 | 明确边界、提前检测、引导以同级权限运行；不承诺覆盖 |
| R8 | 输入法与控件过滤导致文本错误 | 中 | 中 | IME 归并 + 三通道注入 + 读回校验 + 失败挂起 |
| R9 | 多显示器/DPI 组合爆炸 | 中 | 中 | 三空间坐标 + L0–L3 分级 + 只保证 L0/L1 的 coord |
| R10 | 回放与用户抢输入 | 中 | 中 | 输入租约 + 人工介入检测 + 紧急停止 |
| R11 | Schema 演进破坏旧数据 | 低 | 中 | 版本窗口 + 迁移器 + 金样例测试 |
| R12 | 与工作流引擎耦合过深难抽包 | 中 | 中 | 依赖倒置 + import 禁令 + M1 末试切分 |

---

## 15. 附录

### A. 事件 JSON Schema（v1）

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "$id": "https://andy-harness.dev/schemas/repeat/event-v1.json",
  "title": "RepeatEvent",
  "type": "object",
  "required": ["v", "seq", "t", "ts", "type", "src", "data"],
  "additionalProperties": false,
  "properties": {
    "v": {"const": 1},
    "seq": {"type": "integer", "minimum": 0},
    "t": {"type": "integer", "minimum": 0},
    "ts": {"type": "integer", "minimum": 0},
    "type": {
      "enum": ["session_start", "session_end", "pointer_move", "pointer_down", "pointer_up",
               "click", "double_click", "right_click", "middle_click", "drag", "scroll",
               "key", "text_input", "clipboard", "window_change", "marker",
               "pause", "resume", "discard"]
    },
    "src": {"enum": ["hook", "derived", "marker", "system"]},
    "pos": {
      "type": ["object", "null"],
      "required": ["x", "y", "space"],
      "properties": {
        "x": {"type": "integer"},
        "y": {"type": "integer"},
        "space": {"enum": ["phys", "logical"]},
        "monitor": {"type": "integer", "minimum": 1},
        "scale": {"type": "number", "exclusiveMinimum": 0},
        "rel": {"type": "array", "items": {"type": "number", "minimum": 0, "maximum": 1}, "minItems": 2, "maxItems": 2}
      },
      "additionalProperties": true
    },
    "win": {"type": ["object", "null"]},
    "uia": {"type": ["object", "null"]},
    "data": {"type": "object"},
    "flags": {"type": "object", "additionalProperties": {"type": "boolean"}}
  }
}
```

### B. 错误码全表

| 码 | 名称 | 域 | 可恢复 | 默认策略 |
|---|---|---|---|---|
| RP-1001 | 无图形会话 | 环境 | 否 | 中止 |
| RP-1002 | 目标进程未运行 | 环境 | 是 | 启动或挂起 |
| RP-1003 | 目标窗口未找到 | 环境 | 是 | 重试→挂起 |
| RP-1004 | 显示器拓扑变化 | 环境 | 是 | 降级/挂起 |
| RP-1005 | 缩放导致逻辑分辨率变化 | 环境 | 是 | 禁用 coord |
| RP-1006 | 应用无响应 | 环境 | 是 | 中止+快照 |
| RP-1007 | 意外弹窗 | 环境 | 是 | 挂起 |
| RP-1008 | 窗口不可见（cloaked/其他桌面） | 环境 | 是 | 挂起 |
| RP-2001 | 定位链全部失效 | 定位 | 是 | 重试→挂起 |
| RP-2002 | UIA 查询超时 | 定位 | 是 | 跳过该策略 |
| RP-2003 | 图像锚点未匹配 | 定位 | 是 | 降级 |
| RP-2004 | 目标被遮挡 | 定位 | 是 | 降级/挂起 |
| RP-2005 | 目标不可用 | 定位 | 是 | 挂起 |
| RP-2006 | 坐标兜底被闸门禁用 | 定位 | 是 | 挂起 |
| RP-3001 | 前置等待超时 | 时序 | 是 | 重试→挂起 |
| RP-3002 | 断言失败 | 时序 | 视策略 | 按 on_fail |
| RP-3003 | 步骤预算耗尽 | 时序 | 是 | 中止 |
| RP-3004 | 界面持续变化 | 时序 | 是 | 继续并标记 |
| RP-4001 | UIPI 拦截 | 权限 | 否 | 中止 |
| RP-4002 | 安全策略拦截 | 权限 | 否 | 中止 |
| RP-4003 | 审批拒绝/超时 | 权限 | 是 | 中止 |
| RP-4004 | 密码字段未授权 | 权限 | 是 | 挂起 |
| RP-5001 | 前台激活失败 | 输入 | 是 | 挂起 |
| RP-5002 | 剪贴板失败 | 输入 | 是 | 降级注入 |
| RP-5003 | 输入结果不一致 | 输入 | 是 | 重试→挂起 |
| RP-5004 | 输入租约被占用 | 输入 | 是 | 拒绝 |
| RP-6001 | bundle 校验失败 | 数据 | 否 | 拒绝 |
| RP-6002 | Schema 版本不兼容 | 数据 | 否 | 拒绝 |
| RP-6003 | 事件流截断 | 数据 | 是 | 丢弃末行 |
| RP-6004 | 流程定义非法 | 数据 | 否 | 拒绝 |
| RP-7001 | 用户紧急停止 | 系统 | 是 | 中止 |
| RP-7002 | 检测到人工介入 | 系统 | 是 | 暂停→挂起 |
| RP-7003 | 内部异常 | 系统 | 是 | 中止+诊断 |
| RP-7004 | 录制器失联 | 系统 | 是 | 结束录制+truncated |
| RP-8001 | 必填参数缺失 | 参数 | 是 | 拒绝启动 |
| RP-8002 | 参数格式非法 | 参数 | 是 | 拒绝启动 |
| RP-8003 | 无人值守遇 runtime_only 参数 | 参数 | 是 | 拒绝启动+通知 |

### C. 配置项参考

| 键 | 默认 | 说明 |
|---|---|---|
| `repeat.frame_tier` | `standard` | minimal / standard / forensic |
| `repeat.countdown_s` | 3 | 倒计时秒数，0 为立即开始 |
| `repeat.uia_enabled` | true | 关闭后只采坐标与图像 |
| `repeat.uia_timeout_ms` | 300 | 硬超时 |
| `repeat.capture_clipboard` | false | 是否记录剪贴板文本 |
| `repeat.mask_windows` | `[]` | 标题正则/进程名列表 |
| `repeat.mask_keys` | `[]` | 不采内容的组合键 |
| `repeat.coord_gate` | true | 坐标兜底闸门 |
| `repeat.allow_password_replay` | false | 密码字段自动回放（不建议） |
| `repeat.step_timeout_ms` | 12000 | 单步超时 |
| `repeat.run_timeout_min` | 30 | 运行超时 |
| `repeat.retries` | 2 | 单步重试次数 |
| `repeat.retry_backoff_ms` | 800 | 退避基数 |
| `repeat.max_retries_per_run` | 20 | 运行级重试上限 |
| `repeat.emergency_hotkey` | `ctrl+alt+.` | 紧急停止 |
| `repeat.retention_days.standard` | 30 | 标准帧保留 |
| `repeat.retention_days.forensic` | 7 | 取证帧保留 |
| `repeat.bundle_max_mb` | 500 | 单 bundle 上限 |
| `repeat.llm_semantic` | false | 是否启用 LLM 语义命名 |
| `repeat.params_save_last` | true | 前端记住上次非敏感参数输入（本地草稿） |
| `repeat.unattended_policy` | `reject` | 无人值守遇 runtime_only 参数：`reject`（默认，RP-8003）/ `todo`（转待办不占租约） |
| `repeat.prompt.pre_run_notice` | true | 回放执行前告知横幅（§9.5.2） |
| `repeat.prompt.pre_run_ready_s` | 3 | 执行前准备秒数，0 为仅横幅不等待 |
| `repeat.prompt.skip_notice_unattended` | true | 无人值守时跳过前置横幅（结果回执仍必发） |
| `repeat.prompt.result_banner_s` | 5（录制结束横幅 8） | 结果横幅驻留秒数 |
| `repeat.prompt.suppress_when_fullscreen` | true | 用户全屏专注时结果横幅降级为托盘+通知中心 |
| `repeat.prompt.notify_channel` | `""` | 无人值守结果外推渠道（channel_manager 渠道 id，留空不外推） |

### D. 与其他模块的契约变更清单

| 变更 | 文件 | 类型 | 影响 |
|---|---|---|---|
| 新增表 `repeat_sessions` | `harness/infra/database.py` | 建表 | 无 |
| 新增表 `repeat_runs` | 同上 | 建表 | 无 |
| 新增表 `repeat_step_hits` | 同上 | 建表 | 无 |
| 新增 `gui` 节点分派 | `harness/modules/workflows/engine.py` | 加分支 | 不改既有节点 |
| 新增路由器 | `harness/api/app.py`（或路由聚合处） | 注册 | 无 |
| 新增 optional extra `repeat` | `backend/pyproject.toml` | 依赖 | 无（缺失优雅降级） |
| 新增插件 | `backend/plugins/andy_repeat/` | 新目录 | 无 |
| 新增前端路由 `/repeat` | `frontend/src/router` | 路由 | 无 |
| 新增 `harness/infra/notify.py`（NotifyService：托盘/横幅/系统通知） | `harness/infra/` | 新文件 | 无（缺失时提示降级为仅日志+SSE，见 §9.5.4） |

### E. 依赖清单

```toml
[project.optional-dependencies]
repeat = [
  "pynput>=1.7.6",          # 全局输入钩子
  "pyautogui>=0.9.54",      # 坐标注入回退（已有 desktop extra）
  "mss>=9.0.1",             # 截图（已有）
  "uiautomation>=2.0.18",   # UIA 薄封装（comtypes 之上）
]
repeat-image = [
  "opencv-python-headless>=4.9",  # 多尺度模板匹配
]
```

缺失 `repeat` extra 时：插件加载成功但 `available=false`，面板显示"录制依赖未安装"，工具返回明确提示而非抛栈。

---

## 结语

本设计的取舍可以压缩为三句话：

1. **数据决定上限**：事件 + 语义快照 + 锚点三元证据，是回放能做到"确定性"的唯一基础；视频与模型都不进入主路径。
2. **状态机决定下限**：三级状态机把"录制中崩溃""恢复后重跑""失败后挂起"这些真实场景变成有限、可穷举、可测试的路径。
3. **异常处理决定可用性**：14 个边界场景与 37 个错误码不是为了完备性表演，而是为了让每一次失败都能回答"哪一步、为什么、下一步怎么办"。

下一步：执行 M0 Spike，用真机数据确认钩子/UIA/注入的可用性边界，再据实校准 §11 的预算数值。
