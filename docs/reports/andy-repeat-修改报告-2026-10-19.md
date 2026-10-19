# andy-repeat 修改报告（基于体检报告逐项修复）

- 日期：2026-10-19
- 对应体检报告：`docs/reports/andy-repeat-体检报告-2026-10-19.md`
- 回归门禁：后端 `pytest tests/test_desktop_repeat_{core,executor,persistence,recorder,service,win32_locators,api,audit}.py` = **169 通过 / 0 失败**（既有 154 + 审计新增 15，含追加修复新增 3 项）；后端**全量** `pytest tests -p no:randomly` = **1350 通过 / 5 skipped / 0 失败**（9m17s，本轮顺带修掉 P2-11 的跨文件污染后才达成全绿）；前端 `pnpm build` = **exit 0**（vue-tsc -b + vite build）。
- 改动范围：全部经专项复现用例（`backend/tests/test_desktop_repeat_audit.py`）坐实——修复前失败、修复后转绿。

---

## 修复总览（按严重度）

| 编号 | 级别 | 问题一句话 | 状态 | 主要文件 |
|------|------|-----------|------|----------|
| P0-1 | 阻断 | 录制时钟域混用致事件 t 恒 0、ts 全错 | ✅ 已修 | recorder/runner.py |
| P0-2 | 阻断 | RunRecord.to_dict 丢字段致落库覆盖默认值 | ✅ 已修 | models.py |
| P0-3 | 阻断 | 双击去重依赖 seq==eid 巧合 | ✅ 已修 | recorder/runner.py, interpreter/merge.py |
| P0-4 | 阻断 | 作用域标题假确认放行错误窗口写操作 | ✅ 已修 | executor/runner.py |
| P0-5 | 阻断 | _relaxed 放宽标记跨步骤泄漏 | ✅ 已修 | executor/runner.py |
| P1-1 | 功能 | en.ts 缺 5 个 repeat.* 键 | ✅ 已修 | i18n/en.ts |
| P1-2 | 功能 | 覆盖层倒计时实为 0.45 秒 | ✅ 已修 | recorder/overlay.py |
| P1-3 | 功能 | CLI 跨进程无法结束/控制录制 | ✅ 已修 | service.py, config.py |
| P1-4 | 功能 | 点击引发的窗口切换录不到 window_change | ✅ 已修 | recorder/runner.py |
| P1-5 | 功能 | 帧 GC 从未接线，bundle 只增不减 | ✅ 已修 | service.py, plugins/andy_repeat/main.py |
| P1-6 | 功能 | RP-4005 未注册 | ✅ 已修 | errors.py |
| P2-2 | 健壮 | 单步超时无硬检查 | ✅ 部分修（见备注）| executor/runner.py |
| P2-3 | 健壮 | act_hotkey 未 mark_injected | ✅ 已修 | executor/actions.py |
| P2-4 | 健壮 | 粘贴后立即恢复剪贴板竞态 | ✅ 已修 | executor/actions.py |
| P2-5 | 健壮 | 键段静默边界轴混用 | ⛔ 复核撤回 | interpreter/merge.py（未改）|
| P2-6 | 体验 | 进度条漏计 degraded/skipped | ✅ 已修 | RepeatStudioView.vue |
| P2-7 | 一致性 | 审批策略注释与默认漂移 | ✅ 已修 | config.py, safety_gate.py |
| P2-10 | 健壮 | 停止等待轮数硬编码 | ✅ 已修（随 P1-3）| config.py, service.py |

---

## 逐项说明

### P0-1 录制时钟域混用
- 根因：`recorder/runner.py` 的 `_mono0` 被赋成 `_epoch0`（time_ns 域），而 `_t_of/_ts_of` 的输入全来自 `perf_counter_ns()` 域，量级差 20 个数量级 → `t_ns - _mono0` 恒为巨大负数 → `max(0,...)` 钳成 0，每条事件 t=0、ts≈开机秒。连锁使 S5 等待推断、暂停扣除、时长统计全部失效。
- 修复：`self._mono0 = time.perf_counter_ns()`（与打点同域），`_ts_of` 仍用 `_epoch0/1e6 + (t_ns-_mono0)/1e6`。
- 验证：端到端探针 `t_of(5s)=5000ms`、`ts` 与真实 epoch 偏差 0ms；recorder 12 项（含新回归用例）全绿。

