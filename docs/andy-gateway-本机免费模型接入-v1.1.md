# andy-gateway 本机免费模型接入实施说明（v1.1）

| 项 | 内容 |
|----|------|
| 文档版本 | v1.1（接入实施） |
| 日期 | 2026-10-22 |
| 配套文档 | `docs/andy-gateway-设计方案.md`（v1.1，架构与模块设计） |
| 实施范围 | 本机已安装/已登录的 WorkBuddy、Cline、OpenCode Zen Free、Qoder CN |
| 交付物 | 可运行的 lite 网关 + 通道目录 + 探测/自检脚本 + 本文档 |

> **落地状态（2026-10-22）**：本说明中的三条通道已并入后端模块 `backend/harness/modules/gateway/`——后端启动即提供 `http://127.0.0.1:8000/v1`（Key/租户/用量/审计/指标/配置热更新齐备），`tools/andy_gateway_lite.py` 保留为脱离主程序的诊断与对照工具。
>
> **v1.2 修订（2026-10-22）**：Qoder CN 已通过 `qoder-cosy` 适配器接入（原"未接入"结论作废）；补充 COSY 签名 / 私有 base64、流式信封归一化、排队 10605 三层嵌套解包，以及"提问后没有任何反馈"的修复说明（§3.4）。文件名保留 v1.1 以维持既有链接稳定。

---

## 0. 结论摘要

**探测结果：4 家提供方共 20 个免费模型，均已接入；Qoder CN 通过专用 `qoder-cosy` 适配器接入（2026-10-22），其排队/流式信封已做归一化处理。**

| 提供方 | 免费模型数 | 接入方式 | 状态 |
|--------|-----------|----------|------|
| Cline | 6 | OpenAI 兼容 + 客户端身份头 | ✅ 已实测（HTTP 200，cost=0） |
| WorkBuddy（国内版） | 2 | OpenAI 兼容 + WorkBuddy 头族，仅流式 | ✅ 已实测（HTTP 200，credit=0） |
| OpenCode Zen Free | 10 | OpenAI 兼容 + OpenCode 头族 + 工具注入，仅流式 | ✅ 已实测（HTTP 200） |
| Qoder CN | 2 | 私有协议（COSY 信封：AES+RSA+MD5） | ✅ 已接入（qoder-cosy 适配器，2026-10-22） |

已交付一个**可直接运行的 OpenAI 兼容网关**（`tools/andy_gateway_lite.py`，默认 `127.0.0.1:8010`），支持流式/非流式、跨通道故障转移、用量落盘；andy-harness 的 ProviderRegistry 或 Cline/OpenCode 均可直接指向它。

**关键限制（必须在采用前知悉）**：

1. Cline 与 Zen 的免费额度带**客户端身份门禁**（Cline 校验 `X-CLIENT-*` 版本；Zen 明确要求"只能从 OpenCode 内使用"）。本方案按官方客户端的请求头复现，属于服务条款灰区，账号风险由使用者承担（见 §10）。
2. Qoder CN 不是 OpenAI 兼容协议，已通过 `qoder-cosy` 专用适配器接入（COSY 签名 + 私有 base64 body + 信封 SSE 归一化）。其免费模型存在上游排队（10605），网关按 **503 可重试、不冷却账号** 处理，见 §3.4。
3. Cline access token 仅约 1 小时有效，且 refresh token 一次性轮换；lite 已实现自动刷新并原子写回，但仍需关注写回失败/多进程竞争。

---

## 1. 探测范围与方法

### 1.1 本机现状

| 应用 | 版本/身份 | 登录态来源 |
|------|-----------|-----------|
| WorkBuddy（国内版） | 5.5.6，Free 套餐，账号 Andy Qiu | `~/.config/magpie/plugin-auth.json` → `workbuddy`（浏览器 OAuth） |
| Cline | CLI 3.0.68 | `~/.cline/data/settings/providers.json` → `providers.cline.settings.auth` |
| OpenCode Zen Free | 共享 `public` 凭据 | 无需个人 Key |
| Qoder CN | personal_professional_trial | `~/.config/magpie/plugin-auth.json` → `qoder-cn`（device token + machineId） |

> 说明：WorkBuddy/Qoder 的登录态当前由本机 Magpie 的插件保存；Cline 使用 Cline CLI 自己的登录态。lite 只读这些文件，不打印、不上传任何令牌。

### 1.2 方法

