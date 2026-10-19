# andy-repeat（操作录制与回放）全方位体检报告

- 日期：2026-10-19
- 范围：`backend/harness/modules/desktop_repeat/`（55 文件 / 约 11,169 行）+ `api/rest/desktop_repeat.py` + `plugins/andy_repeat/` + `frontend/src/views/RepeatStudioView.vue` + i18n 双语词典 + `tests/`（154 项既有单测全绿作基线）
- 方法：逐文件通读业务逻辑 → 对可疑路径写最小复现探针 → 新增专项用例集 `backend/tests/test_desktop_repeat_audit.py`（修复前 5 项全部失败坐实）→ 与既有"血泪清单"交叉核对是否回退
- 分级：P0 阻断/数据错误（必修）；P1 功能错误/明显体验（应尽快）；P2 健壮性/一致性（择机）；P3 代码质量（记录）

---

## 一、P0 阻断级（实测坐实）

### P0-1 录制器时钟域混用：事件时间戳 t 恒为 0，ts 完全错误
- 位置：`recorder/runner.py` L100-101（`_epoch0=time.time_ns()` 后 `_mono0=self._epoch0`），L123-127（`_t_of`/`_ts_of`）
- 根因：`_t_of`/`_ts_of` 的输入 `t_ns` 全部来自 `time.perf_counter_ns()`（钩子/worker 打点），而 `_mono0` 被赋成了 `time_ns()`（epoch 域）。两个时钟量级相差 20 个数量级，`t_ns - _mono0` 恒为巨大负数 → `max(0, ...)` 钳到 **0**。
- 影响（连锁）：
  1. `events.jsonl` 每条事件 `t=0` → 编译期 S5 等待推断（`waits.infer_timing`，gap>1500ms 才生成 pre_wait）**永不触发**，"录制时等页面加载 5 秒"回放时被压成固定 300ms 步进；
  2. 暂停区间（`pause_intervals`/`to_effective`）按 t 计算 → 恒为 0 宽区间，扣除失效；
  3. `ts`（epoch 时刻）= epoch0/1e6 + 负偏移 → 输出约 9×10⁷（开机秒数级），完全不是真实时刻；
  4. 心跳胶囊"已录 MM:SS"恒 00:00；
  5. 录制封版横幅/统计的时长失真。
- 复现：探针脚本输出 `t_of(5s后事件)=0，ts_of=90322596`。
- 为何单测没抓到：`tests/test_desktop_repeat_recorder.py` fixture 手动 `r._mono0 = time.perf_counter_ns()` 把基准"顺手改正"了——恰好掩盖了生产入口的赋值错误（脚本模式直跑才会暴露，与 MEMORY 中"import 单测测不出脚本模式"教训同源）。
- 修复方向：`self._mono0 = time.perf_counter_ns()`；`_ts_of` 用 `self._epoch0/1e6 + (t_ns-_mono0)/1e6`。

### P0-2 RunRecord.to_dict 丢字段 → finish_run 把 attempts_total/checkpoint/vars 覆盖回默认值
- 位置：`models.py` L564-571（to_dict 无 `vars`/`checkpoint`/`attempts_total`）→ `repository.py` L120-139（finish_run 从 dict 取这三个键）
- 根因：`finish_run` 兼容 dict 输入，正常终态收口传入的是 `RunRecord`，其 `to_dict()` 不输出这三个字段 → `d.get("attempts_total",0)`、`d.get("checkpoint",{})`、`d.get("vars",{})` 全取默认。
- 影响：
  1. `repeat_runs.attempts_total` 恒 0（重试统计/历史详情失真；`_sync_run_state`→`touch_run` 路径同样受影响）；
  2. `checkpoint_json` 收口时被覆盖成 `{}`——断点续跑数据（completed 序列）全丢；
  3. `vars_json` 被覆盖成 `{}`——运行历史看不到本次参数（脱敏后的）。
- 复现：探针输出 `False False False`；`test_desktop_repeat_audit.py::TestRunRecordPersistenceFidelity` 两用例失败。
- 修复方向：`to_dict` 补齐三字段（vars 已在 RunRecord 构造时经 redact_vars 脱敏，可安全序列化）。