### P0-2 RunRecord.to_dict 丢字段
- 根因：`to_dict()` 未输出 `vars/checkpoint/attempts_total`，而 `repository.finish_run` 从该字典取这三个键 → 正常终态收口被写回默认值（重试数清零、断点清空、参数覆盖为空）。
- 修复：`to_dict()` 补齐三字段（vars 已在构造时经 redact_vars 脱敏，可安全落盘）。
- 验证：`TestRunRecordPersistenceFidelity` 两用例（含真库 insert→finish→get 往返）通过。

### P0-3 双击去重依赖 seq==eid 巧合
- 根因：`double_click.supersedes_eid` 是归一器私有 eid 空间，而 merge 用 `ev.seq`（含 session_start/marker 等系统事件的写盘空间）比对；真实流中前置系统事件使 seq>eid，去重永不命中 → 多出一次孤立 click 步骤，回放"先单击再双击"。
- 修复：`_write_stub` 写盘时统一附着 `data["eid"]=stub.eid`；`merge.compile_semantics` 优先按 `data.eid` 判取代，无 eid 的历史 bundle 回退 seq（向后兼容）。
- 验证：`test_double_click_dedup_with_offset_events`（session_start+marker 前置错位场景）产出 `['double_click']`；既有 `test_double_click_drops_superseded_click`（seq==eid）仍绿。

### P0-4 作用域标题假确认
- 根因：`find_window` 四级放宽 miss 后按 exe 兜底返回任一窗口，`_ensure_scope` 只要 found 非空即 `_scope_title_ok=True`，不校验标题是否匹配 `window_title_regex` → 错误窗口里的写操作被 `_scope_ok` 放行（与"18/18 succeeded 但应用无反应"同链路）。
- 修复：found 命中后校验标题正则；不匹配且非"同窗口页面跳转（前台就是作用域窗口）"时置 `_scope_title_ok=False`，写操作按既有契约拒绝 RP-4002。
- 验证：新增 `test_exe_fallback_wrong_window_blocks_write`（exe 兜底命中错误窗口 → 断言 title_ok=False 且步骤 code=RP-4002）；`test_scope_switches_between_same_exe_windows`（真实按标题切换）仍绿。service happy-path 假件随之校正为忠实窗口标题。

### P0-5 _relaxed 跨步骤泄漏
- 根因：某步定位耗尽触发"放宽重试"后 `self._relaxed=True` 常驻，后续所有步骤的 UIA name_type 匹配都按编辑距离≤2 放宽 → "保存/保存副本"类近名控件极易被抢先命中且记录显示 uia@0。
- 修复：`_run_step` 入口复位 `self._relaxed=False`（放宽仅对触发它那一步的重试周期有效）。
- 验证：`test_relaxed_flag_does_not_leac_to_next_step`（s01 触发放宽后 s02 spec.relaxed 回到 False）通过。

### P1-1 en.ts 缺键
- 修复：补 `repeat.discarded / recEnded / noActive / paramHint / runDone` 五条英文文案（与 zh.ts 对齐）。
- 验证：`test_repeat_i18n_keys_present_in_both_locales`（脚本扫描 RepeatStudioView 用到的全部 repeat.* 键，逐词典比对）通过；前端 build 通过。

### P1-2 覆盖层倒计时
- 根因：`_loop` 每 150ms tick 把 `cd` 减 1，"3-2-1"实际仅 ~0.45 秒；`cd_done` 分支从未被投递（死代码）。
- 修复：改为挂钟计算——`cd_end = monotonic()+cd_total`，每 tick 显示 `ceil(剩余)`、到点即收束胶囊；顺带补上 `_apply` 缺失的 `"pill"` 分支（旧版状态胶囊恒显初始"● 准备中"，看不到已录事件数）。
- 验证：语法/ruff 通过、recorder 套件全绿（overlay 无头不直接测渲染，逻辑单列）。

