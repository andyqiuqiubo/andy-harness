# andy-gateway 竞品分析与借鉴点评估

> 日期：2026-10-22 ｜ 分析对象：`dsh-our-free-model`（DeepSeek Harness 插件，v2.x main 分支，源码实读）+ 4 个同类案例
> 分析目的：基于**实际阅读源码**（非 README 转述）评估该插件的设计，提炼可用于优化 andy-gateway 免费模型接入功能的借鉴点
> 源码位置：`.tools/dsh-plugin/src/dsh-our-free-model-main/`（12.3 MB，GitHub `Ebony-Vinyl/dsh-our-free-model`，原 `zouyuxuan122` 仓库已迁移）

---

## 1. 执行摘要

`dsh-our-free-model` 是一个把多家免费模型网关（OpenCode Zen Free / Kilo / 十三个账号渠道）接入 DeepSeek Harness 的社区插件。通读其 47 个源码文件（约 1.1 万行）后确认：**它在"免费额度的脆弱性治理"上达到了生产级水准**，其核心价值不在于功能多，而在于对每一类上游故障都建立了精确的语义分层和对应的处置策略——这正是 andy-gateway 当前最薄弱的部分。

横向对比 9router、magpie、one-api/new-api、opencode2api 后，提炼出 **22 个可借鉴点**，按优先级分为：

| 优先级 | 数量 | 代表项 | 预期收益 |
|--------|------|--------|----------|
| **P0 立即修** | 4 | 错误分类与冷却精细化、流式 tool_calls 重编号、探测判定语义分层、纯思考空响应恢复 | 修复真实故障场景下的错误行为 |

> **实施状态（2026-10-22 当日）**：P0 四项已全部落地并通过测试（27 项网关单测 + 真实三通道端到端）。
> 落地形态：新增 `classify.py`（纯函数失败分类）、`stream_rewrite.py`（流式改写）、`HealthTracker`
> 双粒度冷却与退避等级、`StreamOutcome` 契约与空停恢复。P1 各项按报告顺序待做。
| **P1 近期做** | 8 | 响应体嗅探、思考预算阶梯、combo 容量交集、信任围栏、RTK 压缩、速率诚实口径、本地应答、密钥脱敏 | 可用性与成本显著改善 |
| **P2 评估后做** | 6 | Anthropic 端点、代理切换、自升级、意图路由等 | 按场景取舍 |
| **工程实践** | 4 | 模块 docblock 文化、重验记录表、fail-open、单程快照绑定 | 长期可维护性 |

**一句话结论**：andy-gateway 的骨架（路由/适配/租户/可观测）与同类产品处于同一水平，差距集中在**故障语义的精细度**和**流式正确性**两处——这两处都是免费额度场景下用户每天都会碰到的。

---

## 2. dsh-our-free-model 源码分析

### 2.1 整体结构

```
index.js               Host 半身：适配器注册、清单/可用性探测、webServer 路由、转发端口、
                       公告/升级/热重载接线（约 2900 行）
client.js              浏览器半身：手写 ModuleLoader bundle，**无构建步骤**（约 12,000 行）
adapter/kernel.js      内核接缝：全包唯一允许 import @deepseek-ai/* 的位置
src/
  adapter.js           结构性 LlmAdapter（duck typing，不 import 内核基类）
  upstream.js          网关身份：UA、session id 铸造、工具指纹、按模型选端点
  catalog.js           清单 = 上游 listing + 本地能力表（双层来源）
  probe.js             可用性探测 + 出口 IP 检测
  http.js              出站 HTTP、SSE 提取、失败分类为宿主中性错误码
  stream.js            三种线协议 → StreamChunk 投影 + token 不相交计数
  messages.js          harness 消息 ↔ 各线协议 + 工具调用配对修复
  effort.js            思考强度：档位菜单 / 预算阶梯
  forward.js           独立 OpenAI 兼容监听器（另含 Anthropic Messages）
  combos.js            降级链：虚拟清单行
  route/classify.js    全车道统一失败词汇与冷却策略
  trust.js             插件路由的请求信任围栏
  egress.js            可切换出口（代理 / mihomo 订阅）
  recovery.js          长思考截断后的有界恢复
  token-saver/         工具输出压缩（RTK）+ 提示词瘦身
  local-optimize.js    五个本地应答（不花上游额度）
  store.js             JSON 存储（合并写 + 原子 rename + 损坏保护）
  feed/updater/reload  公告 feed / 应用内升级 / 自热重载
  providers/           75 家聚合提供商注册表
```

### 2.2 六个关键设计（均经源码核实）

#### ① 最小依赖声明 + 优雅降级链（index.js:107）