1. **模型目录**：调用各上游的官方目录接口（Cline `recommended-models`、WorkBuddy `/v3/config`、Zen `/models`），与 Magpie 的本地目录快照交叉核对。
2. **连通性**：对每个通道发起一次 `max_tokens` 很小的真实对话（不打印令牌，只记录状态码、时延、用量）。
3. **交叉验证**：同一模型分别经"直连探针"和"lite 网关"调用，确认归一化与故障转移逻辑一致。

工具：

| 文件 | 用途 |
|------|------|
| `.tools/probe_providers.py` | 直连探针（模型列表 / 最小对话），输出 JSON 报告 |
| `.tools/gateway_probe_report.json` | 探针结果（不含密钥） |
| `.tools/test_gateway_lite.py` | lite 网关的 ASGI 冒烟测试（不占端口） |
| `.tools/cline_refresh.py` | Cline token 刷新与原子写回（带备份） |

---

## 2. 免费模型总表（20 个）

> 标注"已实测"的是本轮真实发起过对话的模型；标注"目录内"的是上游目录确认免费、通道已通过对话验证，但未逐个调用。

### 2.1 Cline（6 个，`https://api.cline.bot/api/v1`）

| 模型 ID | 名称 | 状态 |
|---------|------|------|
| `cline-free/minimax-m3.1-flash` | Minimax M3.1 Flash | ✅ 已实测（200，"OK"，cost=0） |
| `reflection/beam-free` | Beam | ✅ 已实测（200，"OK"，cost=0） |
| `cline-free/nemotron-3.5-super-vl` | Nemotron 3.5 Super VL | 目录内 |
| `nvidia/nemotron-3.5-lightning-free` | Nemotron 3.5 Lightning | 目录内 |
| `poolside/laguna-s-2.1-free` | Laguna S 2.1 | 目录内 |
| `cline-free/solar-mini4` | Solar Mini 4 | 目录内 |

Cline 的免费组由官方 `recommended-models` 的 `free` 字段给出（本轮实时读取，与 Magpie 快照一致）。

### 2.2 WorkBuddy 国内版（2 个，`https://copilot.tencent.com/v2`）

| 模型 ID | 名称 | 上下文 | 状态 |
|---------|------|--------|------|
| `hy3` | Hy3 | 192K 输入 / 64K 输出 | ✅ 已实测（200，"OK."，credit=0） |
| `step-5-preview` | Step-5-Preview | 约 1M（Magpie 快照） | ✅ 已实测（200） |

> `step-5-preview` 不在当前 `/v3/config` 的 37 个模型列表中，但接口可正常调用，属促销/隐藏模型，随时可能下线。

### 2.3 OpenCode Zen Free（10 个，`https://opencode.ai/inference/v1`）

| 模型 ID | 上下文 | 状态 |
|---------|--------|------|
| `big-pickle` | 200K | ✅ 已实测（200，"OK"） |
| `beam-free` | 1M | 目录内 |
| `flint-40b-free` | 未知 | 目录内（新上架） |
| `mimo-v2.6-flash-free` | 200K | 目录内 |
| `nemotron-3.5-super-vl-free` | 1M | 目录内 |
| `nemotron-3.5-lightning-free` | 256K | 目录内 |
| `jev-1.13-free` | 未知 | 目录内（新上架） |
| `deepseek-v4.1-flash-free` | 200K | 目录内 |
| `glm-5.3-flash-free` | 200K | 目录内 |
| `minimax-m3.1-flash-free` | 1M | 目录内 |

Zen 的 `/models` 只返回 id，不返回免费标记；免费集合按 `-free` 后缀 + `big-pickle`（官方目录标注免费）判定。

### 2.4 Qoder CN（2 个，已接入）

| 模型 ID | 名称 | 上下文 | 状态 |
|---------|------|--------|------|
| `qfmodel` | Qwen3.8-Flash | 1M | ✅ 已接入（price_factor=0） |
| `dfmodel` | DeepSeek-Flash | 1M | ✅ 已接入（price_factor=0） |

---

## 3. 提供方协议详解

### 3.1 Cline

| 项 | 值 |
|----|-----|
| 对话端点 | `POST https://api.cline.bot/api/v1/chat/completions` |
| 鉴权 | `Authorization: Bearer workos:<accessToken>`（API Key 则直接 Bearer） |
| 模型目录 | `GET /api/v1/ai/cline/recommended-models`（无需登录）、`GET /api/v1/ai/cline/models` |
| 刷新 | `POST /api/v1/auth/refresh`，body `{refreshToken, grantType:"refresh_token"}` |
| 响应 | 非流式外层包 `{"data": {...}}`；流式为标准 OpenAI SSE |
| 免费门禁 | 读取 `X-CLIENT-TYPE/X-CLIENT-VERSION/X-CORE-VERSION/X-PLATFORM` |