### P1-3 + P2-10 CLI 跨进程接管
- 根因：CLI `record --stop`/pause 是独立进程，新建 service 的内存 `_active_session` 恒为 None，`stop_recording`/`_control` 直接拒绝"当前无活跃录制会话"，与帮助文案承诺相反。
- 修复：新增 `_discover_live_session()`——按"心跳新鲜（<6s）+ 状态 draft + state 非终态"扫描 sessions_root 认领活动会话；`stop_recording`/`_control` 的 sid 解析回退到它。控制/心跳本就走 bundle 文件（ADR-02），天然跨进程。停止等待轮数提为 `cfg.stop_wait_rounds`（默认 140≈21s，测试可调小）。
- 验证：`test_discover_live_session_finds_fresh_draft`（新鲜 draft 被认领、陈旧心跳/已封版不认领、全终态返 None）通过。

### P1-4 点击引发的 window_change
- 根因：`_win_context` 写事件前抢先更新 `_last_fg`，随后轮询 `_detect_window_change` 比较时 prev==curr → 点击切窗口永不发 window_change（只有 Alt+Tab/点别的窗口才由轮询捕获）。
- 修复：抽公共 `_emit_window_change(prev,curr)`；`_win_context` 改"先比较后更新"，变化即补发（时间线上恰在当前事件之前）；轮询路径保留作其他来源兜底，两条路径共用同一发射逻辑。
- 验证：新增 `test_click_triggered_window_switch_emits_window_change`（两段不同标题窗口，点击切换 → 断言 window_change 事件、prev/curr 正确、seq 连续）。

### P1-5 帧 GC 接线
- 根因：`gc()`/`gc_session_frames` 实现完整但全仓无调用点，standard/forensic 档帧的 30/7 天保留形同虚设。
- 修复：service 增加 `_ensure_gc_task/_gc_loop`（启动静默 5 分钟后每 6h 一轮，异常吞掉不阻塞），`shutdown` 取消任务；插件 activate 调 `ensure_maintenance()`；`gc()` 改走线程池并跳过当前活跃会话。
- 验证：service 套件全绿；GC 为旁路低频任务，不影响主链路。

### P1-6 RP-4005 未注册
- 修复：`errors.py` 补注册 `RP-4005`（人工裁决等待超时，recoverable=True、action=abort），使 `_await_resume` 超时中止的 REST 语义与错误字典一致。

### P2-2 单步超时（部分修，含一处自我纠错）
- 修复：`_run_step` 在 `_act` 前补 `budget.check("执行前")`——触发点在注入之前，重试不会重复动作。
- 备注（自我纠错）：最初还在 assert 前加了 check，随即撤除——act 之后动作已执行，此时判超时触发重试会造成非幂等步骤（提交/支付类）二次点击。最终只在 act 前保护。
- 验证：executor/audit/service 三套件 63 项通过（含改动后的超时点）。

### P2-3 act_hotkey 未 mark_injected
- 修复：热键注入成功后 `ctx.mark_injected(None)`，使启用 human_takeover_detect 时"自己的注入"不被误判成人工介入（同 window_focus 的 ALT 技巧教训）。

### P2-4 粘贴恢复剪贴板竞态
- 修复：`_paste` 发完 Ctrl+V 后 `time.sleep(0.2)` 再 restore，避免目标应用异步读取与恢复写入竞态（本函数经 asyncio.to_thread 跑线程池，sleep 不阻塞事件循环）；补 `import time`。