### P0-3 双击去重依赖"seq 与归一化 eid 数值相等"的巧合，真实录制流错位时失效
- 位置：`recorder/normalizer.py` L300-305（double_click 携带 `supersedes_eid`，eid 是归一器私有计数）；`recorder/runner.py` `_write_stub`（**从未把 stub.eid 写入事件 data**）；`interpreter/merge.py` L125-132（用 `ev.seq` 与 `supersedes_eid` 比对）
- 根因：两个不同编号空间（写盘 seq：含 session_start/marker/pause 等系统事件；eid：仅归一化 stub 计数）被当作同一个数比较。真实流中只要首次 click 之前出现任何系统事件（session_start、elevated 告警 marker、pointer_move 摘要等），`seq > eid` → 去重永不命中 → 编译产物多出一次孤立 click 步骤，回放"先单击再双击"。
- 复现：`test_double_click_dedup_with_offset_events`（session_start+marker 前置 → 产出 `['click','double_click']` 而非 `['double_click']`）。既有 `test_double_click_drops_superseded_click` 用 `_ev(1,...)+supersedes_eid:1` 恰好 seq==eid，属"测试造数迁就了 bug"。
- 修复方向：`_write_stub` 落盘时写 `data["eid"]=stub.eid`（全类型统一附着）；merge 优先按 `data.eid` 判取代，无 eid 的旧 bundle 回退按 seq（保持向后兼容）。

### P0-4 作用域标题"假确认"：find_window 兜底命中后 `_scope_title_ok=True`，错误窗口内写操作被放行
- 位置：`platform/win32/windows.py` L214-222（四级放宽全 miss 后按 exe 返回第一个可见顶层窗口）；`executor/runner.py` `_ensure_scope` L534-541（`found is not None` 即 `self._scope_title_ok = True`，不校验 found.title）
- 根因：录制时窗口标题进入 `app_hint.window_title_regex`；页面导航/多开同 exe 时四级放宽落到 exe 兜底，返回的窗口标题**明显不匹配**期望正则，代码却把"标题已确认"置真 → `_scope_ok` 放行写操作。
- 影响：与 MEMORY 已记录的"18/18 succeeded 但目标应用无反应"假成功同链路——正是该血泪教训要求防住的场景（标题未确认必须拒写），当前实现存在确认绕过。
- 修复方向：`found` 命中后校验 `re.search(regex, found.title)`；不匹配时若前台窗口就是 found.hwnd（同窗口导航，MEMORY 既有契约）仍确认，否则 `_scope_title_ok=False`，写操作按既有契约拒绝（RP-4002），宁可失败不在错窗口点。

### P0-5 `_relaxed` 放宽标记跨步骤泄漏
- 位置：`executor/runner.py` L167（run 级初始化）、L412-413（置位后从不复位）、L637-640（对定位链生效）
- 根因：某一步定位耗尽触发"放宽重试"（name 编辑距离≤2 / 结构兄弟容差）后 `self._relaxed=True` 常驻，后续**所有**步骤的 UIA `name_type` 匹配都按放宽执行。
- 影响：真实 UI 上"保存/保存副本"、"确定/取消(近名)"这类编辑距离≤2 的近名控件极易被抢先命中，点在错误按钮上且 hit 记录显示 uia@0，排查困难。属于"越用越错"型缺陷。
- 复现：`test_relaxed_flag_does_not_leac_to_next_step`（s01 触发放宽后，s02 的 spec.relaxed=True）。
- 修复方向：`_run_step` 入口复位 `self._relaxed=False`（放宽仅对"触发它的那一步"的重试周期有效）。

---

## 二、P1 功能错误（应尽快修）

### P1-1 en.ts 缺失 RepeatStudioView 使用的 5 个键
- 位置：`frontend/src/i18n/en.ts`；缺 `repeat.discarded / repeat.noActive / repeat.paramHint / repeat.recEnded / repeat.runDone`
- 影响：英文界面下这些提示显示原始键名或空文本（停止录制反馈、"无进行中录制"、参数提示、录制收口、回放完成横幅——全是高频交互文案）。
- 复现：`test_repeat_i18n_keys_present_in_both_locales`。

### P1-2 录制覆盖层倒计时"3-2-1"实际只有 ~0.45 秒
- 位置：`recorder/overlay.py` `_loop`（`root.after(150,...)` 每 150ms 把 `state["cd"] -= 1`），`_apply("cd_done")` 分支无人投递（死代码）
- 根因：把"每秒减一"写成了"每 tick 减一"，tick 间隔是 150ms。
- 影响：用户看不到 3 秒准备窗口（设计 §3.7 明确倒计时期间不记录、给用户摆正窗口）；且倒计时数字闪现即消，与 `run()` 里 `self._stop.wait(cd+0.2)` 的挂钟时长不同步。
- 修复方向：按挂钟计算剩余 `max(0, ceil(cd_total - (now - start)))`，归零时 withdraw 倒计时窗、deiconify 胶囊。