```js
export const inject = ['llm']   // 唯一硬依赖
```

注释记录了完整推理：cordis 会 withhold 未声明服务并使 fiber 永久 PENDING——v1.2.1 曾因此在无 web server 的组合中永久静默（issue #4）。`webServer` 通过**嵌套 `ctx.inject` fiber** 单独等待，仪表板路由在 web 服务就绪后才注册；`ctx.get()` 做机会性查找（服务未 provide 时返回 undefined 而非抛错）。

**评估**：这是插件化架构的正确姿势——andy-gateway 目前是内核模块（`_init_components` 常驻装配），更简单也合理；但"核心能力不依赖可选设施"的原则同样适用于我们的 tracing 集成（已做到：插件缺席静默跳过）。

#### ② 上游身份的版本化追踪（upstream.js 头部注释）

这是全包信息密度最高的文件。头部记录了**四轮实测定期重验**，每轮列出变更项与失败模式：

| 日期 | 变更 | 失败码 |
|------|------|--------|
| 2026-09-24 | 初版：公共凭据、指纹头、按模型端点分流、免费档工具闸门（无它 403） | FreeTierError |
| 2026-10-14 | 车道迁移到 `/inference/v1`；必须同时发 `x-opencode-session-id`；session id 必须铸造而非派生 | 403 |
| 2026-10-16 | v2 指纹：UA 改 `opencode/latest/2.0.26/cli`；**去掉** `x-opencode-request`；工具闸门从"4 个名字各带占位 property"升级为"read/shell 两个名字且 description 与 schema 逐字一致" | 403 |
| 2026-10-17 | session id 增加**编码校验**：时间部分必须是 `~(ts_ms << 12 \| counter)` 降序值，低 12 位落在 `0xfc0–0xfff`；升序写法或 sha256 摘要即使形状正确也拒 | 403 |

工具定义逐字复制自官方 v2.0.26 CLI（`FINGERPRINT_TOOL_DEFINITIONS`，注释明确警告"不要为了好看动这些字符串"）；因为 v2 的闸门比对定义本身，"桩工具 + 占位 property" 技巧已失效，只能用提示词压（`placeholderNote` 的措辞是实测选出来的：写成"不可用"会让模型连调用方自己的工具也拒绝）。

**评估**：这份重验表与我们的 `docs/andy-gateway-本机免费模型接入-v1.1.md` 中的协议要点是同一思路（我们记录了 Cline token 1 小时轮换、WorkBuddy UA 10085、Zen 头族）。**可借鉴的是把重验记录做成源码内嵌的表格**（含日期/失败码），而非只写在文档里——上游再变动时，改代码的人第一眼就能看到历史。

#### ③ 可用性判定的语义分层（probe.js `stateOf`，全包最值得抄的函数）

```js
const ROUTING_REFUSAL_STATUS = new Set([400, 404, 422])  // 网关"点名拒绝该模型"
```

判定规则（注释原文的推理链）：

- **unavailable**（从选择器移除）：状态码 ∈ {400, 404, 422}，或文案含 `unavailable|not supported|no such model|unknown model|invalid model`——**且仅当状态码 < 500 时**才采信文案（503 的 reason phrase "Service Unavailable" 曾被误读为模型判定，把一个可用模型踢出选择器，直到下一轮才回来）；
- **region-blocked**（单独分组）：`RegionError`；
- **throttled / unknown**（保持可见）：429、5xx、超时、断网、`laneLocked`——"网关自身的故障不是对模型的判定"；
- 注释写明非对称性的理由："选择器里一条陈旧记录浪费用户一次可重试的失败回合，而一次 hiccup 就让模型消失，消耗的是用户毫无感知的 reprobe-再等待周期"。

探测本身也精细：并发恒为 2（该车道按 session 计配额，宽扇区会限流要测 avail 的用户）；探测流内检测 200 状态里的错误帧（`{"type":"error"}` 出现在 delta 位置——只问"有没有 delta 像 delta"曾把一个每轮必败的模型标成绿标）；出口检测三源 fail-open（ipify/ipinfo/ipapi，任一应答即可）。

**评估**：我们的 `HealthTracker` 只有 `available()/mark_failure()/mark_success()` 三态布尔 + 单一 cooldown；`probe()` 返回 bool。**这是 andy-gateway 最大的单点差距**：一次 503 就把通道冷却 30s，与"模型被点名拒绝"混为一谈。详见 §4-P0-3。

#### ④ 失败词汇与冷却策略（route/classify.js，移植自 9router ERROR_RULES）

纯函数（无时钟/IO/随机数），两个权威源合并：