### P2-5 键段静默边界轴混用（复核后撤回）
- 原判断：`ev.t - prev_eff` 混用原始轴与有效轴会高估跨暂停间隔。
- 复核结论：录制层 `enter_pause` 已在暂停边界主动结算键段，跨暂停的键段现实中不存在；对历史/手改 bundle，膨胀判分段是"宁分勿并"的保守正确行为（防止"暂停前+恢复后"被并成一个输入步骤）。改有效轴反而丢分段语义 → **不改代码**，报告与本次修复均记为撤回。

### P2-6 进度条漏计
- 修复：`RepeatStudioView.vue` 的 `runBar.done` 统计从 `'done'` 扩为 `['done','degraded','skipped']`，与后端 `stats()`（done+degraded）口径一致，避免降级场景进度虚低。

### P2-7 审批策略注释漂移
- 修复：`config.py`/`safety_gate.py` 中把"degraded（默认）"的过时注释更正为"never（默认）"，并补注默认取 never 的决议依据（回放默认不做人工审批，靠紧急停止热键 + 页面中止兜底）。

---

## 新增/调整的测试

- **新增** `backend/tests/test_desktop_repeat_audit.py`（9 项）：A1 落库保真（2）、A2 relaxed 泄漏、A3 exe 兜底拒写、A4 热键降级（真跑 + 干跑，2 项）、B1 双击错位去重、B1b 跨进程接管、B2 i18n 完整性。均为"修复前失败、修复后通过"的复现型断言。
- **新增** recorder 回归用例 `test_click_triggered_window_switch_emits_window_change`（P1-4）。
- **调整** 共享假件 `repeat_fakes.py`：`FakeWindows.find_window` 回退为宽容版（作用域标题隔离由专用假件覆盖，避免各套件互相牵制）。
- **调整** service 测试假件：`FakeWindows()` → 传入与录制一致标题的窗口（P0-4 修复使忠实校验生效，宽容假件才能测 happy path）。

## 追加修复：真机 s09 热键死锁（P0-6 ~ P0-9）

> 触发：实机复测"浏览器输入 URL 后未按 Enter"场景，回放停在 s09 无限等待人工。
> 详见体检报告 `andy-repeat-体检报告-2026-10-19.md` 第七章的证据链。

### P0-6 键事件时间戳用"结算写盘时刻"而非"按下时刻"
- 根因：`_write_stub` 一律取 `time.perf_counter_ns()` 作 `t_ns`；但键段要等 1200ms 静默 / 150ms IME 防抖才 flush，normalizer 已写入的按下时刻 `data["_t_ms"]` 反而在写盘时被 `pop` 丢弃 → 回车落到导航之后。
- 修复：`_write_stub` 开头若 `stub.data["_t_ms"]` 为有效正数，则 `t_ns = int(_t_ms * 1e6)`（与 `_mono0` 同属 perf_counter 域，不混入 epoch）。
- 附带：`_key_flush` 合成的文本/键 stub 继承段内最后一个真实按下时刻 `seg_t`，使整段共享按下时刻而非 flush 时刻。

### P0-7 键事件的窗口上下文取自"写盘时的前景"（已被导航污染）
- 修复：键/文本事件改用按下时刻即可确定的 `self._last_fg`（若非空），**不再**重新调 `_win_context()`；其余事件维持原查询。

### P0-8 worker 循环顺序致 window_change 抢在按键之前
- 修复：`_worker_loop_inner` 有批次分支改为"先 `normalizer.tick()` 结算键段 → 再 `_detect_window_change()`"，保证按键引发的页面切换排在按键之后（seq 不再倒挂）。

### P0-9 执行层 hotkey 定位耗尽即死锁（因果倒置）
- 根因：`act_hotkey` 完全不使用 `hit` 元素（`ctx.injector.hotkey(keys)` 直接向当前焦点注入），定位链只是一个"会失败的门闩"。锚点被绑到"按下后才出现的页面"时，就必须先定位到页面才允许按键——而页面要等按键才存在；再叠加 `on_fail=suspend_manual`，表现为**无限挂起**而不是失败。
- 修复：`_run_step` 捕获 RP-2001/RP-2002 且 `step.action == "hotkey"` 时降级为 `_focus_hit(step)`（作用域窗口仍由 `_ensure_scope` 保证，语义不变）；`_dry_one` 同步判 `focus`，避免 Studio 预览把该步报成"不可用"。
- 兼容性：该兜底覆盖**存量已编译工作流**（旧 bundle 无 `_t_ms`、锚点仍绑错页面），无需重录即可通关。

