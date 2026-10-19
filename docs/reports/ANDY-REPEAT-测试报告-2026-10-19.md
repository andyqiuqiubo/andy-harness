# andy-repeat 录屏与回放 · 三阶段测试报告

> **日期**：2026-10-19
> **范围**：`desktop_repeat` 模块 + `andy_repeat` 插件 + `/repeat` 工作台 + 工作流画布 `gui` 节点集成
> **方法**：真实桌面真机验证 + 确定性单测/集成测试 + 前端构建门禁
> **结论**：主流程与全部分支按计划执行完毕；发现并修复 **20 项缺陷**（其中 6 项为阻断级），全部通过回归。

---

## 一、测试环境

| 项 | 值 |
|---|---|
| OS | Windows 10（10.0.18363），桌面会话 |
| 显示 | 1920×1080，**125% DPI 缩放**（用于验证坐标/DPI 处理） |
| Python / Node | 3.12（backend venv）/ 22.22.2 |
| 依赖 | `pynput` / `uiautomation` / `mss` / `pyautogui` 均已安装 |
| 目标应用 | 自研确定性 Win32 窗口（标准 EDIT + BUTTON，具备 UIA 语义） |
| 浏览器 | 本机 Chromium（Playwright 驱动，真实前端 + 真实后端） |

> 目标窗口与注入均由测试脚本自动完成；所有截图来自真实运行，不是设计稿。

---

## 二、阶段一：核心主流程（正常路径）

### 2.1 测试矩阵与结果

| # | 步骤 | 验证点 | 结果 |
|---|---|---|---|
| 1 | 启动后端与前端 | `/api/health` 200、`/repeat` 可访问、插件 `available=true` | ✅ |
| 2 | 开始录制 | 录制器进入 `recording`，心跳正常 | ✅ |
| 3 | 真实操作 | 点击输入框 → 键入 `QA-123` → 点击 Submit（SendInput 真实注入） | ✅ |
| 4 | 停止录制 | 会话封版 `ready`，事件数 15，SHA256 清单通过 | ✅ |
| 5 | 解析 | 4 步（window_focus / click / type / click），定位链含 UIA 语义项 | ✅ |
| 6 | 发布为工作流 | 生成 `gui` 节点图并跳转画布 | ✅ |
| 7 | 干跑体检 | 4/4 命中，全部 `hit@0` | ✅ |
| 8 | 真实回放 | 状态 `SUCCEEDED`，每步 `#0 uia/focus` | ✅ |
| 9 | 结果断言 | 目标输入框值 = `QA-123`；按钮点击次数 1 → 2 | ✅ |
| 10 | 导出技能 | `SKILL.md` 生成，含 `graph_sha256` 与 `repeat_run` | ✅ |

### 2.2 控制类主流程（真机）

| # | 场景 | 验证点 | 结果 |
|---|---|---|---|
| 1 | 暂停 | 心跳 `paused`，暂停区间零事件 | ✅ |
| 2 | 恢复 | 心跳 `recording`，后续事件正常入库 | ✅ |
| 3 | 打点 | `marker` 事件含 label/note，解析为 marker 步骤 | ✅ |
| 4 | 停止 | 会话 `ready`，活跃态清空、租约释放 | ✅ |
| 5 | 丢弃 | 会话 `discarded`，不生成清单，活跃态清空，可立即重录 | ✅ |
| 6 | 热键结束 `Ctrl+Alt+R` | 会话 `ready`，服务侧自动收口（≤3s） | ✅ |
| 7 | 热键丢弃 `Ctrl+Alt+X`×2 | 会话 `discarded`，有 `discarded_at` 标记、无 `manifest.json` | ✅ |
| 8 | 无会话时暂停/打点 | 返回 `RP-5004`，不误改状态 | ✅ |
| 9 | 停止已封版会话 | 幂等返回 `ready`，不写控制文件（兼容 CLI 独立进程） | ✅ |
| 10 | 删除不存在会话 | 服务返回 `false`，REST 404 | ✅ |