### P1-3 CLI `record --stop` 结束不了后端进程里的录制（跨进程 active_session 失效）
- 位置：`cli.py` cmd_record_stop → `service.py` `stop_recording` L330-332：`sid = session_id or self._active_session`——CLI 是独立进程，新建 service 实例的 `_active_session` 恒为 None → 直接 RP-5004"当前无活跃录制会话"；但帮助文案承诺"再次运行 record --stop 结束"。
- 影响：CLI 路径的录制无法正常封版（除非手填 --session），用户只能回页面点停止或按热键。
- 修复方向：`stop_recording(None)` 且本进程无 active_session 时，扫描 sessions_root 找"draft + 心跳新鲜"的 bundle 自动接管（同 `_control` 同理受益）。

### P1-4 点击引发的窗口切换永远录不到 `window_change`
- 位置：`recorder/runner.py` `_win_context`（L302-309，先更新 `_last_fg`）与 `_detect_window_change`（L533 起，用 `_last_fg` 做 prev 比较）
- 根因：worker 处理点击 stub 时 `_win_context()` 已把 `_last_fg` 覆盖为新前台窗口，随后的差异检测 prev==curr → 永不发事件。只有"用户自己 Alt+Tab/点别的窗口"才录到 window_change。
- 影响：编译少一类 window_focus 锚点步骤（所幸后续步骤 app_hint 自带窗口信息，回放多能自洽），但"点开对话框→在对话框操作"流程的时间线与全屏留证帧缺失，语义/审计不完整。
- 修复方向：把"发现 fg 变化 → 发 window_change"收敛进 `_win_context` 的更新路径（先比较后更新，统一 `_emit_window_change`）。

### P1-5 帧 GC 从未接线，bundle 只增不减
- 位置：`service.gc()`（L968）与 `store/bundle.gc_session_frames` 实现完整，但**全仓无任何调用点**（scheduler、插件 activate 均无）。
- 影响：standard/forensic 档录制帧 30/7 天保留策略形同虚设，磁盘无界增长（forensic 一次录制可达数百 MB）。
- 修复方向：服务内挂低频周期任务（6h 一次，异常吞掉），插件 activate 的 async 上下文中创建。

### P1-6 `_await_resume` 超时中止使用未注册的错误码 RP-4005
- 位置：`executor/runner.py` L805-806；`errors.py` RP 表无此码 → `RepeatError("RP-4005")` 落到 `default_action="abort"、recoverable=False` 兜底，REST 语义与错误字典不一致。
- 修复方向：errors.py 注册 RP-4005（"人工裁决等待超时"，recoverable=True，action=abort）。

---

## 三、P2 健壮性/一致性