- **kind/scope/retryable**（FCC 词汇）：`invalid_request / context_window / auth / permission / billing / rate_limit / overloaded / timeout / upstream / unavailable / aborted` 十一类；
- **cooldownMs/backoff**（9router 策略表）：文本规则**优先于**状态码规则。

| 规则 | 冷却 | scope |
|------|------|-------|
| 文案 "no credentials" | 2 min | account |
| 文案 "request not allowed" | 5 s（等刷新即可，不是 2 min） | account |
| 文案 "improperly formed request" | 2 min（provider 自己建的 payload 坏了，仍冷却账号） | account |
| 文案 rate limit / too many requests / quota exceeded | 指数退避 | account |
| 文案 capacity / overloaded | 指数退避 | **provider**（全站压力，非凭据耗尽） |
| 状态 401/402/403/404 | 2 min | account |
| 状态 429 | 指数退避 | account |
| 未匹配（尤其未匹配 4xx） | 30 s 瞬态 | **request**（重放同一 body 复现拒绝，不冷却任何东西） |

指数退避 `2000ms × 2^(level-1)`，封顶 5 min / level 15；provider 自报的 retry-after **钳制在 30 min**（"provider 确实会报多小时的重置，照单全收会让链路静默清空"）。429 退避等级在成功后**归零**（9router `resetAccountState`）。

注释特别标注了两处与 FCC 的分歧及理由（402 记 BILLING 而非 PERMISSION，因为"去充值"才是可行动答案；404 记 UNAVAILABLE 而非 UPSTREAM，因为账号根本不再路由该模型）。

**评估**：我们的 `map_error`（openai_like.py:171）目前只有四条：401/403→CRED_INVALID 300s（credential 级）、429→30s（credential 级）、5xx→30s（channel 级）、其他→UPSTREAM_ERROR 不冷却。缺文本规则、缺 scope、缺退避等级、缺 retry-after 钳制、缺"未匹配 4xx 不冷却"。详见 §4-P0-1。

#### ⑤ 流式正确性的一组细节（stream.js + forward.js）

- **DSML 洗涤器**：DeepSeek v4 系在 reasoning→action 边界会泄漏内部控制标记（实测 `\n\n<｜DSML｜ calls>` 以 content 流出）。洗涤器把"可能补全成开标记"的尾巴扣住不放，直到下一个 delta 能判定；close 时把剩余 spilled 或丢弃。注释明说代价："用户在回答里逐字引用该 token 会丢这些字符——故意的，控制标记例行泄漏更糟"；
- **tool_calls 键解析**（`toolKey`）：wire 的 `tool_calls[].index` 是累积键；没有 index 的形状用 call id 匹配已开块；都没有就延续前一个。只用一个常量键会把并行调用合并成一个块、id 互相覆盖；
- **转发时 index 重编号**（forward.js 头注）：上游块 index 与 reasoning/text 共享编号空间，思考回合的第一个工具调用常以 **index 2+** 到达，而每个按 index 累积的 OpenAI 客户端拿到稀疏数组——合并或丢弃参数（issue #20 "forwarded clients cannot call tools"）；
- **指纹工具抑制**：免费档闸门要求声明 read/shell，模型真去调时，客户端会收到一个它从未注册的工具，回报一个它无能为力的错误——转发时把这些调用拦掉；
- **诚实速率**（`windowTokens`/`decodeWindow`）：291 个未流出任何帧的 reasoning token 曾在第一个可见 token 前生成完毕，把速率算成 2941 tok/s（真实约 40）。现在分子只计"能容纳它的那段时间"内落地的 token，看板用 Σtoken/Σ秒，**不对各次速率取平均**（一个 1 ms 窗口曾把 26 次调用的均值抬高三个数量级）；答案集中在一两个大帧的模型不显示速率（显示 —）。

**评估**：我们的 `_stream` 直接透传 SSE 行，`aggregate_stream` 非流式路径自己累积（index 从 `tc.get("index", 0)` 原样取）。**转发路径没有重编号**——这是会真实咬到 OpenAI 兼容客户端的 bug。详见 §4-P0-2。

#### ⑥ 长思考截断恢复（recovery.js，issue #12）

触发条件：首段**仅有非空思考、无正文、无工具调用**，且上游未发正常结束帧（断流）或发了 stop 但只有思考（宿主会把"空停"判成空响应）。恢复动作：把思考检查点封进 instruction，要求"用已有结果直接交付最终答案、不要重启分析、800 词内"，续写一次。不触发：用户取消、已输出正文/工具的正常结束、达输出上限、明确上游错误。另有 `continuationMessages` 处理 finish=length 的截断续接（issue #28：宿主只显示"发送 继续 恢复"，用户得手动）。

