# andy-repeat —— 录屏即技能：自研桌面操作录制与智能回放方案

> **文档性质**：产品与工程落地方案（v2，取代《桌面自动化精灵-方案设计.md》作为实施依据）。
> **硬约束**：**不 fork、不二次开发任何应用级开源工程**，全部核心代码自研；仅使用操作系统 API 与基础依赖库（等价于"用 Windows API"，不构成二开）。
> **目标**：能力先行在 andy-harness 内孵化（插件形态），成熟后抽离为独立 Python 包 `andy-repeat` 开源到 GitHub。
> **日期**：2026-10-18 · 适用仓库 `H:/jsd-andyharness`（Windows 10 实测环境）。

---

## 目录

1. [对参考文档与开源调研的结论](#一对参考文档与开源调研的结论)
2. [产品定位与设计公理](#二产品定位与设计公理)
3. [总体架构与代码形态](#三总体架构与代码形态)
4. [核心数据契约](#四核心数据契约)
5. [录制器设计](#五录制器设计)
6. [解释器设计（事件流 → 语义步骤）](#六解释器设计事件流--语义步骤)
7. [定位策略链设计](#七定位策略链设计)
8. [执行器与异常恢复](#八执行器与异常恢复)
9. [AI 层：生成技能与失败自愈](#九ai-层生成技能与失败自愈)
10. [安全与隐私](#十安全与隐私)
11. [与 andy-harness 的集成点](#十一与-andy-harness-的集成点)
12. [里程碑](#十二里程碑)
13. [风险清单与 Windows 特有坑位](#十三风险清单与-windows-特有坑位)
14. [开源化与仓库规划](#十四开源化与仓库规划)
15. [测试策略](#十五测试策略)
16. [附录：技术选型总表](#十六附录技术选型总表)

---

## 一、对参考文档与开源调研的结论

### 1.1 《桌面自动化精灵-方案设计.md》评审

该文档质量较高，以下结论**予以保留并作为本方案的需求基线**：

| 保留结论 | 出处 | 本方案态度 |
|---|---|---|
| 录制是"证据采集"而非录像：结构化事件 + 界面语义 + 关键帧 | §4.2 设计要点 | 完全采纳，且作为第一设计公理 |
| 定位策略链：语义 → 图像 → 相对 → 坐标兜底 | §3.2 ③/§6.5 | 完全采纳，落为**可注册策略链**（对齐项目 `input_dispatch` 的双注册表范式） |
| 产物是工作流而非黑盒宏；运行绑定不可变版本 | §1.1/§6.3 | 采纳，并直接复用 `GraphEngine` 节点契约而非另造步骤模型 |
| 干跑 / 定位体检 / 单步 / 断点续跑 | §3.2 ④ | 采纳，M2 起落地 |
| 风险分级、护栏唯一出口、隐私前置屏蔽（非事后脱敏） | §5.3 | 采纳，复用 `computer_use/safety.py` 三层防护 |
| 残余风险声明："把失效变得可预测、可发现、可低成本修复" | §7.7 | 采纳为产品文案基调 |

以下结论**需要修正**：

| 原方案表述 | 问题 | 修正 |
|---|---|---|
| "跨桌面平台兼容性"列为非功能硬要求（§5.1） | 项目 AGENTS.md 明确"Windows 10 开发与验证、跨平台风险未排除"；三平台适配层在 M1–M3 是纯负担 | **Windows-first**。仅在录制器/执行器边界保留接口抽象，macOS/Linux 适配推迟至 v2 开源后社区化 |
| 界面语义采集"CPU < 5% 无感"（B1） | 对 Chromium/Electron 应用，UIA 树查询代价高（首棵树可达秒级）；无策略地"同步捕获控件树"做不到低开销 | 改为**惰性快照**：只在点击/回车等关键事件时刻，单点查询（`ElementFromPoint`）+ 祖先链回溯，300ms 超时即降级为纯坐标；逐帧不采树 |
| 解释器"按业务意图切分步骤、识别循环、推断参数"一步到位（§3.2 ②） | 这是全系统最难的能力，规则式上限低，文档未给分期 | 拆成**规则版（确定性、离线、免费）→ LLM 增强版（可选）**两阶；MVP 只承诺规则版可达成的切分质量 |
| 数据模型全新设计（§6） | 项目已有 `workflows` / `artifact_store` / `evidence_v2` / `tracing` / `progress.py`（SSE 进度总线） | 定义层尽量映射既有契约；仅新增"录制会话 bundle"一种新数据形态；审计/留证可选挂 `evidence_v2` 哈希链 |
| "权限提升层不支持"只写在边界章节（§7.3/§7.6） | 这是 Windows UIPI 的**硬限制**，不是产品选择；必须在架构层面（进程完整性级别）说清楚 | 移入 §13 坑位清单，含检测与用户引导方案 |

### 1.2 开源调研结论与"自研"边界

先澄清一件事：**什么算"二开"，什么不算**。

- **应用级开源工程**（record-and-replay-skill、OpenAdapt、Microsoft Skill Recorder、pywinauto_recorder、TinyTask、action_recorder）：它们各自是完整的"录制器/编译器/产品"。**本方案全部不用其代码**，只借鉴被验证过的设计思想。
- **操作系统 API 与基础依赖库**（UI Automation COM 接口、Win32 低级钩子、`pynput`、`uiautomation`(Python 对 UIA 的薄封装)、OpenCV、pyautogui）：这是任何同类实现的必用地基，属于**依赖**而非二开，与"用 requests 发 HTTP"同性质。andy-harness 现有 `computer_use` 插件本来就已依赖 pyautogui/mss，不改变。

对调研对象的**设计思想采纳**如下：

| 调研对象 | 值得采纳的思想 | 本方案落点 |
|---|---|---|
| Codex Record & Replay / record-and-replay-skill | 记事件流不记视频；产物是"带语义定位符的技能"，不是像素宏 | §4 数据契约 + §9 技能生成 |
| OpenAdapt | "录制一次 → 编译成确定性可自愈回放"；capture 与 flow 两组件分离 | 录制器/解释器/执行器三进程·三阶段分离（§3） |
| pywinauto_recorder | 不生成硬编码坐标、按控件定位——验证了 UIA 语义定位方向正确 | §7 定位链；但我们比它多做**链式降级**而非单策略 |
| Microsoft Skill Recorder | 录制期口述 → 语音转写并入意图 | 列为 M4+ 可选增强（本地 whisper），不进 MVP |
| TinyTask / action_recorder | 覆盖层交互（倒计时/热键/暂停）的轻量体验；视频+事件双记录 | §5.3 录制覆盖层体验；视频仅作为可选留证附件（M3） |

**结论**：调研没有给出可直接依赖的"Windows + UIA + 语义链 + 与 Agent Harness 集成"的组合方案，自研路线成立，且差异化明确（见 §2.3）。

---

## 二、产品定位与设计公理

### 2.1 命名与一句话定义

**andy-repeat**：**演示一遍，得到一个可离线确定性回放、可 AI 修复、可封装成技能的桌面流程**。

- 在 andy-harness 中：`desktop_repeat` 能力模块 + `andy_repeat` 插件（工具 + REST + 前端面板）。
- 独立形态：`andy-repeat` Python 包（`pip install andy-repeat`，自带 CLI：`repeat record / interpret / replay / skill`）。

### 2.2 四条设计公理

1. **录制即取证**。保存的是"事件流 + 点击时刻的界面语义 + 关键帧"三元证据，绝不依赖逐帧视频做回放。
2. **回放主路径不依赖 AI**。解释与定位是确定性的（规则 + 策略链）；无模型、断网、低配机器照常回放。AI 只做两件事：步骤语义命名（增强可读性，可跳过）与失败时的修复建议（辅助人工）。这决定产品可信赖性——与 OpenAdapt 式"VLM 全程在场"形成差异。
3. **一切动作经护栏唯一出口**。回放执行不调 pyautogui 裸函数，而是调 `computer_use` 的 `DesktopController` + `SafetyPolicy`（独立包形态下保留同名抽象），风险分级、白名单、审批沿用既有机制。
4. **失效是常态，工程目标是把失效变成"可预测、可定位到步骤、可低成本修复"**（继承原方案 §7.7），具体手段：干跑体检、失败快照对比、引导式重选目标、定位命中率统计驱动的脆弱性告警。

### 2.3 与调研对象的差异化定位

| 维度 | TinyTask 类宏 | record-and-replay-skill | OpenAdapt | **andy-repeat** |
|---|---|---|---|---|
| 回放机制 | 坐标宏 | Agent 现写脚本 | VLM 逐步决策 | **确定性策略链回放** |
| 断网可用 | ✅ | ❌（依赖 Agent） | 部分 | ✅（主路径） |
| Token 成本 | 0 | 每次回放开销 | 高 | **回放 0，仅修复时按需** |
| 产物形态 | .exe 宏 | SKILL.md | 流程模型 | **工作流(graph) + 可选 SKILL.md 封装** |
| 控件语义定位 | ❌ | 部分（macOS AX） | ❌（视觉） | ✅ UIA 全链 |
| 与 Agent/调度/审批集成 | ❌ | 松 | ❌ | ✅ 原生（harness 插件） |
| 失败自愈 | ❌ | ❌ | ✅但黑盒 | ✅ 白盒：降级→告警→AI 建议→人工确认 |

---

## 三、总体架构与代码形态

### 3.1 进程视图（三进程分离）

```
┌─────────────────────────────────────────────────────────────┐
│ backend 进程 (FastAPI)                                       │
│  ├─ modules/desktop_repeat/   核心逻辑（纯 Python，无 IO 副作用）│
│  │    recorder/  interpreter/  locators/  executor/  skill/  │
│  ├─ api/rest/desktop_repeat.py  REST + SSE（复用 progress 范式）│
│  └─ plugins/andy_repeat/      工具注册 + 装配 + plugin.json    │
└──────────────┬──────────────────────────────────────────────┘
               │ Popen（复用 infra/subproc.py）
   ┌───────────▼────────────────┐        ┌─────────────────────────┐
   │ recorder 子进程（独立）      │        │ executor 运行任务         │
   │  · pynput 全局钩子          │        │ （backend 内 asyncio 任务，│
   │  · UIA 惰性快照             │        │   非独立进程，便于接护栏） │
   │  · mss 关键帧               │        │  · DesktopController      │
   │  · tkinter 覆盖层           │        │  · SafetyPolicy           │
   │  · 直写 bundle JSONL        │        │  · 步骤级截图/日志         │
   └────────────────────────────┘        └─────────────────────────┘
```

**录制器为什么必须是子进程**：
1. 崩溃隔离（公理：采集异常不得导致宿主不可用）——钩子回调里任何异常只死掉 recorder，不影响 backend；
2. 钩子回调对延迟敏感（低级钩子超时会被系统摘除），必须避开 backend 的 asyncio 抖动；
3. DPI 感知可控：recorder 进程启动即设 `SetProcessDpiAwarenessContext(PER_MONITOR_V2)`，保证采集坐标与 UIA 逻辑坐标一致；
4. 可控保活/回收：与 `browser_daemon` 插件同范式（后端 Popen detached 子进程 + 状态复核）。

### 3.2 目录规划（harness 内孵化期）

```
backend/harness/modules/desktop_repeat/     # 核心：不 import fastapi，可整体抽包
├── __init__.py
├── models.py            # RecordingEvent / StepDefinition / LocatorChain / RepeatBundle 等 dataclass
├── recorder/
│   ├── runner.py        # 子进程入口：python -m ...recorder.runner --bundle <dir> --config <json>
│   ├── capture.py       # pynput 监听 → 归一化事件（逻辑坐标/DPI/修饰键）
│   ├── uia.py           # UIA 惰性快照（ElementFromPoint + 祖先链 + 属性摘要）
│   ├── frames.py        # mss 关键帧策略（前后帧/窗变帧/差分判稳）
│   ├── overlay.py       # tkinter 覆盖层：倒计时、红点状态、热键、暂停/丢弃
│   └── privacy.py       # 前置屏蔽：IsPassword 控件、屏蔽窗口列表、按键类型黑名单
├── interpreter/
│   ├── pipeline.py      # 事件流 → 步骤序列（规则版，确定性的）
│   ├── merge.py         # 去噪归并：抖动过滤/击键聚合/拖拽滚轮归并
│   ├── waits.py         # 等待点推断（基于帧差分与窗口激活时刻）
│   └── semantic.py      # LLM 语义命名与参数建议（可选，走 model_manager）
├── locators/
│   ├── registry.py      # @register_locator 双注册表（对齐 input_dispatch 范式）
│   ├── uia_chain.py     # automation_id / name+control_type / structure_path / class+index
│   ├── image_anchor.py  # 模板匹配（多缩放），锚点小图存 bundle
│   ├── relative.py      # 相对锚点（窗口客户区比例/父容器偏移）
│   └── coordinate.py    # 绝对坐标兜底（逻辑坐标 + 缩放因子 + 屏幕几何）
├── executor/
│   ├── engine.py        # RepeatRunner：按序驱动步骤，产出 StepRun 记录
│   ├── actions.py       # click/type/hotkey/scrolldrag/waitwindow/assert 动词 → DesktopController/UIA
│   ├── recovery.py      # 失败阶梯：重试→降级定位→放宽→跳过→挂起人工→中止取证
│   └── pacing.py        # 节拍/三级超时/输入设备独占队列
├── skill/
│   └── generator.py     # 工作流 → SKILL.md（scripts/replay.py 调本地引擎）
├── store.py             # bundle 读写（目录制 + JSONL + SHA256 manifest）
└── service.py           # DesktopRepeatService：录制会话 CRUD、解析、运行编排、SSE 进度

backend/plugins/andy_repeat/
├── plugin.json
├── main.py              # 装配：注册服务/工具/REST
└── tools.py             # 工具：repeat_record_start/stop、repeat_run、repeat_dry_run…

backend/harness/api/rest/desktop_repeat.py   # REST + SSE
frontend/src/views/RepeatStudioView.vue      # 录制/解析/回放工作台（/repeat）
```

独立开源期（§14）：`desktop_repeat/` 原样平移为 `andy_repeat` 包，harness 侧改为薄依赖。

---

## 四、核心数据契约

### 4.1 bundle：录制会话（自包含目录，git 友好可选）

```
<artifact_store>/repeat/sessions/<session_id>/
├── session.json      # 元信息：环境基线(分辨率/DPI/缩放/OS/显示器数)、采集配置、完整性标记
├── events.jsonl      # 原始事件流（每行一个归一化事件，追加写，崩溃可恢复截断点）
├── frames/           # 关键帧 PNG（命名 <event_seq>-<pre|post>.png）
├── anchors/          # 图像锚点小图（解释器裁剪产物，回放期生成）
└── manifest.json     # 所有文件的 {path, sha256, size}；审计模式下转 evidence_v2 哈希链
```

**原始事件 schema（events.jsonl 单行）**：

```json
{
  "seq": 143,
  "t_ms": 1721300451234,
  "type": "click",                     // move|down|up|click|double_click|right_click|key|shortcut|scroll|drag|window_change|marker|pause|discard
  "pos": {"x": 812, "y": 377, "dpi_scaled": true},
  "button": "left",
  "key": null, "modifiers": ["ctrl"],
  "window": {"title": "订单管理 - ERP", "cls": "Chrome_WidgetWin_1",
             "pid": 12044, "exe": "erp.exe", "rect": [200,120,1400,900]},
  "uia": {                              // 惰性快照，超时/失败则为 null 并记 "uia_timeout": true
    "control_type": "Edit",
    "name": "客户名称",
    "automation_id": "txtCustName",
    "class_name": "Chrome_RenderWidgetHostHWND",
    "structure_path": "Window>/Pane>/Document>/Edit[3]",
    "value_hint": null,
    "is_password": false,
    "ancestor_chain_len": 6
  },
  "screen": {"w": 1920, "h": 1080, "scale": 1.0, "monitor": 1}
}
```

要点：
- **时间戳毫秒单调 + seq 双序**，回放节拍与切分都靠它；
- `uia` 只存**定位所需最小属性集**，不存全树（体积与耗时都不允许）；
- `is_password=true` 的控件：按键内容**不进管道**（前置屏蔽），只记"在某密码框输入了 N 个字符"占位，回放时该步骤强制设计为**人工确认节点**。

### 4.2 工作流：录制的产物

复用现有 `workflows` 表与 `GraphEngine` 图契约，新增 **`gui` 节点类型**：

```json
{
  "id": "n7", "type": "gui",
  "position": {"x": 320, "y": 240},
  "data": {
    "action": "type",                          // click|double_click|right_click|type|hotkey|scroll|drag|window_focus|minimize|close|wait_window|wait_element|assert_element|assert_text|manual_confirm|marker
    "label": "在【客户名称】输入框填入 {{param.cust_name}}",   // 解释器生成（LLM 增强）
    "target": {                                // 定位策略链（有序，首个命中即用）
      "locators": [
        {"kind": "uia", "by": "automation_id", "value": "txtCustName", "timeout_s": 3, "confidence": 0.98},
        {"kind": "uia", "by": "name_type", "value": {"name": "客户名称", "control_type": "Edit"}, "timeout_s": 2, "confidence": 0.9},
        {"kind": "uia", "by": "structure_path", "value": "Window>/Pane>/Document>/Edit[3]", "timeout_s": 2, "confidence": 0.7},
        {"kind": "image", "anchor_ref": "anchors/n7.png", "threshold": 0.82, "confidence": 0.6},
        {"kind": "relative", "anchor": "window:ERP主窗口", "ratio": [0.45, 0.28], "confidence": 0.5},
        {"kind": "coord", "pos": [812, 377], "scale": 1.0, "screen": [1920,1080], "confidence": 0.3}
      ],
      "app_hint": {"exe": "erp.exe", "window_title_regex": "订单管理.*"}
    },
    "input": {"mode": "clipboard_paste", "value": "{{param.cust_name}}"},  // clipboard_paste|uia_set_value|keystroke(兜底)
    "pre_wait": {"kind": "element_appear", "locator_ref": 0, "timeout_s": 8},
    "post_assert": null,
    "risk": "write",
    "on_fail": {"strategy": "ladder", "retries": 2, "backoff_ms": 800, "then": "suspend_manual"},
    "confidence": 0.9,
    "needs_review": false,
    "evidence": {"session_id": "s8f2a…", "event_range": [141, 156]}   // 可回溯到录制片段
  }
}
```

- `start/end/condition/assign` 等控制节点直接用现有类型，循环用 `condition + assign` 组合表达（M3 再考虑专用 `loop` 节点）；
- 录制产物**保存为一个新的 workflow 行**（`description` 带 `source_session` 元数据），天然获得画布编辑、版本、导入导出现状能力；
- **定位器命中统计回填**：每次运行把 `hit_locator_index` 写进 StepRun，供"策略自优化 + 脆弱性告警"（见 §8.4）。

### 4.3 运行记录

步骤级留证直接复用 `workflow_runs` 扩展字段 + `artifact_store`（截图）+ `tracing`（span）；不另造一套 run 表。SSE 进度直接复用 `reverse_engineering/progress.py` 的 `ProgressStore/ProgressReporter` 范式（含那两个已修复的坑：回放快照用 `list(...)`、终止事件立即 return）。

---

## 五、录制器设计

### 5.1 事件采集（capture.py）

- `pynput.mouse.Listener` + `pynput.keyboard.Listener` 全局低级钩子；回调线程**只做归一化 + 入队**，重活（UIA 查询、截图）在 worker 线程做，保证钩子回调 <1ms（Windows 低级钩子超 300ms 会被系统摘除，留 300 倍余量）。
- 归一化内容：逻辑坐标（除以 DPI 缩放）、修饰键状态快照、双击识别（两次 down/up 间隔 <500ms 且位移 <4px）、拖拽识别（down→move*→up 合成 drag 事件）、滚轮步数累计。
- 移动事件**不逐条记**：只记"点击/回车前的最后一段移动轨迹摘要"（起点/终点/时长），既省存储又够回放参考（回放本就不该逐像素移动鼠标，见 §8.2）。
- 高频移动降采样 + 背压：队列满先丢 move 摘要、绝不丢 click/key/window_change。

### 5.2 UIA 惰性快照（uia.py）

仅在 `click / double_click / right_click / key(enter|tab) / window_change` 时刻触发：

1. `IUIAutomation.ElementFromPoint(x, y)` 取最深元素；
2. `TreeWalker.GetParent` 回溯 ≤8 层，记录祖先链 control_type/name 摘要（供 structure_path）；
3. 抽取最小属性集（§4.1 schema）；
4. **300ms 超时硬截止**：超时记 `uia_timeout=true`、`uia=null`——解释器据此把该步骤置信度压低并只生成 coord/image 策略；
5. 对 Chromium 系（class 含 `Chrome_RenderWidgetHostHWND`）：首查慢，录制开始事件先对前台窗口做一次预热查询；属性仍取（UIA 对 Chrome 暴露的可访问性节点质量已足够定位用）。

依赖：`uiautomation`（MIT，纯 Python comtypes 封装）或直用 `comtypes` + `UIAutomationCore`。选型倾向 `uiautomation`（省胶水代码），两者都在子进程内使用，不影响 backend。

### 5.3 关键帧策略（frames.py）

| 触发 | 内容 |
|---|---|
| 每个语义事件前 | 全屏 1 帧（JPEG q80 + 最长边 1920 归一；`精简` 档可只存点击点周围 400×300 裁剪） |
| 事件后 400ms | 1 帧"效果帧"（解释器用它推断等待点与断言） |
| window_change | 前后各 1 帧 |
| 用户标记键 | F10 打 marker 事件（分段注释） |

三档留证策略（继承原方案 §5.2）：`精简`（仅锚点裁剪图）/ `完整`（默认）/ `仅失败`（录后丢弃帧只留结构）——按会话配置。

### 5.4 覆盖层与操作体验（overlay.py）

- 启动录制：REST/工具触发 → backend Popen recorder 子进程 → tkinter 全屏半透明遮罩 **3-2-1 倒计时**（借鉴 TinyTask 体验），遮罩期间自身窗口 `ClickThrough`，不吞用户事件；
- 角落状态胶囊：红点 + 已录时长 + 事件数；
- 全局热键（pynput 注册）：`Ctrl+Alt+R` 开始/结束、`Ctrl+Alt+P` 暂停/恢复、`Ctrl+Alt+X` 丢弃本次；
- 结束：完整性预检（事件非空、manifest 校验、无钩子中断告警）→ 写 `session.json` 状态 → 子进程退出；
- 明确提示：**录制期间请勿在管理员权限窗口操作**（UIPI 限制，见 §13），录到"疑似提升窗口"（`GetWindowThreadProcessId` + 完整性检测失败）时给 marker 告警。

### 5.5 隐私屏蔽（privacy.py）——前置，非事后

- `is_password=true` 控件的后续按键内容不采集（只记长度占位）；
- 会话配置 `mask_windows[]`（标题正则/进程名）：命中窗口的关键帧**不截图**、UIA 不查询；
- 会话配置 `mask_keys[]`：如明确禁采剪贴板内容（本期只记"发生过复制"事件，不取内容）。

---

## 六、解释器设计（事件流 → 语义步骤）

### 6.1 规则版管线（pipeline.py，确定性、离线）

```
events.jsonl
  → ① 过滤：纯 move/修饰键单独按下(非组合键成分)/系统噪声(输入法候选窗键)
  → ② 归并：连续 key→type 段落(以焦点控件为界)；down+move+up→drag；连续 scroll→scroll_to
  → ③ 窗口绑定：window_change 生成 focus_window 步骤；跨窗口事件自动分组 app_hint
  → ④ 定位链生成：每个目标按 uia 快照属性装配有序链（§7.2 打分规则）
  → ⑤ 等待推断：pre/post 帧差分收敛时刻 → 相邻步骤间隔 >1.5s 或界面持续变化 → 插入 wait_element/wait_window（固定时延仅兜底）
  → ⑥ 断言建议：post 帧相对 pre 无变化的点击 → 标 "疑似无效点击，可删"；有输入框值变化 → assert_text 建议（默认不启用）
  → ⑦ 循环检测：连续 ≥3 次"同 structure_path 前缀 + 递增 Edit[i]/ListItem[i]"子序列 → 提示可参数化循环（needs_review）
  → ⑧ 参数建议：type 内容匹配日期/数字/路径/邮箱正则 → 建议提升为 start 节点参数（needs_review）
  → ⑨ 风险标注：目标控件/窗口标题命中危险词库(删除|提交|支付|发送|格式化) → risk 升级、on_fail=suspend_manual、默认 needs_review=true
输出：workflow graph 草稿（gui 节点序列）+ 「待确认清单」
```

低置信步骤（`uia=null`、锚点裁剪过小、循环/参数推断）在画布上高亮——**"明确指出哪里需要改"优先于"全自动"**（继承原方案 §4.4）。

### 6.2 LLM 增强版（semantic.py，可选）

把规则版步骤序列（脱敏后的 label 原料：控件 name、窗口标题、输入内容摘要）交给 `model_manager` 注册的 Provider，一次调用产出：
- 每步骤业务化命名（"填入客户名称"而非"点击 Edit"）；
- 步骤分组与流程标题、描述；
- 参数命名建议。

约束：LLM 只**改写 label 和元信息**，不允许增删步骤、不允许改定位链——防止模型幻觉破坏可执行性。无 Provider 时静默跳过（降级原则）。

---

## 七、定位策略链设计

### 7.1 注册表范式

```python
@register_locator("uia")          # kind 字符串可扩展
class UiaLocator:
    def find(self, ctx: ExecContext, spec: dict) -> Hit | None: ...
    # Hit = Rect + element引用 + score + 诊断信息(尝试耗时/失败原因)
```

与 `input_dispatch` 同构：**核心执行器只遍历链，新增策略零侵入**。每个 locator 自带 `describe_failure()`，失败诊断进 StepRun。

### 7.2 策略集与打分规则（解释器装配链时使用）

| 优先级 | kind | 适用判断（解释器打分） | 备注 |
|---|---|---|---|
| 1 | `uia: automation_id` | 非空即高分（+0.4） | 最稳，Win32/WPF/Qt 普遍有 |
| 2 | `uia: name + control_type` | name 非空、非纯数字/日期(+0.3) | 文案变更风险，配模糊匹配（编辑距离≤2） |
| 3 | `uia: structure_path` | 祖先链完整(+0.15) | 布局改版易失效，做兄弟节点序号容差 ±1 |
| 4 | `uia: class_name + index` | 旧 Win32 应用 | 覆盖非 UIA 老程序 |
| 5 | `image` 锚点 | 前四者皆缺时补 | 400×300 全屏帧里裁剪目标周围 60–140px；多尺度模板匹配（应对缩放差异）；threshold 默认 0.82 |
| 6 | `relative` | 有稳定窗口锚 | 目标中心相对客户区比例坐标，抗窗口移动不抗改版 |
| 7 | `coord` 绝对 | 永远存在（兜底） | 附录制的 screen/scale；执行前环境校验不一致时**先告警**再用 |

链装配规则：得分 ≥0.7 的策略入链；`uia=null` 的事件只剩 5/6/7 且整步 confidence ≤0.5、强制 `needs_review`。

### 7.3 命中统计与自优化

StepRun 记 `hit_locator_index`。工作流级聚合：
- 某步总是第 5 位（image）才命中 → 提示"automation_id 失效，建议更新链首"；
- 链首命中率周环比骤降 → "疑似界面改版"告警（进 insights）。

---

## 八、执行器与异常恢复

### 8.1 运行编排

- `RepeatRunner` 作为 asyncio 任务在 backend 内运行（挂起/审批/事件回传都更方便）；
- **输入设备独占队列**：全局互斥，运行期拒绝第二个运行；提示用户"请勿动鼠标键盘"，并提供**紧急停止热键**（`Ctrl+Alt+.`，由 executor 自己的热键监听实现，不依赖被接管的前台）；
- 节拍：步骤间默认 300ms + 上一目标命中即走（不傻等）；步骤级/运行级/应用无响应三级超时（继承原方案 §5.5）；
- 执行前环境校验：会话状态（未锁屏 `WTSQueryUserToken` 侧证、有图形会话）、目标 exe 是否存在、屏幕几何与录制基线差异 >15% 时告警（coord 策略可能失效）。

### 8.2 动作动词（actions.py）

| 动作 | 主实现 | 兜底 |
|---|---|---|
| click | UIA `InvokePattern/TogglePattern/SelectionItemPattern`（**不依赖坐标、不抢前台**） | 元素中心点 `pyautogui.click` |
| type | `ValuePattern.SetValue`；不可用时 **剪贴板粘贴**（Ctrl+V，自动恢复原剪贴板） | 逐字符键入（强制先切英文输入法上下文） |
| hotkey | pyautogui `hotkey` | — |
| scroll/drag | 坐标式（UIA ScrollPattern 覆盖差） | — |
| window_focus | `SetForegroundWindow` + 激活校验 | 任务栏坐标点击 |
| wait/assert | UIA 存在性 + `Rect` 可见 + OCR/Value 文本比对 | 图像差分（"界面稳定"） |

**输入法坑位**：中文输入一律 clipboard_paste，杜绝 IME 候选窗干扰（继承原方案 §7.3，落成默认策略而非建议）。

### 8.3 失败阶梯（recovery.py）

```
步骤失败
 ├─ 可重试类(未就绪/瞬态遮挡)   → 退避重试(默认2次,800ms起)
 ├─ 定位失败                   → 沿链降级下一策略 → 末位仍失败:
 │                                ① coord 存在且屏幕基线一致 → 坐标兜底点一次
 │                                ② 否则按 on_fail: suspend_manual / abort
 ├─ 被 SafetyPolicy 拦截        → 直接中止 + 审计（绝不绕过；interactive 模式可人工放行后续跑）
 ├─ 断言失败                   → 按策略: 中止/跳补偿分支/人工
 └─ 应用无响应/系统错误         → 中止 + 现场快照(全屏帧+UIA摘要) + 告警 + 转 todo
```

- **干跑**：全链只做 `find()`，产出体检报告（每步命中第几策略/耗时/失败原因）；定时任务前自动先干跑（原方案 §7.1 "主动体检"落地形态）。
- **断点续跑**：`checkpoint.py` 现成机制挂接——运行状态含已完成步骤与变量池快照，从失败步骤恢复；幂等标记步骤（`idempotent=true`）恢复前强制做前置检查。

### 8.4 留证与复盘

`仅失败留证` 为默认档：失败瞬间全屏帧 + 目标区域裁剪 + 定位尝试清单（每策略耗时与诊断）+ 期望目标旧快照并排展示——**引导式修复界面 = 左"当时录到的"右"现在长这样"，用户点选新目标即重写链首**。

---

## 九、AI 层：生成技能与失败自愈

### 9.1 技能封装（对齐 Codex "录完变 Skill" 体验，接入 skill_manager）

`skill/generator.py` 把一个已发布工作流导出为 SKILL.md（三级渐进披露，L1 仅 ~100 token）：

```
skills/repeat-<slug>/
├── SKILL.md            # frontmatter: name/description(何时用)
│                       # 正文: 流程说明、参数表、"调 repeat_run 工具执行本技能"、注意事项
└── scripts/check.py    # L3: 可选前置检查脚本
```

对话中模型命中该 skill → 调插件注册的 `repeat_run(workflow_id, params)` 工具 → 确定性回放。**AI 只负责"想起该用哪个流程 + 填参数"，执行全程不碰模型**——回放零 token 成本，与 record-and-replay-skill"每次让 Agent 现写脚本"的形态本质不同，这是本项目最有传播力的差异点。

### 9.2 失败自愈（M4+，白盒）

挂起人工介入时，把「失败诊断包」（期望目标属性+旧快照、当前界面 UIA 摘要+新帧、失败步骤定义）交给多模态模型产出**修复建议 diff**（改哪个定位器、阈值、等待条件）；建议**必须人工确认**才写回新版本。永远不做"模型现场决策下一步业务动作"。

---

## 十、安全与隐私

| 面 | 机制 | 落点 |
|---|---|---|
| 动作出口 | 回放唯一经 `DesktopController`+`SafetyPolicy`；risk 分级沿用 read/write/dangerous | 复用 `computer_use`，plugin 形态下 executor 注入同一 controller |
| 作用域 | 工作流绑定 `app_hint` 白名单（exe/窗口标题正则）；越界窗口事件运行期拒发 | executor 环境校验 + safety 层双查 |
| 危险步骤 | 解释器风险标注 → 首次执行强制 interactive 授权；支持按步骤覆盖 | `on_fail=suspend_manual` + permission_manager 审批 |
| 凭据 | 密码框输入永不入库（前置屏蔽）；含敏感参数标记 `sensitive`，日志脱敏、值运行时注入 | 对齐 `security.py` 脱敏 |
| 采集授权 | 显式点击开始 + 倒计时 + 常驻红点 + 一键丢弃；数据全本地 | overlay 体验即授权 UI |
| 分发 | 导入他人工作流跑静态扫描（coord-only 步骤占比、dangerous 动作清单、app_hint 缺失告警）后明示风险再启用 | M5 |
| 审计 | 审计事件只追加；审计模式会话挂 `evidence_v2` 哈希链 bundle | store.py 可选开关 |

---

## 十一、与 andy-harness 的集成点

| 既有件 | 用法 |
|---|---|
| `computer_use/desktop.py` | executor 直接实例化 `RealDesktopController`（缺依赖时 `available=False` 优雅降级，整个面板显示"桌面操控未就绪"） |
| `computer_use/safety.py` | 动作下发前置门 |
| `workflows` | 产物存储/画布编辑/版本/导入导出现状能力；GraphEngine 新增 `gui` 节点 handler（`_run_node` 分派处） |
| `scheduler/runner.py` | 新增任务 kind `repeat`：无人值守直接跑工作流（先自动干跑），比"跑一段提示词"更稳 |
| `skill_manager` | §9.1 技能封装目标 |
| `artifact_store` | bundle 根目录 |
| `evidence_v2` | 审计模式链哈希 |
| `tracing`/`insights` | 步骤 span、成功率/命中率看板 |
| `todo_manager` | 挂起人工介入 → 待办 |
| `progress.py` 范式 | `GET /api/desktop-repeat/runs/{id}/events` SSE 流（复用两个已修坑位说明） |
| `model_manager` | §6.2/§9.2 可选 LLM |

REST 草案：`/api/desktop-repeat/sessions`(POST start / POST {id}/stop / GET / DELETE)、`/sessions/{id}/interpret`(POST→workflow 草稿)、`/runs`(POST)、`/runs/{id}/dry-run`、`/runs/{id}/resume|abort`、`/runs/{id}/events`(SSE)、`/workflows/{id}/skill`(POST 生成技能)。

---

## 十二、里程碑

| 阶段 | 交付 | 达成判据 | 预估工作量 |
|---|---|---|---|
| **M0 环境 Spike（先行，1~2 天）** | ① 沙箱外真机验证 pynput 钩子/UIA 查询/mss 截图不被拦截（项目沙箱曾拦 Frida 注入与句柄删除，钩子类预期可过但必须实测）；② 对记事本+一个 Electron 应用录放闭环 demo | 出书面结论：可用/受限清单 | S |
| **M1 录→放最小闭环（1~2 周）** | recorder 子进程 + bundle 存储 + 规则解释器 v0（click/type/hotkey，coord+uia(name,id) 两级定位）+ RepeatRunner + REST/SSE + 前端最简面板（开始/结束/列表/运行） | 验收 A1/A6/A8 初版：录一段记事本输入，自动出 workflow 并可回放成功 | M |
| **M2 定位链与干跑（1~2 周）** | locators 注册表全 6 类 + 图像锚点（解释器自动裁剪锚点图）+ 干跑/定位体检 + 失败阶梯 + 截图留证 | A2/A6 达标：记事本+资源管理器+一个 Chrome 页面场景回放成功率 ≥90%（≥20 次试验） | M |
| **M3 编辑·参数·调度（1~2 周）** | 画布 gui 节点属性面板、链序调整、参数化（start 节点注入）、循环建议→condition 展开、scheduler `repeat` kind、断点续跑 | A3/A4/A5/A7：定时无人值守跑通"每日导出→改名→归档"实例 | M |
| **M4 技能与语义增强（1 周）** | SKILL.md 生成 + `repeat_run` 工具接入对话 + LLM 语义命名 + 风险标注完善 + 隐私屏蔽全量 | 对话里说"跑一下我录的报表流程"即用技能回放；密码框全程无明文入库 | S~M |
| **M5 分发与自愈（1~2 周）** | 导入安全扫描、引导式修复界面、AI 修复建议 diff、insights 脆弱性看板、环境差异检查（跨机迁移告警） | A9/A10 | M |
| **v0.1 开源切分** | 见 §14 | 独立包 CLI 四命令可用 + README + 演示 GIF | S |

> 总量对齐原方案 M1–M5，但把"跨平台、口述转写、视频留证、并发队列"全部剔出承诺范围。

## 十三、风险清单与 Windows 特有坑位

| # | 坑 | 事实 | 方案 |
|---|---|---|---|
| 1 | **UIPI 完整性级别** | 普通权限进程读不到/点不动管理员权限窗口（钩子与 UIA 均受限）——是系统硬限制 | recorder/executor 与目标应用同级运行；检测到目标为提升窗口时明示"需以管理员运行 harness"或该步转人工节点；产品文案不承诺覆盖 |
| 2 | **DPI 缩放** | 125%/150% 缩放下，非 DPI-aware 进程取到的是虚拟化的假坐标 | recorder 与 executor 进程启动即声明 PerMonitorV2；bundle 记录 `scale`；coord 策略换算逻辑坐标 |
| 3 | **Chromium/Electron UIA** | 首棵树可达秒级；未开启 `--force-renderer-accessibility` 时属性可能稀疏 | 惰性单点查询 + 300ms 超时降级 + 预热；命中率统计驱动引导用户补图像锚点 |
| 4 | **输入法** | 逐字符键入会被中文 IME 劫持成拼音 | type 默认 clipboard_paste；录制期同样以控件 value 变化为准归并文本 |
| 5 | **前台锁定** | Windows 限制 `SetForegroundWindow`（非前台进程抢焦点可能失败） | 优先 pattern 直接 invoke（不需要前台）；确需前台时用 Alt 键技巧 + 激活校验重试 |
| 6 | **反自动化应用** | 部分游戏/网银禁止钩子与注入 | 明示边界（继承原方案 §7.6"不做对抗"），检测到即停止并提示 |
| 7 | **远程桌面/锁屏** | 会话断开后无图形界面 | 执行前环境校验拦截，明确报错 |
| 8 | **安全桌面(UAC 弹窗)** | WinLogon 桌面，钩子不可达 | 该时刻标 marker，运行到相应步骤设计为人工确认节点 |
| 9 | **本项目智能体沙箱** | 沙箱曾拦截特权原生操作（Frida 注入、句柄删除）；UIA/钩子属用户态、预期可过，但**未实测** | M0 Spike 第一件事就是实测；若受限，e2e 验证改在真实后端（start-all.bat / Tauri 壳）执行——REA 已有同款先例与流程 |
| 10 | **长流程资源** | 数百步长流程 + 截图累积 | 三档留证 + artifact 滚动清理（原方案 §6.9 策略照搬）|

## 十四、开源化与仓库规划

**孵化期**（现在）：代码住在本仓库，目录即 §3.2。约束：`modules/desktop_repeat/` 内**禁止 import fastapi / harness.api**，只准依赖 `harness.engine/infra` 的薄边界（DesktopController、artifact 路径注入、model client 注入），靠依赖注入缝保持可移植。

**开源期**（M1 末即切分，勿等成熟）：

```
andy-repeat/                     # 独立 GitHub 仓库
├── andy_repeat/                 # ← desktop_repeat 平移 + 自带 controller 实现(从 computer_use 剥离等价物)
│   ├── recorder/ interpreter/ locators/ executor/ skill/
│   └── cli.py                   # repeat record|interpret|replay|dry-run|skill
├── integrations/harness/        # andy-harness 插件适配层(小目录，指回主仓库)
├── samples/                     # 示例流程(记事本/资源管理器)
├── tests/                       # FakeDesktopController + tkinter 样例应用 e2e
└── DISCLAIMER.md                # 沿用主仓库免责声明风格(录屏隐私/误操作风险)
```

- 许可：MIT（与主仓库一致）；
- 依赖面控制：`pynput`(录制)、可选 extras `[uia]=uiautomation/comtypes`、`[image]=opencv-python-headless`、`[ocr]=winrt-windows-media-ocr`；回放对 `pyautogui/mss` 复用主 harness 已有 `desktop` extra 同名选择；
- README 主打演示 GIF（录一段→画布→自动回放），差异化一句话："**确定性回放、离线可用、AI 只在失效时出场**"。

## 十五、测试策略

| 层 | 手段 |
|---|---|
| 单测 | 解释器管线：手工构造 events.jsonl 金样例（含 IME 干扰/抖动/密码框/Chromium uia=null 等用例）→ 断言步骤序列与链装配；各 locator 的纯逻辑（打分、structure_path 容差匹配） |
| 组件测 | `FakeDesktopController` 同款 FakeUiBridge：无显示器 CI 上跑通 executor 全路径 |
| e2e（仅 Windows runner） | 自带 tkinter 迷你样例应用（确定性控件树：固定 AutomationId 的输入框/按钮/列表），录制→解释→回放→断言值一致；这是**可进 CI 的闭环**，不依赖真实第三方软件 |
| 真机回归 | 记事本 / 资源管理器 / Chrome 静态页 三场景脚本化冒烟，统计定位命中率（B4/B5 校准数据来源） |
| 质量门槛 | 沿用 mypy strict + ruff；`desktop_repeat` 全部 dataclass 带类型；GUI 节点执行失败不得使 GraphEngine 泄漏句柄 |

## 十六、附录：技术选型总表

| 用途 | 选型 | 理由 / 备选 |
|---|---|---|
| 全局输入捕获 | **pynput** | 纯 Python、跨三平台（虽我们 Windows-first）、资源占用与调研对象实测同级（0–3%）；备选： ctypes 直挂 `SetWindowsHookEx`（更可控但重复造轮子，M5 后性能优化再考虑） |
| UIA 访问 | **uiautomation**（comtypes 之上薄封装） | 只取 `ElementFromPoint/TreeWalker/Pattern`，是"用系统 API 的胶水"，非二开其上层产品；备选：直用 comtypes（多写 300 行胶水，收益低） |
| 截图 | **mss** | 已在 `desktop` extra，快且无 PIL 强耦合 |
| 输入注入（回放兜底） | **pyautogui** | 现状：computer_use 已依赖；pattern 优先、坐标兜底 |
| 图像锚点 | **opencv template matching**（`opencv-python-headless`） | 多尺度匹配成熟稳定；项目已有 numpy，M2 引入，可选 extra |
| OCR（assert_text 兜底） | **Windows 内置 WinRT OCR**（`winrt-Windows.Media.Ocr`） | 系统自带引擎、离线、支持 zh-CN、无外部服务、包体 ~几百 KB；注意需确认语言包已装（`Get-WindowsCapability Language.OCR*`）；备选：本地 PaddleOCR（重，增强档）；**不用云 OCR**（数据不出本机原则） |
| 覆盖层 | **tkinter** | 零新依赖，倒计时/状态胶囊够用 |
| LLM | harness `model_manager` Provider | 仅 §6.2/§9.2；可选可降级 |
| 进度流 | `progress.py` 范式 + SSE | 项目已验证的流式形态（含踩坑修复） |

---

## 结语

对参考文档的取舍一句话：**产品层结论全盘继承，工程层重写地基**——把"技术中立"落到 harness 的具体文件与既有模块上，把"跨平台/全自动语义/视频留证"三处过度承诺收缩为 Windows-first、规则优先+AI 增强、关键帧证据。

对开源调研的姿态一句话：**借思想，不借代码**——事件流取证、语义定位、技能化封装这三个被验证的方向全部自研实现；且和y-repeat 的差异化（确定性回放零 token、AI 仅出场于失效点、与 harness 的调度/审批/技能生态原生打通）在任何调研对象上都不存在现成等价物，自研路线成立。

下一步建议：先执行 **M0 Spike**（半天脚本 + 一天结论），用实测数据确认本项目沙箱与真实终端下 pynput/UIA/mss 的可用性边界，再启动 M1。