必需头（与 Cline CLI 3.0.68 对齐）：

```http
Authorization: Bearer workos:<accessToken>
Content-Type: application/json
HTTP-Referer: https://cline.bot
X-Title: Cline
User-Agent: Cline/3.0.68
X-CLIENT-TYPE: cline-cli
X-CLIENT-VERSION: 3.0.68
X-PLATFORM: cli
X-PLATFORM-VERSION: 3.0.68
X-CORE-VERSION: 0.0.90
X-IS-MULTIROOT: false
```

注意事项：

- 版本号是"门禁比对值"，Cline 发版后需要跟着更新（配置在 `tools/andy_gateway_lite.py` 顶部常量）。
- access token 约 1 小时过期；refresh token 一次性轮换，**刷新后必须写回** `providers.json`，否则 Cline CLI 自身下次登录失效（lite 已实现：备份 + `os.replace` 原子写）。
- `max_tokens` 过小（如 16）时，推理模型可能把预算耗在 reasoning 上，上游返回 HTTP 500 `empty response content`；建议 ≥ 256 或不传 `max_tokens`。

### 3.2 WorkBuddy 国内版

| 项 | 值 |
|----|-----|
| 对话端点 | `POST https://copilot.tencent.com/v2/chat/completions` |
| 配置/模型 | `GET https://copilot.tencent.com/v3/config`（注意：不是 `/v2/v3/config`） |
| 鉴权 | `Authorization: Bearer <access>` + `X-User-Id` / `X-Domain` / `X-Product` / `X-IDE-Type` |
| 流式 | **仅接受 `stream: true`**；非流式由网关聚合 SSE |
| 默认系统提示 | 无 system 消息时 WorkBuddy 会补默认系统提示 |

必需头：

```http
Authorization: Bearer <access>
X-User-Id: 0d8ec857-...
X-Domain: www.workbuddy.cn
X-Product: SaaS
X-IDE-Type: WorkBuddy
User-Agent: WorkBuddy/5.5.6
X-Agent-Purpose: conversation
X-IDE-Name: WorkBuddy
X-IDE-Version: 5.5.6
X-Agent-Intent: craft
X-Requested-With: XMLHttpRequest
X-Conversation-ID: <uuid>
X-Conversation-Request-ID: <uuid>
X-Conversation-Message-ID: <uuid>
X-Request-ID: <uuid>
```

注意事项：

- `User-Agent` 缺失会被上游以错误码 `10085` 拒绝；版本号需与官方客户端对齐。
- `hy3` 是推理模型：`max_tokens` 过小（如 64）时可能全部用于 reasoning，`content` 为空；建议 ≥ 256 或不传。
- 工具调用需要做 JSON Schema 归一化（`max_completion_tokens`→`max_tokens`、`developer`→`system`、union 类型合并、非法工具名替换等）。**lite 暂未实现工具归一化**，带复杂 tools 的请求可能 400；纯对话已可用。
- 使用 `X-Conversation-*` 便于上游按会话计量；lite 以会话键派生稳定 conversation id。
- access token 有效期较长（本机到 2026-11-15），但**未实现刷新**；过期后需重新登录 WorkBuddy/Magpie。

### 3.3 OpenCode Zen Free

| 项 | 值 |
|----|-----|
| 对话端点 | `POST https://opencode.ai/inference/v1/chat/completions` |
| 鉴权 | `Authorization: Bearer public`（共享凭据，无需个人 Key） |
| 模型目录 | `GET /inference/v1/models`（需 `x-opencode-client: cli`） |
| 免费门禁 | 只允许"来自 OpenCode 客户端"的请求（`FreeTierError`） |
| 工具 | 免费层要求请求携带 5 个核心工具（`bash/edit/glob/grep/read`），否则 403 |
| 流式 | 实践上非流式会被拒；lite 统一强制 `stream: true` 后聚合 |

必需头：

```http
content-type: application/json
accept: */*
user-agent: opencode/1.19.0 ai-sdk/provider-utils/4.0.23 runtime/bun/1.3.14
x-opencode-project: global
x-opencode-session: ses_<12hex><14base62>
x-opencode-request: msg_<12hex><14base62>
x-opencode-client: cli
x-opencode-session-id: ses_...
authorization: Bearer public
```