**评估**：这直接命中我们文档记录的坑——"hy3、Cline M3.1 在 max_tokens 过小时把预算耗在 reasoning，content 为空，建议 ≥256"。checkpoint 续写比"建议用户调大参数"优雅得多。详见 §4-P0-4。

### 2.3 其他值得记录的工程设计

| 设计 | 位置 | 要点 |
|------|------|------|
| **信任围栏** | trust.js | 插件路由前缀长于内核 `/api`，dispatch 最长前缀获胜→先于内核准入检查。两层：优先桥接宿主 `connection` 服务（同时认 `admit`/`requestRejection` 两个世代的方法名和 `{rejection}` 信封/裸状态两种返回），缺失时退回结构化围栏（仅 loopback、`sec-fetch-site: cross-site` 拒、Origin/Referer authority 匹配、桌面 `dsh-app://app` 中继窄例外）。服务抛错时**拒绝**而非放行（"永远不能因为宿主服务故障绕过浏览器认证"） |
| **loopback 绑定解析** | forward.js:180 | hostname 必须 DNS 解析且**每个**答案都是 loopback 才允许 listen——否则 hosts 文件/企业 DNS 把 localhost 指向可路由地址时，字符串检查放行而 OS 把额度送给整个子网（issue #19） |
| **绑定失败分类** | forward.js:219 | Windows 上被 `0.0.0.0` 通配符占用的端口对 loopback bind 返回 **EACCES 而非 EADDRINUSE**（常见 owner：netsh portproxy / Hyper-V WinNAT 预留），读错会让人去查权限。绑定策略：指数退避重试 4 次 → 附近 10 个端口扫描 → 兜底 ephemeral，真实端口始终发布给调用方 |
| **响应体嗅探** | http.js | 网关高负载时以 200 + `application/json` 返回完整 SSE 帧序列。信 header 曾让整轮报废（`response.text()` 后 JSON.parse 失败），且错误对象带 `status:200` 会把可用模型误判为不可路由。现在嗅探首块（≤4KB）按形状分流，**已读字节重新注入流** |
| **transportCause** | http.js:75 | Node fetch 把 DNS/TCP/TLS 拒绝全塌缩成 `TypeError: fetch failed`；沿 `cause` 链走 3 层取出 ENOTFOUND/ECONNRESET/证书警报，拼成 "fetch failed — getaddrinfo ENOTFOUND opencode.ai (ENOTFOUND)"（issue #79） |
| **combo 容量交集** | combos.js | 链首支持 vision 不意味着 fallback 也支持；1M 窗口的链首会把 128K 的 fallback 撑爆。虚拟模型广播的能力是**每个候选都能兑现的交集**，不是并集。存储值全部当不可信输入处理 |
| **prepareCall 快照绑定** | adapter.js:151 | 模型元数据与 dispatch 闭包绑到同一个 state 快照——回合中途到来的 catalog 刷新不会把一代的容量表和另一代的端点表混用 |
| **扁平重试策略** | adapter.js:73 | `providerRetryPolicy()` 必须返回扁平对象：嵌套在 `backoff:{}` 里会让调度器读到 `undefined`，`undefined * 2^n = NaN`，持久会话日志拒绝非有限值，**可恢复的瞬时故障升级成整轮报废**。429 故意不进 retryableCodes（该车道 429 带递增 retry-after，两次自动重试把每堵配额墙变成三堵） |
| **store 损坏保护** | store.js:56 | 读失败时把损坏文件复制为 `.corrupt-<ts>` 保留，而不是让下一次 flush 用默认值 rename 覆盖——否则用户的转发 Key、已读公告全部丢失且无可恢复 |
| **升级安全** | updater.js | 下载 → SHA-256 校验 → stage → 备份（pending/previous 双rename）→ 原子替换 → 校验回读 → 热重载，任一步失败回滚；manifest 签名 + 逐文件哈希 |
| **generation 计数器** | index.js:146 | 热重载在 `globalThis` 上维护代号，旧闭包不得在 dispose 后碰存储（会与新实例的写入竞态） |
| **分发模式** | index.js:127 | `self`（自升级/自公告/自热重载）vs `managed`（安装包管理者拥有字节，三项待机）——"两个写者一个安装目录=损坏的安装" |
| **本地应答** | local-optimize.js | 五个 Claude Code 固定问题本机应答：额度探测、权限提示前的命令前缀、会话标题、建议模式、文件路径提取。每个检测器只读请求形状，不匹配就原样不动 |
| **RTK 压缩** | token-saver/ | 工具输出就地重写（git diff 40KB 移除 90% 字节）；提示词瘦身两族默认全关；**全部失败开放**（压缩器抛错跳过、过滤器抛错原样放行——"请求绝不能因为一个本来为让它更便宜的环节而失败"）；按字节留痕供看板显示测量值而非声明值 |