> 真机脚本共 **29 项检查**（主流程 10 + 控制 10 + 热键 9），全部通过。

---

## 三、阶段二：全部分支（异常 / 边界 / 权限 / 并发）

### 3.1 请求校验与状态码

| 类别 | 用例 | 结果 |
|---|---|---|
| 分页边界 | `limit=0/201/abc`、`offset=-1` → 422 | ✅ |
| 类型错误 | `countdown_s="abc"` → 422 | ✅ |
| 资源不存在 | 未知会话 DELETE → 404；未知运行 GET → 404 | ✅ |
| SSE 未知运行 | 立即 404（旧实现永久挂起） | ✅ |
| SSE 已终结运行 | 总线为空时合成 `run_done` 并立即收束 | ✅ |
| 非法裁决 | `decision=bogus`（运行存在/不存在）→ 422 | ✅ |
| 未知运行裁决/中止 | → 422 | ✅ |
| 未知工作流干跑/回放 | → 422 | ✅ |
| 缺参启动 | 返回 `rejected` + 缺参清单（200，零副作用） | ✅ |
| 版本哈希不一致 | → 422 `RP-6004` | ✅ |
| 干跑静态校验 | 非法动作在干跑阶段返回 `ok=false` + `RP-6004` | ✅ |

### 3.2 权限

| 用例 | 结果 |
|---|---|
| 认证启用、无 token 访问 `/status` | 401/403 | ✅ |
| 非管理员访问 REST | 403 | ✅ |
| 非管理员访问 SSE `?token=` | 403（旧实现被中间件 401 拦截） | ✅ |
| 管理员访问 | 放行 | ✅ |

### 3.3 并发

| 用例 | 期望 | 结果 |
|---|---|---|
| 同时开始两次录制 | 仅一个获得租约，另一个 `RP-5004` | ✅ |
| 同时启动两次回放 | 仅一个获得租约，另一个 `RP-5004` | ✅ |
| 录制中启动回放 | `RP-5004` | ✅（既有用例） |

### 3.4 执行器异常与竞态

| 用例 | 期望 | 结果 |
|---|---|---|
| 无人值守遇人工节点 | 立即中止 `RP-4004`，不挂起等待 | ✅ |
| 裁决早于挂起（点"跳过"过快） | 裁决不丢失，继续执行 | ✅ |
| 断言失败 | 挂起 `RP-3002`，可中止 | ✅ |
| 目标应用未运行（交互） | 挂起 `RP-1002` | ✅ |
| 目标应用未运行（无人值守） | 失败 `RP-1002` | ✅ |
| marker 步骤 | 直接成功，不注入 | ✅ |
| 审批提供方返回 pending | token 可授权，重试可继续 | ✅ |
| 重试同一步骤 | 时间线不重复、统计不虚高 | ✅ |
| 启动/裁决后的接管基线 | 不误判为人工介入 | ✅ |

### 3.5 存储与边界（既有用例，回归通过）

事件序号单调/并发写、末行截断丢弃、schema 过新拒绝、清单篡改拒绝、UIA value 不落盘、帧 GC、密码段转人工、键段/滚轮/拖拽归并、定位链评分、参数校验与脱敏等 **86 项**全部通过。

### 3.6 前端

| 项 | 结果 |
|---|---|
| `pnpm build`（vue-tsc + vite） | ✅ 通过 |
| `pnpm test`（vitest） | ✅ 108 项通过 |
| 真实页面交互（录制/解析/干跑/回放/画布） | ✅ 截图验证 |

---

## 四、缺陷清单与修复

> 严重度：**P0 阻断**（核心能力不可用/数据语义错误）、**P1 高**（主路径明显受损）、**P2 中**、**P3 低**。