| # | 位置 | 问题 | 建议 |
|---|------|------|------|
| P2-1 | `executor/runner.py` 构造参数 `checkpoint` / `_resume_index` | 断点续跑**从未接线**：service 从不传 checkpoint（`_start_run_record`/REST 无 from_checkpoint），机制空转 | 本轮记录为已知缺口，后续在 service.resume 链路或独立 "rerun-from" 端点接 |
| P2-2 | `executor/pacing.py` `StepBudget.check` | 全链路无人调用 → "单步 12s 超时（RP-3003）"只有 resolve_chain 的 left_ms 间接兜住 | **已修**：在 `_act` 调用前补 `budget.check("执行前")`（触发点在注入之前，重试不会重复动作）。断言前**不**加 check：act 之后已注入，若此时判超时触发重试会造成非幂等步骤二次点击（提交/支付类），故只在 act 前保护。 |
| P2-3 | `executor/actions.py` `act_hotkey` | 注入后未 `mark_injected(None)`——若启用 human_takeover_detect，热键注入后的任何一步都可能被"系统输入比我们的新"误判成人工介入 | 成功即 mark |
| P2-4 | `executor/actions.py` `_paste` | `Ctrl+V` 发出后**立即** restore 剪贴板：目标应用异步读取与恢复写入竞态，大文本粘贴易被截和 | restore 前 sleep ~0.2s（已在 to_thread，安全） |
| P2-5 | `interpreter/merge.py` L143-147 | 键段静默边界 `ev.t - prev_eff`：ev.t 原始轴、prev_eff 有效轴，轴混用使跨暂停段间隔高估 → 多余分段（无害但不精确） | **复核后撤回**：录制层 `enter_pause` 已在暂停边界结算键段，跨暂停的键段现实中不存在；对历史/手改 bundle，膨胀判分段是"宁分勿并"的保守正确行为（防"暂停前+恢复后"并成一个输入步骤），改有效轴反而丢分段语义 |
| P2-6 | `RepeatStudioView.vue` `runBar` | `done` 只统计 `'done'`，不含 `'degraded'`（降级成功也是完成）→ 进度条虚低 | `['done','degraded','skipped']` |
| P2-7 | `config.py` L83-85 | `approval_policy` 注释称默认 `degraded`，dataclass 实际默认 `never`（2026-10-19 16:40 决议）——注释漂移误导后来者 | 注释与实现对齐 |
| P2-8 | `service.py` `_control` L316-323 | "等生效"循环自满足（自己刚写的 _rev 必然 >= 期望值，首轮即 break），实际未等待 recorder 消费 | 保持现状可接受（状态最终经心跳收敛），记录 |
| P2-9 | `safety_gate.py` L77-80 | `params.get('password')` 分支是死代码（编译产物无该键，密码路径已转 manual_confirm） | 保留（纵深防御），记录 |
| P2-10 | `stop_recording` 等待轮数 | 硬编码 140×0.15s≈21s，不可配——P1-3 的自动接管路径需要测试可缩短 | 提为 `cfg.stop_wait_rounds`（默认 140） |
| P2-11 | `tests/test_desktop_repeat_api.py` 的 `api` 夹具 | **全量跑才失败的测试隔离缺陷**：`harness.main._services` 是模块级单例；先跑的 `test_auth`/`test_admin_gate` 在 `HARNESS_AUTH=1` 下 import `harness.main`，把 `enabled=True` 的 AuthService 注册进该全局，而 monkeypatch 只回滚 env、不回滚注册表 → 本文件的"裸 app"误判认证已启用，SSE 端点 401（单跑该文件或只跑 repeat 八套件时全绿，掩盖了问题） | **已修**：`api` 夹具把 `harness.main._services` 换成空注册表，使 `_require_admin_sse` 走"无 AuthService → 放行"（该文件语义就是未启用认证） |

---

## 四、P3 记录级（本轮不改）

- P3-1 `runner.py` 控制令牌 `NEXT/SUSPEND/FATAL` 用字符串 + 调用方 `is` 比较：同模块字面量靠驻留巧合工作，脆弱。
- P3-2 `run_embedded_step` 在步骤挂起时把 `outcome("suspend")` 当 status 返回给 GraphEngine，混合工作流里表现为"状态字符串意外"。
- P3-3 `uia_locator.describe_failure` 未覆盖 `not_available/bad_response/error` 文案（前端提示退化为通用句）。
- P3-4 `image_locator._ncc_fallback` 粗搜步长可能错过峰值（无 opencv 时精度受限）——已在文档口径声明。
- P3-5 `overlay._apply('cd_done')` 死分支（P1-2 修复时顺带清理投递方即可）。

---

## 五、复查确认无回退的关键不变式（既往血泪项）

seq 单调（INV-R1）与 emit 锁内分配；BundleLock 陈旧锁接管；manifest 封版校验；`WinInputLease` 同进程幂等 + `start_run` 非终态 runner 互斥；RunBudget 分钟→秒换算；坐标校正不要求 rect 内；read_value 不回退 Name；type 弱锚点降级 focus 注入（真/干跑一致）；AltGr VK 回退；Alt+Tab 编译期转 window_focus；任务栏噪声剔除与"全噪声保留"兜底；window_focus 未找到→skip；find_window pid=0 跳过 pid 级；空标题不生成 `.*.*`；CREATE_NO_WINDOW；atomic_write 退避；suspend 超时收口；审批补发 token / 一次放行整轮 / 可信命中不写集合；HumanTakeover 默认关 + 三层降噪；重试复用同 sid StepRun；`touch_run` 增量落库；SSE `?token=`；ProgressStore 快照回放 + 终止事件收口；画布 start 独占首格。——本轮逐条核对代码，**均未回退**。

---

## 六、修复顺序建议