### 追加测试（均为"屏蔽修复即失败"的复现型断言）

| 用例 | 锁定项 | 未修复时表现 |
|------|--------|--------------|
| `test_desktop_repeat_recorder.py::test_key_events_keep_press_time_and_press_window` | P0-6 / P0-7 / P0-8 | enter 的 `t` 变成 flush 时刻、`win.title` 变成导航后标题、`window_change` 的 seq 早于 enter |
| `test_desktop_repeat_audit.py::test_hotkey_locator_exhausted_falls_back_to_focus_injection` | P0-9 | `inj.calls` 为空、步骤 suspended（真机为无限挂起）→ 断言报"热键未注入：定位链耗尽即死锁" |
| `test_desktop_repeat_audit.py::test_dry_run_hotkey_reports_focus_not_miss` | P0-9 干跑口径 | `hit_kind` 为空、`message = RP-2001 目标未找到（定位链耗尽）` |

- 挂死路径已加 `asyncio.wait_for(..., timeout=10)` 栅栏：未修复时是**明确的失败**而非把测试套件拖挂（复跑验证：屏蔽修复后两条执行层用例 15s 内失败退出）。
- ruff 复核：改动文件干净（残留告警均为改动前既有的 `UP041`/`E741`）。

### P2-11 测试隔离：全量跑才失败的 SSE 401（顺带发现，非本轮改动引入）
- 现象：`pytest tests`（全量）时 `test_desktop_repeat_api.py::test_sse_unknown_run_404_not_hang` / `test_sse_terminal_run_closes_immediately` 报 `assert 401 == 404/200`；但单跑该文件、或只跑 repeat 八套件时全绿。
- 根因：`harness.main._services` 是**模块级单例**。`test_auth` / `test_admin_gate` 在 `monkeypatch.setenv("HARNESS_AUTH","1")` **之后**才 `from harness.main import app`，导入触发 setup 把 `enabled=True` 的 AuthService 注册进该全局；monkeypatch 只回滚 env、不回滚注册表，污染残留到后续所有测试。本文件的裸 app 经 `_require_admin_sse` 查到"认证已启用" → 无 token 即 401。
- 修复：`api` 夹具内把 `harness.main._services` 换成空 `ServiceRegistry()`（仅在本夹具作用域内，monkeypatch 自动还原），让该文件的语义回到"未启用认证 → 放行"。
- 验证：`test_admin_gate + test_desktop_repeat_api` = 28 passed；`test_auth + test_desktop_repeat_api` = 47 passed（修复前均为 SSE 用例失败）。
- 备注：这是**测试基建**缺陷而非产品缺陷（生产单进程内 `_services` 始终是真实注册表）；但它是"只在全量跑暴露"的典型，故一并修掉，避免后续再次误判成产品 bug。

## 追加修复 2：模态对话框锁死回放（真机 run-f9376f87dd，EditPlus「另存为」）

> 现象：点击 EditPlus 保存按钮后弹出「另存为」对话框，回放没有点对话框里的保存按钮（录制时点了），停在 s11 报 RP-1007「检测到弹窗/模态框遮挡」。