| ID | 严重度 | 现象 | 根因 | 修复 | 回归 |
|---|---|---|---|---|---|
| D1 | P0 | 所有 UIA 语义定位静默失败，全部降级到图像/坐标 | `Control.Exists()` 传了不存在的关键字 `sleepIntervalSeconds`，异常被吞 | 改为位置参数（跨 uiautomation 版本稳定） | `test_find_class_index_uses_positional_exists_args` |
| D2 | P0 | `name_type` / `structure_path` 永不命中 | `Compare` 回调被 `(control, depth)` 两参数调用，签名不兼容 | 回调接受 `depth`；闭包变量改 keyword-only | `test_find_name_type_compare_accepts_depth_argument`、`test_match_path_compare_accepts_depth_argument` |
| D3 | P1 | 首个 UIA 定位调用直接失败 | worker 冷启动就绪竞态，`_submit` 立即返回 not ready | 等待就绪事件（按请求预算封顶 5s） | `test_submit_waits_for_worker_readiness` 等 3 项 |
| D4 | P1 | 键入步骤永远没有控件锚点，只能盲打 | `_key_flush` 在返回事件前重置焦点快照 | 快照附着到事件（`Stub.uia`） | `test_key_segment_anchors_focus_control_by_handle` |
| D5 | P1 | 键事件锚到下一个被点击的控件（如按钮） | 焦点在 worker 线程延迟查询，被 400ms 后帧阻塞拖后 | 钩子回调记录焦点句柄，按句柄取快照 | 同上 + `test_key_segment_without_handle_falls_back_to_focused` |
| D6 | P1 | `Ctrl+Alt+字母` 全局热键全部失效 | AltGr 组合下 pynput `char=None`，键名变成 `<82>` | VK→键名回退映射 | `test_key_event_uses_vk_when_char_missing` |
| D7 | P0 | 点"丢弃本次"后录制反而被保存为 `ready` | 丢弃后仍走 `_shutdown_final → finalize`，覆写终态并生成清单 | 丢弃为终态，跳过 finalize | 真机热键丢弃用例（9/9） |
| D8 | P0 | 丢弃/热键结束后会话锁死，无法再录/回放 | 服务未清 `active_session`、未释放租约 | 心跳监视直接观察终态并收口；discard 与 stop 共用收尾路径 | 真机控制流 + 热键用例 |
| D9 | P0 | 交互模式点"重试"无限循环挂起 | 审批提供方返回 pending 但 token 为空，`grant('')` 永远失败 | 安全门为任何 pending 决策补发 token | `test_provider_pending_decision_is_grantable` |
| D10 | P2 | 重试后时间线出现重复步骤行、统计虚高 | 每次重试都 `append` 新 StepRun | 复用同 sid 的 StepRun；前端 `step_started` 原地重置 | `test_retry_reuses_step_row` |
| D11 | P1 | 点击"回放"后第一步就报"检测到人工介入" | 启动点击/重试点击被当成回放期间的干扰 | 执行起点与人工裁决后重置接管基线 | `test_takeover_baseline_reset_on_start_and_resume` |
| D12 | P3 | 会话列表事件数恒为 0 | 列表接口只返回嵌套 `session`，前端读 `counters` | 后端列表项补 `counters` | 截图验证 |
| D13 | P2 | 流程行"回放"按钮白底白字不可见 | `.rp-flow-ops button` 覆盖了 `btn-primary` 背景 | 增加 `.rp-flow-ops .btn-primary` 配色 | 截图验证 |
| D14 | P0 | 录制流程在工作流画布无法渲染/编辑 | 前端缺少 `gui` 节点定义 | 增加节点定义、面板分组、属性面板 | 截图 + 构建 |
| D15 | P2 | 画布中 start 与首个步骤重叠、节点超出视口 | 编译布局首格复用 (60,120)；无自适应 | 布局改为 start 独占首格 + 3 列换行；载入后自动适配 | 截图验证 |
| D16 | P2 | 未知运行返回 200；SSE 未知运行永久挂起；非法裁决被接受；abort 未知运行 500 | REST/服务缺少存在性校验 | GET/SSE/abort 404；裁决先校验；服务抛 `RP-6004` | 6 项 API 用例 |
| D17 | P3 | 删除不存在的会话返回成功 | `sdir.exists() is False` 被当成成功 | 删除前记录存在性；REST 404 | `test_delete_missing_session_404` |
| D18 | P2 | 体检全绿，回放才报"步骤非法" | 干跑未做静态校验 | 干跑前置 `steps_from_graph + validate` | `test_dry_run_reports_invalid_step` |
| D19 | P2 | 缺参时按提示"只填值"会被当成空参数，二次 rejected | 前端只解析 `name=值` | 支持键值对与按顺序填值，提示文案写明格式 | 构建 + 代码审查 |
| D20 | P3 | 回放成功后仍显示"等待人工"旧提示 | `run_done` 未清理 notice | 成功时覆盖为"回放完成" | 截图验证 |