---

## 3. 同类案例横向对比

| 维度 | dsh-our-free-model | 9router (open-sse) | magpie (yetone) | one-api / new-api | andy-gateway（我们） |
|------|-------------------|--------------------|-----------------|-------------------|---------------------|
| 定位 | dsh 宿主插件，免 Key 车道 + 13 账号渠道 + 75 聚合商 | 本地路由网关（桌面） | 桌面网关（Go，menu bar） | 服务端网关（Go，多租户计费） | 后端内核模块 |
| 协议适配 | 3 种上游线（chat/messages/responses）+ 2 种客户端面（OpenAI/Anthropic） | OpenAI↔Claude↔Gemini↔Responses 互转 | IR 中间表示，4 种 wire API | 30+ 渠道适配 | openai-like profile（Cline/WB/Zen） |
| 失败分类 | kind+scope+文本优先规则表 | ERROR_RULES 文本优先（dsh 的源头） | 分级常量（out-of-credit 30min / out-of-quota 15min / 限流照厂商 / 重复失败升级 10min） | 渠道优先级 + 重试 | 4 条 map_error 规则 |
| 退避 | 2s×2^(n-1) 封顶 5min，level 15；provider 值钳 30min | 同左（dsh 的源头） | 照厂商 Retry-After 与重置头，"不瞎猜也不硬撞" | 固定重试 | 固定 30s |
| 锁粒度 | (account, model) 双粒度（modelLock_$model） | modelLock_${model} 扁平字段 | per-key/account 锁 + 重置时间感知 | 渠道级 | **channel 级** |
| 路由策略 | fallback / round-robin（combo 链） | Round Robin / Fill First / Combo fusion（扇出+judge 综合） | smart（重置最早先用→保 prompt cache 温热）/ order / rotate / usage（半衰期 1h 衰减） | 优先级+权重 | 5 种（weighted/ RR / least_inflight / least_latency / sticky） |
| 会话亲和 | session 绑定（per-session 配额） | — | affinity.go（auto/session/turn/off，明说 prompt cache "值真金白银"） | — | sticky 策略 |
| 意图路由 | — | — | decide.go 问小模型分类意图（组 effort=auto） | — | 无（不建议做，见 §5） |
| 凭据安全 | 0600 settings.json；API 只回显 `hasKey` + 后四位；密封车道由宿主指纹把守 | OAuth 自动刷新 | "从不读 shell 里的密钥"；密钥只留在 magpie | 渠道 Key 与令牌完全隔离 | Fernet + machine-id 加密 |
| 配额防超卖 | 按 session 计额（稳定 session id 是设计核心） | 实时配额追踪 + 重置倒计时 | 额度透视 | 预扣/实扣差额退回 | RPM/TPM estimated 检查 |
| 可观测 | 本机看板（17 周热力图/速率/首字）；请求 trace 定长诊断（绝不序列化 headers/bodies） | 仪表盘 + 调试日志 | Call 记录（最近 40 条）实时路由视图 | 用量/成本报表 | gw_usage + audit + Prometheus 指标 + span |
| 升级/热更 | SHA-256 分级校验 + 原子替换 + 回滚 + 热重载 + generation 防竞态 | 云同步 | 应用内更新 | 版本发布 | 配置版本化 + 2s 热更新 + 回滚 |

**从表中看到的定位差异**：one-api/new-api 解决"组织内多租户分发与对账"，magpie/9router/dsh 解决"个人把多家免费/订阅额度喂给多个 agent"。andy-gateway 的语境与后者一致，因此**免费额度的脆弱性治理**（故障语义、流式正确性、额度节俭）优先级高于计费与集群特性。

---

## 4. 可借鉴点清单（按优先级）

### P0-1 错误分类与冷却策略精细化（对应 classify.js / 9router ERROR_RULES）

**现状**（openai_like.py:171-209）：4 条硬编码规则；401/403 一律 credential 级冷却 300s；429 固定 30s；5xx channel 级 30s；其余 4xx 不冷却不转移。

**问题**：
1. 一次 400（用户自己 body 有问题）之后，若混入 429 语义或账号级误判，健康凭据被无谓冷却；
2. 429 逐次撞墙都是同样 30s，没有递增，退避无效；
3. 上游自报 `Retry-After: 3600` 时我们只当 30s，立刻重试再撞；
4. "no credentials"（凭据真没了）与 "request not allowed"（等刷新即可）混为一类 300s；
5. 熔断是 channel 级——一个模型被限流，同通道其他模型陪葬（9router 用 `modelLock_${model}` 扁平字段做双粒度）。