### 根因（三层叠加）
1. **`_modal_blocked` 一刀切**：只看"作用域窗口被禁用"就判遮挡。模态框弹出 → 主窗口被系统 Disable → 每一步都在主窗口作用域下被 RP-1007 拒绝——包括"切到对话框继续操作"这一步本身（s11 window_focus），形成**想脱离遮挡的动作被遮挡判定拦死**的死锁。
2. **`find_window` exe 兜底**：对话框从点击到可枚举有约 800ms 延迟，紧随其后的步骤立即枚举扑空时，兜底返回**旧主窗口**而非 None，扑空被伪装成"命中了别的窗口"，作用域永不更新。
3. **对话框标题漂移**：EditPlus 对话框标题在「另存为」↔「EditPlus」间跳变（cls 同为 #32770）。s13 的 hint（cls=#32770 + title=.*EditPlus.*）在标题为「另存为」时被 P0-4 的严格标题校验判为"错误窗口"→ 写操作 RP-4002（即使修了 1、2 也会卡在这）。

### 修复（executor/runner.py + config.py）
- `_modal_blocked(step)` 语义重建：作用域被禁用只说明"存在模态面"——window_focus/wait_window 等**切作用域动作必须放行**（且先经 `_adopt_modal_window` 把作用域挪到模态面，否则聚焦的仍是被禁用的旧窗口）；其它动作尝试 `_adopt_modal_window()`（前台窗口与旧作用域同 pid/exe → 接管为新作用域，前台即用户可见操作面）；都不行才是真遮挡。
- `_ensure_scope` 补"目标窗口出现"有界等待（`cfg.window_appear_wait_ms`，默认 2.5s、200ms 一探）：find_window 返回 None **或命中窗口不满足 hint**（含 exe 兜底伪装命中）时触发；仅当 hint 有明确目标（标题正则/类名）才等待，不拖慢"窗口当真不存在"。
- cls 精确重找 + cls 强证据放行：find_window 把 exe+title 排在 exe+class 之前，主窗口标题常恰好命中对话框 hint（"EditPlus - [...]" 匹配 .*EditPlus.*）→ cls 不符时按 (exe, cls) 重找；`_title_ok` 在标题不符时接受 **cls 精确命中**为强证据（cls 编译期取自真实目标窗口，标题漂移≠找错窗口）。P0-4 的 exe 兜底严格校验**不变**（无 cls 证据时仍严格）。

### 二次修复（用户重启后仍报 RP-1007：接管条件过严 → 语义收窄）

第一次修复只解决了"FOCUS 动作被拦"，但残留两个洞：

1. **接管要求同进程**：回放时用户刚点过「回放」按钮，**前台常常是 andy-harness 自己的窗口**（harness.exe），与 editplus.exe 不同进程 → `_adopt_modal_window` 返回 False → 仍然 RP-1007 挂起。
2. **window_focus 聚焦被禁用的窗口必失败**：`act_focus` 对 `SetForegroundWindow` 失败直接抛 RP-5001，把整轮钉死在这一步（比挂起更隐蔽）。

修改：
- `_modal_blocked(step)` 语义收窄为**只剩一种真遮挡**：`foreground()` 取不到（UAC 安全桌面等）。其余情况一律放行：FOCUS 动作放行并尝试接管；同进程前台 → 接管为新作用域；跨进程前台 → **降级放行**（`_modal_degraded=True`），让定位链在作用域子树里实打实地搜（UIA 里模态对话框通常是主窗口子节点，真机 structure_path 即 `Window[EditPlus]/Window[另存为]/Button[保存(S)]`），命中就执行，命中不到才是 RP-2001 且诊断包留全证据。
- **护栏**：降级期间 `allow_blind_coord=False`——只认 UIA/图像的确证命中，禁止隔着模态框凭坐标乱点。
- **不接管跨进程前台**：避免把后续点击带进 andy-harness 自己的窗口（比挂起更危险）。
- `act_focus`：目标窗口被模态禁用（`is_enabled=False`）→ **skip**（channel="skip"），与"目标窗口已关闭 → skip"同一策略，不再 RP-5001。
- 增加 warning 日志：打印 `scope.title/exe` 与 `fg.title/exe`，下次一眼看出前台到底是谁。