注意事项：

- 会话 ID 必须是 OpenCode 形态（低 12 位为小计数器的 `ses_` ID）；随手拼的 UUID 会被判为非 OpenCode 请求。
- 工具参数不能为空（`bash.command`、`read.filePath` 等），否则 403。
- 该门禁是**显式的客户端限制**，复现请求头属于条款灰区（见 §10）。

### 3.4 Qoder CN（已接入）

| 项 | 值 |
|----|-----|
| 对话端点 | `https://gateway.qoder.com.cn/algo/api/v2/service/pro/sse/agent_chat_generation?FetchKeys=llm_model_result&AgentId=agent_common&Encode=1` |
| 模型目录 | `GET /algo/api/v2/model/list?Encode=1`（COSY 签名） |
| 鉴权 | device token（`local:qoder-cn` → `~/.config/magpie/plugin-auth.json`），签名还需 `machineId` |
| 签名 | AES-128-CBC（**密钥兼 IV**，手动 PKCS7 补位）+ RSA-1024 PKCS1v15 包裹 AES 密钥 + MD5 覆盖 `payload\nkey\nts\n编码后wire\n去 /algo 前缀 path`；`COSY-Version: 1.1.69`、`Cosy-ClientType: 10` |
| body | 明文 JSON 先经 Qoder 私有 base64（打乱字母表 + `$` 补位 + 三段互换）编码成 wire 串再发送，签名也对该 wire 串计算 |
| 模型参数 | 上游要求带模型清单里的原始 `model_config`；适配器 `prepare()` 先拉清单预热（缺字段会静默挂起至 read timeout） |
| 流式归一化 | 上游 SSE 为 `data:{"statusCodeValue":200,"body":"<OpenAI chunk 字符串>"}`（`data:` 后**无空格** + 信封）；网关 `normalize_stream_line()` 剥壳成标准 `data: {chunk}`，错误信封转 `data: {"error":...}` 后**立即终止流**（否则上游不关连接会无限 keepalive） |
| 排队语义 | 排队码 `10605` 藏在**三层嵌套** message（`statusCodeValue=403 → code=403 → code=10605 → isQueued`）；`_envelope_error()` 递归解包后译成 **503 可重试**、`cooldown_scope=none`，不冷却账号/通道，也不让前端把它标成"永久不可用" |
| 免费模型 | `qfmodel`（Qwen3.8-Flash）、`dfmodel`（DeepSeek-Flash），`price_factor=0` |

实现文件：`backend/harness/modules/gateway/adapters/qoder_cosy.py`（COSY 信封/私有 base64/agent body 纯函数）、`qoder_cosy_adapter.py`（`build_request` / `prepare` / `catalog_request` / `probe` / `aggregate_stream` / `normalize_stream_line` / `map_error`）。回归用例见 `backend/tests/test_gateway_core.py`。

---

## 4. 统一模型命名与路由

对外模型名：`<channel>/<model>`，例如：

```
cline/cline-free/minimax-m3.1-flash
workbuddy/hy3
zen/big-pickle
```

跨通道故障转移别名：

```json
"free/auto": [
  "cline/cline-free/minimax-m3.1-flash",
  "workbuddy/hy3",
  "zen/big-pickle"
]
```

路由规则（lite 实现）：

1. 解析别名或 `<channel>/<model>`，得到有序候选；
2. 跳过冷却中的通道（429/5xx 触发 30s 冷却）；
3. 依次尝试，最多 3 个候选；
4. 首个成功响应的通道返回；流式在"首字节前"失败仍可换通道，首字节后不重试；
5. 全部失败返回 503，附最后一次错误摘要（已脱敏）。

---

## 5. 凭据生命周期与安全

| 提供方 | 存储位置 | 有效期 | 刷新策略 | 风险 |
|--------|----------|--------|----------|------|
| Cline | `~/.cline/data/settings/providers.json` | ~1h | lite 在到期前 5 分钟刷新，写回前备份，`os.replace` 原子替换 | 多进程同时刷新会花掉同一枚 refresh token；lite 用进程内锁 + 刷新前重读 |
| WorkBuddy | `~/.config/magpie/plugin-auth.json` | 至 2026-11-15 | 未实现刷新；过期需重新登录 | 依赖 Magpie 的登录态文件 |
| Zen | 代码内 `public` | 长期 | 无需刷新 | 无个人凭据 |
| Qoder CN | `~/.config/magpie/plugin-auth.json` | device token 长期 | ✅ 已接入 | qoder-cosy |