1. P0-1（时钟）→ 2. P0-3（eid 落盘+去重）→ 3. P0-2（to_dict）→ 4. P0-5（relaxed）→ 5. P0-4（标题确认）→ 6. P1-1（i18n）→ 7. P1-2（倒计时）→ 8. P1-3+P2-10（CLI 接管）→ 9. P1-4（window_change）→ 10. P1-5（GC 接线）→ 11. P1-6（RP-4005）→ 12. P2-2/2-3/2-4/2-5/2-6/2-7 → 回归：`pytest` 7 个既有套件 + audit 套件全绿 + `pnpm build`。

---

## 七、追加诊断：真机 s09 热键死锁（run-092d827a9a）

> 本章为实机复测后的补充：静态体检（前六章）未能覆盖"录制时序 + 执行层门闩"耦合出的**因果倒置**类缺陷。

### 现象

在 Chrome 地址栏输入 `dify.ys.fengsou.cc/apps` 后**没有按 Enter**（录制时是按了的），回放停在原地不动，前端提示：

```
回放等待人工 · s09：目标未找到（定位链耗尽）
诊断包 …\sessions\run-092d827a9a\diagnostics\run-092d827a9a\s09
```

即：s09（hotkey enter）的定位链永远搜不到目标 → 阶梯判 RP-2001 → `on_fail=suspend_manual` → 无限等待人工，整轮回放"假死"。

### 证据链（录制 bundle `r-20261019-231817-94e8d5` 的 `events.jsonl`）

| seq | type | t(ms) | 关键字段 |
|-----|------|-------|---------|
| 11 | key `right` | 12450 | uia = `Edit '地址和搜索栏'` |
| 12 | window_change | 15758 | → `工作室 - Dify`（Enter 引发的导航） |
| 13 | key `enter` | **15867** | uia = `Pane '工作室 - Dify - Google Chrome - qiu'`；win = `工作室 - Dify - Google Chrome` |

三处异常同向指向"回车被记录成了导航**之后**才发生"：

1. Enter 的 `t` 晚于它自己引发的 `window_change`；
2. Enter 的窗口上下文是**导航后**的 Dify 页面（而同段的 backspace / right 都是地址栏 Edit）；
3. Enter 的 UIA 锚点被绑到 Dify 根 Pane——一个"按下回车之后才存在"的元素。

编译产物坐实后果：`s09 action=hotkey params={"combo":"enter"}`，`loc[0] = name_type Pane[工作室 - Dify...]`，前置 `s08 window_focus` 要求标题匹配 `.*工作室.*`。

### 根因（四个，前三个在录制层，第四个是执行层独立缺陷）

| # | 级别 | 位置 | 根因 |
|---|------|------|------|
| P0-6 | 阻断 | `recorder/runner.py::_write_stub` | 键/文本事件用**结算写盘时刻**作时间戳。键段要等 1200ms 静默（`KEY_SEGMENT_SILENCE_MS`）或 150ms IME 防抖才 flush，normalizer 明明已记录按下时刻 `data["_t_ms"]`，却在写盘时被 `pop` 丢弃 → 回车落盘时刻晚于导航完成时刻 |
| P0-7 | 阻断 | `recorder/runner.py::_write_stub` | 写盘时**才**调 `_win_context()` 查前景窗口，此刻前景已是导航后页面 → 键事件的窗口上下文被污染成"按下之后才出现的页面" |
| P0-8 | 阻断 | `recorder/runner.py::_worker_loop_inner` | 循环顺序为"先 `_detect_window_change()`、再 `normalizer.tick()`" → 按键引发的 window_change 抢在按键之前写盘，seq 倒挂 |
| P0-9 | 阻断 | `executor/runner.py::_run_step` | **独立于录制层的执行层缺陷**：`act_hotkey` 完全不使用 `hit` 元素，定位链只是个"会失败的门闩"。锚点一旦绑错（真机这例）就形成"必须先定位到页面才允许按回车，而页面要等回车才存在"的因果倒置——重试多少次都不可能成功，且因 `suspend_manual` 表现为**无限挂起**而非失败 |

> P0-9 是"存量已编译工作流"的解药：即使 bundle 是旧录制（无 `_t_ms`、锚点仍绑错），执行层兜底后也无需重录即可通关。

（本轮还同步复核：`_key_flush` 合成的文本 stub 未继承段内真实按下时刻，与 P0-6 同源，一并归入修复。）