### 复现用例（屏蔽修复即失败）
| 用例 | 复现点 |
|------|--------|
| `test_modal_dialog_does_not_block_window_focus` | 对话框未可枚举+主窗口禁用+前台已切对话框 → 旧代码 RP-1007；新代码放行并接管作用域 |
| `test_click_in_dialog_with_drifted_title_not_rejected` | cls=#32770+标题漂移 → 旧代码 RP-4002 拒写；新代码 cls 强证据放行并正确定位对话框 |
| `test_window_focus_waits_for_dialog_to_appear` | 前 2 次枚举无对话框 → 旧代码沿用主窗口；新代码等待后命中 |
| `test_modal_blocked_degrades_when_foreground_is_another_process` | 前台是别的进程（harness 窗口）→ 旧代码 RP-1007 挂起；新代码降级放行且**不**接管跨进程窗口 |
| `test_window_focus_skips_when_target_disabled_by_modal` | 目标被禁用且聚焦必失败 → 旧代码 RP-5001（钉死）；新代码 skip 继续 |
| `test_desktop_repeat_executor.py::test_modal_blocked_suspend`（**语义更新**） | RP-1007 收窄为"取不到前台"一种场景；另增 `test_modal_disabled_window_no_longer_suspends` 锁定降级放行 |

---

## 新增功能：视觉辅助核对（百炼 VL，旁路）

- 动机：用户实测总能发现"回放行为与录制意图不符"类缺陷（s09 热键、s11 模态框）；逐步的 pre/post 帧已落盘，用图像理解模型做**事后核对**能自动给出"这一步实际做了什么/画面是否变化/是否生效"，把审查成本从逐帧看图降到看汇总清单。
- 模块：`harness/modules/desktop_repeat/vision/`（client.py：百炼 OpenAI 兼容 `/chat/completions`，纯 httpx、base64 Data URL、密钥解析 显式>env `DASHSCOPE_API_KEY`>key 文件；analyzer.py：逐步取 pre/post 帧 → 严格 JSON 回答 → 汇总 `suspects` 疑似未生效清单）。
- 接线：`compile_session` 末尾 `vision_enabled` 时后台任务（不阻塞解析返回）；结果落 bundle `vision.json` + 会话 `session.json.vision` 摘要 + DB；REST `GET/POST /sessions/{sid}/vision`。
- 真机验证（2026-10-20）：`qwen3-vl-plus` 在 dashscope 通用端点实测可用；对真机 bundle r-20261019-232142-5771d9 的 s10/s13 双帧分析成功。**注意**：用户免费额度的 `qwen3.8-omni-flash` 在通用端点返回 `AccessDenied.Unpurchased`（需 workspace 专属域名），故默认取 qwen3-vl-plus（按量计费，约 0.01~0.02 元/步，具体以百炼计费为准）。
- 成本护栏：`vision_max_steps`（默认 24）+ 无 pre/post 帧的步骤直接跳过 + 失败不重试超过 1 次。

## 未处理项（明确留待后续，非遗漏）

- P2-1 断点续跑接线（checkpoint 机制完整但 service 无 from_checkpoint 入口）——属功能缺口而非缺陷，需产品决策后接独立端点。
- P2-8 `_control` 等生效自满足、P2-9 password 死分支——现状可接受（状态经心跳最终收敛 / 纵深防御保留），仅记录。
- P3 全部（控制令牌字符串比较、embedded 挂起状态回传、locator 失败文案、NCC 精度、cd_done 死分支）——代码质量项，本轮不改以控制改动面。
- 真机 e2e（`logs/repeat-qa/e2e_phase1.py`）需非沙箱图形会话，本环境无法执行，交付前请在实机复跑一遍录制→解析→回放闭环。
- **复跑重点（s09）**：重录一次"地址栏输入 URL → 回车"流程，确认 `events.jsonl` 中 `key enter` 排在 `window_change` **之前**、其 `win/uia` 仍是地址栏 Edit；再回放验证不再挂起。存量旧 bundle 可直接在**不重录**的情况下回放，由 P0-9 的执行层兜底通关（但建议重录以获得干净的锚点）。