安全约束（lite 已执行）：

- 令牌只在请求内存中出现，不写日志、不进响应、不进用量记录；
- 用量日志 `logs/gateway-lite-usage.jsonl` 只记通道、模型、状态、耗时、token 数；
- 默认只监听 `127.0.0.1`；可通过 `GATEWAY_LITE_KEY` 启用 Bearer 鉴权；
- Cline 刷新写回前自动生成 `providers.json.bak-<ts>` 备份。

---

## 6. 已交付组件与使用

### 6.1 文件

| 文件 | 作用 |
|------|------|
| `tools/gateway.providers.json` | 通道目录（4 个启用，含 Qoder CN），含免费模型清单、端点、鉴权类型、流式/工具要求 |
| `tools/andy_gateway_lite.py` | OpenAI 兼容网关（`/v1/models`、`/v1/chat/completions`、`/healthz`） |
| `start-gateway-lite.bat` | 一键启动（默认 8010） |
| `.tools/probe_providers.py` | 上游直连探针 |
| `.tools/test_gateway_lite.py` | 网关自检（ASGI，不占端口） |
| `.tools/cline_refresh.py` | Cline token 刷新/写回（独立使用） |

### 6.2 启动

```bat
start-gateway-lite.bat
:: 或
backend\.venv\Scripts\python.exe tools\andy_gateway_lite.py --port 8010
```

可选鉴权：

```bat
set GATEWAY_LITE_KEY=agw-local-dev
```

### 6.3 接入 andy-harness

在 Settings → Providers 新建自定义 Provider：

```
base_url = http://127.0.0.1:8010/v1
api_key  = agw-local-dev   （设置了 GATEWAY_LITE_KEY 时必填；未设置时填任意非空值）
models   = cline/cline-free/minimax-m3.1-flash, workbuddy/hy3, zen/big-pickle, free/auto
```

Cline / OpenCode 等外部客户端同理，把 `base_url` 指向该地址即可。

### 6.4 直接调用

```bash
curl http://127.0.0.1:8010/v1/models

curl http://127.0.0.1:8010/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"workbuddy/hy3","messages":[{"role":"user","content":"你好"}],"stream":false}'
```

---

## 7. 实测记录

| 时间 | 通道 | 模型 | 端点 | 状态 | 结果 | 时延 |
|------|------|------|------|------|------|------|
| 08:20 | Cline | `cline-free/minimax-m3.1-flash` | `/api/v1/chat/completions` | 200 | `OK`，`cost=0` | ~2.1s |
| 08:20 | Cline | `reflection/beam-free` | 同上 | 200 | `OK`，`cost=0` | 同通道同量级 |
| 08:19 | WorkBuddy | `hy3` | `/v2/chat/completions` | 200 | `OK.`，`credit=0` | ~2.9s |
| 08:22 | WorkBuddy | `step-5-preview` | 同上 | 200 | 流式正常 | ~2.7s |
| 08:22 | Zen | `big-pickle` | `/inference/v1/chat/completions` | 200 | `OK`，usage 正常 | ~5.3s |
| 08:25 | lite（ASGI 自检） | `free/auto` | `ASGITransport` | 200 | 命中 Cline 首候选 | ~2s |
| 08:25 | lite（ASGI 自检） | `workbuddy/step-5-preview`（stream） | 同上 | 200 | SSE 正常 | ~2.6s |
| 08:40 | lite（uvicorn 实机） | `workbuddy/hy3` | `127.0.0.1:8011` | 200 | `OK.` | ~3s |

模型目录探测：

| 通道 | 端点 | 状态 | 耗时 |
|------|------|------|------|
| Cline | `recommended-models` | 200（6 个免费） | 680ms |
| WorkBuddy | `/v3/config` | 200（37 个模型；`hy3` 在列，`step-5-preview` 不在列但可调用） | 155ms |
| Zen | `/models` | 200（86 个模型，10 个免费） | 1058ms |

---

## 8. 与主设计文档的映射