**改造方案**（`adapters/openai_like.py` + `router.py`）：
- 引入纯函数 `classify_failure(status, body, retry_after) -> (kind, scope, cooldown_ms, retryable)`，规则表：文本优先（`no credentials`→2min/account、`request not allowed`→5s/account、`rate limit|too many requests|quota exceeded`→退避/account、`capacity|overloaded`→退避/provider、`improperly formed`→2min/account），状态码兜底（401/402/403/404→2min/account、429→退避），**未匹配 4xx→request scope 不冷却**（重放同一 body 复现拒绝，冷却账号只会隐藏真实原因）；
- 退避等级按 `(channel_id, model)` 记：`2000ms × 2^(level-1)`，封顶 5min，level 15；`mark_success` 归零；
- `Retry-After` 解析后钳制在 30min；
- `HealthTracker` 键从 `channel_id` 改为 `(channel_id, model)`：`available(channel, model)`，任一模组在冷却即对该模型不可用——一个模型的墙不推倒同通道的其他模型。

### P0-2 流式透传的 tool_calls 重编号 + 指纹工具抑制（对应 forward.js 头注）

**现状**（service.py `_stream`）：逐行透传 `data:` 帧，OpenAI 语义完全依赖上游编号。

**问题**：上游块 index 与 reasoning/text 共享编号空间，思考回合的第一个工具调用常以 index 2+ 到达；任何按 index 累积的客户端（OpenAI SDK、Cline、OpenCode）拿到稀疏数组→参数被合并或丢弃。另外 `requires_tools` 通道注入的 `ZEN_CORE_TOOLS` 若被模型调用，客户端会收到它没注册的工具。

**改造方案**：透传时对每个 `data:` 帧做轻量改写（只动 `choices[].delta.tool_calls`）：
- 维护 `index_remap: dict[上游index, 0..n]`，首个工具调用出现时起从 0 连续编号；
- 抑制指向指纹工具名（`read`/`shell` 或 `ZEN_CORE_TOOLS` 内名字）的 tool_call delta——客户端从未注册它们，回过去只是个无法处理的错误（dsh 的处置：整个调用拦掉，宁可没有也不能给个死的）。

### P0-3 探测判定的语义分层（对应 probe.js `stateOf`）

**现状**：`probe()` 返回 bool → `mark_success/mark_failure`，一次失败冷却全通道。

**问题**：上游 503（网关自身过载，免费池最常见）被当作通道故障冷却；而 404/400+明确文案（网关不再路由该模型）却只冷却 30s 后重试。两者语义正好颠倒。

**改造方案**：`probe()` 返回 verdict 而非 bool，映射到 `HealthTracker`：
- `MODEL_DENIED`（400/404/422 或文案点名，且状态 <500）：该 `(channel, model)` 长冷却（如 10min），`/v1/models` 标注"暂不可路由"但**不删除**；
- `GATEWAY_TROUBLE`（5xx/429/超时/断网）：短冷却（如 30s），模型保持可用；
- `REGION_BLOCKED`（地区文案）：归入 region-limited 分组；
- 出口 IP 变化时重跑探测轮（dsh：VPN 开关改变"哪些模型对用户存在"，选择器必须无重启跟随）。

同时修 `stateOf` 的文案采信前提：**状态 ≥500 时绝不采信文案**（"Service Unavailable" 是每个反代的 reason phrase）。

### P0-4 纯思考空响应的一次性恢复（对应 recovery.js）

**现状**：非流式聚合后直接返回；`hy3`/M3.1 系列在 `max_tokens` 小时 content 为空（文档已记录该坑，当前解法是"建议用户 ≥256"）。

**改造方案**：`aggregate_stream` 返回时携带 `saw_reasoning / saw_text / saw_tool_call / saw_finish` 标志；`service.chat` 非流式路径检测"仅有思考"→ 用 `recoveryMessages` 形状的 instruction（"上一段分析中断于最终答案前，用已有结果直接交付，800 词内，不要重启分析"）续写一次，仍空才返回错误。**有界**：总时窗（如 30min）内只恢复一次；用户取消/已达上限/明确上游错误不触发。流式路径同理：首段只有 reasoning delta 且流异常关闭时，可选择静默转非流式恢复或明确报错（建议后者，避免客户端已收到的部分重复）。

### P1-1 响应体形状嗅探（对应 http.js）