> 另：D8 的严格校验一度导致 CLI `record --stop`（独立进程）无法结束录制，已改为**幂等语义**并纳入真机控制流用例（`C_stop_finished_idempotent`）。

---

## 五、回归验证

| 门禁 | 结果 |
|---|---|
| 后端 pytest（core/executor/persistence/recorder/service/win32/api） | **126 passed** |
| 真机主流程脚本 | **通过**（目标状态断言一致） |
| 真机控制流脚本 | **20/20 检查通过** |
| 真机热键脚本 | **9/9 检查通过** |
| 前端 `pnpm build` | **通过**（vue-tsc + vite） |
| 前端 `pnpm test` | **108 passed** |

---

## 六、未覆盖与残余风险

| 项 | 说明 | 建议 |
|---|---|---|
| 管理员权限窗口 | UIPI 硬限制，测试环境未以管理员运行目标应用 | 在目标环境用同级权限实测；文档已标注 |
| UAC 安全桌面 | 无法自动注入 | 设计为人工节点；需人工验证 |
| 锁屏/远程断开 | 未在断开会话下跑 | 已有 `RP-1001` 预检；建议补充环境测试 |
| 第三方应用（ERP/Office/Chrome） | 仅用自研 Win32 目标验证 | 建议按实际业务应用做 3~5 条真机回归 |
| 多显示器/不同缩放组合 | 仅验证 125% 单屏 | 建议覆盖 100%/150%、双屏 |
| Chromium 系 UIA 稀疏 | 未在真实 Chrome 页面验证 | 建议用实际 Web 系统验证图像锚点与降级 |
| 长流程（数百步） | 未做压力测试 | 建议验证帧容量、运行时长与内存 |
| 反自动化应用 | 不做对抗 | 明确不支持 |

---

## 七、证据与产物

| 内容 | 路径 |
|---|---|
| 用户操作手册 | `docs/manual/andy-repeat-录屏回放用户操作手册.md` |
| 手册截图 | `docs/manual/screenshots/repeat-0*.png` |
| 真机主流程脚本 | `logs/repeat-qa/e2e_phase1.py` |
| 真机控制流脚本 | `logs/repeat-qa/e2e_phase1_controls.py` |
| 真机热键脚本 | `logs/repeat-qa/e2e_hotkey.py` |
| 截图采集脚本 | `logs/repeat-qa/demo_ui.py` + `logs/repeat-qa/shots.mjs` |
| 运行日志 | `logs/repeat-qa/*.log` |
| 新增回归测试 | `backend/tests/test_desktop_repeat_win32_locators.py`、`test_desktop_repeat_api.py`，以及 recorder/executor 内的新增用例 |

---

## 八、结论

1. **主流程可用**：真实桌面环境下，"录制 → 解析 → 发布 → 干跑 → 回放 → 技能导出"闭环通过，目标应用状态与预期一致。
2. **核心定位能力已修复到设计预期**：修复后所有步骤命中 UIA 链首（`hit@0`），不再无条件降级到图像/坐标。
3. **安全与终态语义已收紧**：丢弃不可被覆写为已保存；热键结束会立即收口；审批重试可真正放行；重试不再产生重复步骤。
4. **集成缺口已补齐**：录制产物可在工作流画布正常渲染、查看与编辑。
5. 仍建议在真实业务应用与多显示环境下补做一轮真机回归（见 §6），再对外承诺"生产可用"。