| 主设计文档章节 | 本轮落地情况 |
|----------------|--------------|
| §4.2 适配器抽象 | Cline/WorkBuddy/Zen 均可用 `openai-like` + 通道 profile 覆盖，无需新增适配器类；Qoder 需 `qoder-cosy` |
| §4.3 路由与故障转移 | lite 已实现候选过滤、冷却、3 候选上限、`free/auto` 别名 |
| §4.5 凭据与多租户 | 本轮仅本机凭据读取；多租户/Key 管理尚未实现（P2） |
| §4.6 可观测 | lite 已落用量 JSONL；span/指标/审计待 P1/P3 |
| §4.7 动态扩展 | lite 用 JSON 目录（热更新需重启）；版本化配置待 P2 |
| §8 P0 | lite 等价于 P0 的"单通道直连"扩展到三通道，并提前验证了 P1 的故障转移 |
| §8 P4 | Cline/WorkBuddy/Zen 的适配器工作被证明是"配置级"而非"代码级"；仅 Qoder 需要新适配器 |

---

## 9. 未完成项与下一步

> 注：本表为 v1.1 当时的计划。截至 2026-10-22，**Qoder CN `qoder-cosy` 适配器已完成**；lite 的通道/路由/记账能力已并入 `backend/harness/modules/gateway/`，Key/租户/审计/指标与前端管理台均已落地。下表保留仍待推进的项。

| 项 | 优先级 | 说明 | 预估 |
|----|--------|------|------|
| WorkBuddy 工具 schema 归一化 | P1 | `max_completion_tokens→max_tokens`、`developer→system`、union/enum/tuple 归一、工具名替换 | S–M（1–2 天） |
| 把 lite 并入 `modules/gateway/` | P1 | 复用 Database/Key 管理/审计/span；lite 作为回归基线 | M（3–5 天） |
| Cline 版本钉子维护 | 持续 | 上游发版后更新 `CLINE_VERSION/CLINE_CORE`；建议做启动自检（版本过旧时告警） | 持续 |
| WorkBuddy token 刷新 | P2 | 当前未实现；可在到期前提示或复用 Magpie 刷新 | S |
| 多租户 Key / 审计 / 指标 | P2/P3 | 按主设计文档执行 | 见主文档 |
| 工具调用端到端测试 | P1 | Cline/Zen/WorkBuddy 的 tools 往返、流式 tool_calls | S |

---

## 10. 合规与风险

1. **客户端身份门禁**：Cline 免费模型校验 `X-CLIENT-*` 版本；Zen 明确"只能从 OpenCode 内使用"。本方案复现官方客户端请求头以通过门禁，**属于服务条款灰区**，可能导致账号被限制；是否启用由使用者自行决定，建议仅个人自用、控制频率。
2. **凭据是个人资产**：lite 只监听 127.0.0.1，不应暴露到公网；如需局域网访问，必须设置 `GATEWAY_LITE_KEY` 并加反向代理鉴权。
3. **上游可随时变更**：免费模型、字段、门禁规则都可能变化；本方案的通道目录与版本常量需要维护。
4. **额度非无限**：WorkBuddy 为 Free 套餐（每日额度），Cline/Zen 免费模型也有速率限制；lite 的 429 冷却只是止损，不增加额度。

---

## 附录 A：复现步骤

```bat
:: 1. 直连探测（模型列表 + 最小对话）
backend\.venv\Scripts\python.exe .tools\probe_providers.py --lists
backend\.venv\Scripts\python.exe .tools\probe_providers.py --chat cline
backend\.venv\Scripts\python.exe .tools\probe_providers.py --chat workbuddy
backend\.venv\Scripts\python.exe .tools\probe_providers.py --chat zen

:: 2. lite 自检（不占端口）
backend\.venv\Scripts\python.exe .tools\test_gateway_lite.py

:: 3. 启动
start-gateway-lite.bat
```

## 附录 B：通道目录字段

| 字段 | 说明 |
|------|------|
| `id` | 通道标识，同时作为模型前缀 |
| `adapter` | 适配器类型：`openai-like`（OpenAI 兼容通道）/ `qoder-cosy`（Qoder COSY 私有协议，已接入） |
| `base_url` / `chat_path` | 上游地址 |
| `auth` | `cline-cli` / `workbuddy-oauth` / `public` / `qoder-device` |
| `stream_only` | 上游只接受流式时置 true，网关负责聚合 |
| `unwrap` | 非流式响应需要解包的外层键（Cline 为 `data`） |
| `requires_tools` | 上游强制要求 tools 时置 true（Zen） |
| `models` | 该通道的免费模型 ID 列表 |
| `weight` | 预留：多凭据加权（lite 暂未使用） |