上游高负载时以 200 + `application/json` 返回完整 SSE 帧序列（dsh issue #6：信 header 让整轮报废，且错误对象带 `status:200` 反过来把可用模型误判为不可路由）。我们已有 Cline 非流式 `{"data": ...}` 包装的 unwrap，但没有"流式响应以 JSON content-type 到达"的处理。**方案**：读首块 ≤4KB 嗅探形状，SSE 帧序列按流式解析（已读字节重新注入），完整 JSON 按非流式解析。低成本高保险。

### P1-2 思考强度作为真实预算（对应 effort.js）

`build_request` 目前原样透传 `max_tokens`。两类控制值得引入：
- 通道 profile 声明该上游**接受** `reasoning_effort` 的（Cline/Zen 均在其列）→ 客户端传 effort 时按档位表映射原值下发，集合外值归一到最近档；
- 上游忽略 effort 的（WorkBuddy hy3）→ 预算阶梯（light 2048 / balanced 8192 / deep 模型上限），"思考不可关"的模型整体翻倍。

原则照搬 dsh：**不能强制执行的档位不声明**；档位描述由决定请求的同一个函数生成（"广告 8K 而线上发 16384"是与"设置页显示错数字"同类的缺陷）。`/v1/models` 随之暴露 reasoning 能力。

### P1-3 combo/别名的容量交集与模态过滤（对应 combos.js）

`free/auto` 别名当前是固定顺序故障转移，不校验成员能力。引入：虚拟模型的能力 = **成员交集**；请求带 image 时只路由到 vision 成员（9router combo.js 同样只扫**当前轮**的模态，不被历史钉死）。这同时修复"别名广播了链首的 1M 窗口，fallback 128K 被撑爆"。

### P1-4 数据面信任围栏（对应 trust.js + resolveLoopbackBind）

`/v1` 目前只靠 `agw-` Key。补两层（可配置）：
- 绑定层：监听地址若为 hostname，DNS 解析后**每个**答案必须 loopback（防 hosts 文件把 localhost 指向可路由地址——字符串检查放行而 OS 把额度送出去）；
- 请求层：可选 Origin/Referer authority 匹配 + `sec-fetch-site: cross-site` 拒绝（浏览器页面不能被诱导跨站调用我们的数据面）。
另记录 Windows 端口占用经验：被 `0.0.0.0` 通配符占用的端口对 loopback bind 报 **EACCES 而非 EADDRINUSE**（owner 常是 netsh portproxy / WinNAT），诊断时别去查权限。

### P1-5 RTK 工具输出压缩（对应 token-saver/）

免费额度场景，输入侧最大的浪费是 agent 轮次里的工具结果（git diff 40KB 实测可移除 90% 字节且模型仍能就它作答）。我们本机已装 RTK（Rust Token Killer，`D:\RTK`），可在网关侧做 `tool_result` 就地压缩：**失败开放**（压缩器抛错跳过、过滤器抛错原样放行）、**按字节留痕**（看板显示测量值而非声明值）、仅压缩不认识的内容不动。这对"用免费模型驱动 andy-repeat 这类重工具回放"直接省钱。

### P1-6 速率的诚实口径（对应 stream.js `windowTokens`/`decodeWindow`）

`gw_usage` 已有 ttfb；补 tokens/s 时注意：reasoning token 在第一个可见 token 前生成完毕，用整段耗时除以答案落地秒数会算出 2941 tok/s 这种假数（真实 40）。规则：分子只计可计时窗口内落地的 token，聚合用 Σtoken/Σ秒，**不对各次速率取平均**（一个 1ms 窗口曾把均值抬高三个数量级）；答案集中在大帧落地的模型不显示速率。

### P1-7 本地应答（对应 local-optimize.js）

识别五类不需要上游的固定问题本机应答：额度探测（Claude Code 的 quota probe）、权限提示前的命令前缀、会话标题、建议模式、文件路径提取。每个检测器只读请求形状；不匹配原样转发。对免费额度是纯省。

### P1-8 密钥脱敏与存储细节（对应 aggregate.js + store.js）

- 管理面任何 API **只回显** `hasKey` + 后四位指纹，绝不给明文（我们凭据已 Fernet 加密，需核对 admin 序列化路径无泄漏点）；
- 含 Key 的文件 0600；
- store 读损坏时保留 `.corrupt-<ts>` 而非默认值覆盖。

### P2 评估后做

| # | 借鉴点 | 来源 | 评估 |
|---|--------|------|------|
| 1 | `/v1/messages` Anthropic 端点 | forward.js/anthropic.js | 若未来要把 Claude Code 直接接到 andy-gateway 则做；当前自有 Agent 前端不需要 |
| 2 | 可切换出口（per-channel proxy / mihomo 订阅） | egress.js | region-blocked 场景的对策；中短期用环境变量代理即可，不做订阅管理 |
| 3 | 自升级 + 公告 feed + 热重载 | updater/feed/reload | 对**内核模块**价值低（随 harness 发布）；其中的 SHA-256 分级校验与 generation 防竞态可在做插件化时参考 |
| 4 | 双源模型发现（上游 listing + models.dev） | dsh-connect-opencode-free | 我们已有 `gateway.providers.json` 静态清单 + probe_url；可加"从 `/v1/models` 拉取 + 本地覆盖表合并"，任一源不可达沿用上次结论 |
| 5 | 配额预扣防超卖 | one-api/new-api | 免费额度无计费，RPM/TPM 已有 estimated 检查；仅当对外开放分发时考虑 |
| 6 | 意图路由（小模型分类） | magpie decide.go | **明确不做**：免费车道上加一次分类调用是纯浪费，与"额度节俭"目标相反 |

### 工程实践借鉴（不限于功能）

1. **源码内嵌重验表**：upstream.js 头部那种"日期/变更/失败码"表格，改协议相关代码的人第一眼看到历史。建议在 `adapters/openai_like.py` 头部补一张（Cline token 轮换 / WB UA 10085 / Zen v2 头族与 session 编码，均有实测日期）。
2. **模块 docblock 写"为什么"**：dsh 每个文件头解释存在理由与失败模式（含 issue 号）。我们 gateway 模块已有此文化，保持。
3. **fail-open 原则**：任何"为了更便宜/更快"的旁路环节（压缩、优化、本地应答）不得因自身故障让请求失败。
4. **单程快照绑定**：`prepareCall` 把模型元数据与 dispatch 绑到同一快照，防刷新混杂。对应我们的 `_apply_snapshot` 原子 swap——在途请求持旧快照的设计已一致，补一条注释说明即可。

---

## 5. 明确不建议采纳的点

| 点 | 来源 | 理由 |
|----|------|------|
| 身份伪装对抗层（逐字复制官方工具定义、session id 降序编码） | upstream.js | 我们已在接入文档标注 ToS 灰区与账号风险；dsh 把这些做进产品是对其分发模式的取舍，**andy-gateway 作为内核模块不宜加码**。维持"复现官方客户端头族"现状，不再加新伪装技巧 |
| fusion 策略（扇出多模型 + judge 综合） | 9router combo.js | 免费额度下扇出=加倍消耗，与节俭目标冲突 |
| 意图路由 | magpie decide.go | 见 P2-6 |
| 分布式集群 / Redis / 计费 | one-api/new-api | 单节点 SQLite 已满足个人网关；接缝已留（P5），不过度设计 |

---

## 6. 结论与下一步

**判断**：dsh-our-free-model 是同类插件中工程质量最高的一个（模块 docblock、重验记录、fail-open、语义分层都是可验证的源码事实而非 README 宣传）。它对 andy-gateway 的价值集中在两点：**把"上游故障"从单一 cooldown 细化为 kind×scope 的语义系统**，以及**把流式透传从字节搬运升级为协议正确的重写**。这两项都不需要新依赖，合计约 600–800 行。

**建议实施顺序**（每项独立可验证）：

1. **P0-2 流式 tool_calls 重编号 + 指纹工具抑制**（约 150 行，有明确回归场景：思考回合 + 工具调用）；
2. **P0-1 错误分类与 (channel, model) 双粒度冷却 + 退避等级**（约 250 行，含纯函数单测）；
3. **P0-3 探测 verdict 分层 + 出口变化重探**（约 150 行）；
4. **P0-4 纯思考空响应一次性恢复**（约 120 行，直接消灭"content 为空"类工单）；
5. P1 各项按需取用（嗅探和 RTK 压缩 ROI 最高）。

全部 P0 完成后，andy-gateway 在"免费额度治理"维度与 dsh-our-free-model 的差距将缩小到协议覆盖面（我们 3 通道 vs 其 13+1 车道），而那是渠道数量问题，不是设计问题。

---

*附：本报告引用的所有源码结论均来自 `.tools/dsh-plugin/src/dsh-our-free-model-main/` 实读（index.js / adapter.js / upstream.js / catalog.js / probe.js / http.js / stream.js / effort.js / recovery.js / combos.js / route/classify.js / trust.js / forward.js / egress.js / store.js / updater.js / token-saver/index.js / local-optimize.js 及 README 实现结构章节）；案例对比来自对 9router（DeepWiki + CSDN 架构解析）、magpie（源码走记 + 官网）、one-api/new-api（架构文档）、opencode2api（功能走查）的公开资料交叉核对。*
