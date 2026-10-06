# 工作流编排器（Workflow Studio）功能验证报告

- **验证日期**：2026-10-06
- **验证对象**：`backend/harness/modules/workflows/`（引擎/服务/REST）+ `frontend/src/views/WorkflowView.vue`（画布编辑器）+ `frontend/src/views/WorkflowListView.vue`（列表首页）+ `frontend/src/components/VarRefField.vue`（变量选择器）
- **验证方法**：① 后端 pytest；② 引擎直跑（真实图 + 本地 HTTP 服务 + mock provider）；③ 经 FastAPI `TestClient` 的 REST E2E；④ 前端 `vue-tsc` 类型检查 + `vite build`；⑤ 静态核对节点配置面板与 i18n 文案暴露度；⑥ 与 Dify 官方节点文档逐项比对。
- **验证局限**：沙箱无浏览器，**未做前端交互的浏览器实测**（拖拽/连线/缩放的视觉与手感未经人工点测），但类型检查、构建、API 契约与引擎逻辑均已实测通过。

---

## 一、结论摘要（TL;DR）

1. **核心编排能力已具备且经真实执行验证**：8 类节点（开始/结束/大模型/代码/条件分支/HTTP/模板/变量赋值）均可真实运行，分支感知拓扑调度正确，变量池 `{{节点.字段}}` 全链路贯通。
2. **与 Dify 的核心交互模型高度对齐**：无限画布 + 节点面板 + 端口连线 + 变量选择器 + 节点运行状态/输出展示，交互范式一致。
3. **未发现的阻塞级（Blocker）缺陷**：无导致流程完全不可用或数据丢失的致命问题（删除有前端二次确认；运行失败仅终止当前流程、不破坏库）。
4. **主要差距**（按影响排序）：
   - **运行采用同步阻塞**，长 LLM 任务有被网关/客户端超时截断的风险（最大架构隐患）；
   - **LLM / 代码节点缺少 Dify 的高级能力**（知识检索、记忆、JSON 结构化输出、JS 运行时等）；
   - **编辑器交互体验**缺 undo/redo、小地图、流式轨迹等；
   - 列表缺"最近编辑时间"、删除无软删回收。

> 详细修改优先级见第七章「轻重缓急清单（P0/P1/P2）」。

---

## 二、已验证通过项（逐节点 / 逐能力）

### 2.1 节点执行（引擎直跑，真实数据）

| 节点 | 验证场景 | 结果 | 证据 |
|---|---|---|---|
| 开始 start | 变量类型转换 `number/integer/boolean` | ✅ | `num=5`→整数 5；布尔兼容 `1/true/yes/on` |
| 代码 code | Python 子进程沙箱 + `{{ }}` 渲染 + 自定义 timeout/retry | ✅ | `len('秋游')=2`、`doubled=10`；超时/重试参数生效 |
| 条件分支 condition | 多条件 + `logic=AND/OR` + 全运算符 + true/false 分支 | ✅ | `10>5` 命中 true 分支；false 分支节点标记 `skipped` |
| HTTP | **真实往返**本地服务 + 查询参数 + 鉴权 + body_type + 超时/SSL | ✅ | `status_code=200`、`headers` 含 `X-Test`、`body`/`size` 正确 |
| 模板 template | Jinja2 渲染（条件/循环/过滤器） | ✅ | `主题=秋游，长度=2，计数=10.0` |
| 变量赋值 assign | set/clear/append/extend/加/减/乘/除 | ✅ | `counter=10`（add）、`greeting=2`（引用上游） |
| 大模型 llm | 参数透传 + 推理/正文分离 + merged + `max_retries` 重试 | ✅ | `temperature/top_p/.../max_tokens` 透传；`reasoning` 独立；fail_once 时 `calls=2`；merged 模式合并 |
| 结束 end | 多输出变量引用 | ✅ | `end_outputs` 正确收齐 |

### 2.2 后端接口 / 契约

| 项目 | 结果 | 说明 |
|---|---|---|
| 路由 `GET/POST /`、`GET/PUT/DELETE /{id}`、`POST /{id}/run`、`GET /runs`、`GET /runs/{id}` | ✅ | 全部可访问 |
| REST E2E：创建→运行→历史→删除 | ✅ | 经 `TestClient` 全流程跑通，`end_outputs` 含 `Hello, 世界` |
| 运行结果契约 | ✅ | 后端返回 `engine_result.end_outputs`，前端 `confirmRun` 读 `res.engine_result.end_outputs`，**前后端一致** |
| 历史端点形态 | ⚠️ 注意 | 正确路径为 `GET /api/workflows/runs?workflow_id=<id>`；`GET /{id}/runs` 为 **404**（前端未使用错误路径，契约正确） |
| pytest `test_workflows.py` | ✅ | 5/5 通过（含 CRUD、链式运行、失败、校验、404） |

### 2.3 前端

| 项目 | 结果 | 说明 |
|---|---|---|
| `vue-tsc -b --force` 类型检查 | ✅ | 0 错误 |
| `vite build` 生产构建 | ✅ | 构建成功（仅有 chunk 体积告警，非错误） |
| 高级配置面板暴露度 | ✅ | 静态核对：`temperature/top_p/frequency_penalty/presence_penalty/max_tokens/reasoning_format/max_retries/retry_interval/conditions/logic/auth/body_type/ssl_verify` 等**全部在编辑器模板中渲染** |
| i18n 文案齐全度 | ✅ | 上述字段标签、运算符（11 种）、认证类型/种类、请求体类型、SSL 开关等中文键**均存在**，无"裸 key"风险 |
| 变量选择器（Dify 风格 `{}` 弹层） | ✅ | 按上游节点分组、展开字段、光标处插入 `{{节点.字段}}` |
| 运行输入闭环 | ✅ | `onRun` 收集开始节点变量为入参；`confirmRun` 做 required 校验 + `ensureSaved()` 自动保存后运行 |

---

## 三、校验与健壮性验证

| 场景 | 期望 | 结果 |
|---|---|---|
| 工作流为空 | 拒绝 | ✅ |
| 无「结束」节点 | 拒绝 | ✅ `WorkflowValidationError` |
| 多个「开始」节点 | 拒绝 | ✅ |
| 条件分支未同时连 true+false | 拒绝 | ✅ |
| 节点执行抛错 | 整流程标记 error 并终止 | ✅（见差距 P1-5：无"失败继续"） |

---

## 四、与 Dify 节点能力对齐差距分析

> 以下以 Dify 官方文档（`https://docs.dify.ai/zh/cloud/use-dify/nodes/`）为基准。

| 节点 | 本项目已实现 | 与 Dify 的差距 |
|---|---|---|
| 开始 Start | 变量定义 + 类型转换(string/number/integer/boolean) | 缺 `select`、`file`、`array[object/string/number/file]`、`object` 类型 |
| 结束 End（Output） | 多输出变量引用 | Dify 输出节点支持变量类型声明；多输出节点共存时合并策略未显式说明 |
| 大模型 LLM | 模型/提示词/5 项采样参数/推理分离/重试 | 缺 **上下文/知识检索**、**记忆(memory)**、**视觉(vision)**、**JSON 结构化输出(response_format)**、**工具调用**、高级参数(top_k/seed/stop/logit_bias) |
| 代码 Code | Python 沙箱 + `{{}}` 渲染 + 超时/重试 | 仅 Python；**缺 Node.js(JavaScript)**；Dify 要求 `main` 函数 + 声明输入/输出，本项为自由式 `output={}`（易误用） |
| 条件分支 If-Else | 多条件 + AND/OR + 11 运算符 + 双出口 | 运算符缺 `is null`/`is not null`、数组包含；Dify 有更细的类型比较 |
| HTTP 请求 | 方法/URL/查询/请求头/鉴权(api-key bearer/basic/custom)/body_type(json/form/raw/binary)/超时/SSL/重试 | body 缺 **multipart/form-data 文件上传**（当前 form=urlencoded，binary 按文本发送） |
| 模板转换 Template | Jinja2 全功能 | 嵌套对象 `{{ node.obj.field }}` 依赖扁平冗余键，深层嵌套可能不工作（待验证） |
| 变量赋值 Variable Assigner | set/clear/append/extend/加减乘除 | 已覆盖 Dify 的 set/append/clear；算术为额外增强；缺 ENV/会话级变量持久语义 |
| （未实现） | — | 缺 **迭代(Iteration)**、**知识检索**、**问题分类**、**工具**、**参数提取器**、**变量聚合器** 等 Dify 节点 |

---

## 五、需要修改的轻重缓急清单（P0 / P1 / P2）

评级说明：**P0**=影响可用性/可靠性/数据的必须项；**P1**=明显能力缺口或体验硬伤，下个迭代应修；**P2**=体验增强/对齐补全，可排期。

### P0 — 紧急（架构/可靠性）

- **[P0-1] 运行改为异步 + 进度回传**
  - 现状：`POST /workflows/{id}/run` 在请求内**同步**执行整图，前端 `await` 直到完成。
  - 风险：LLM/多节点长流程（>30s）易被反向代理或客户端超时截断，前端拿到超时错误但后端可能仍在跑/已跑完，结果丢失。
  - 建议：改为「创建运行任务 → 后台执行 → 轮询 `GET /runs/{id}` 或 WebSocket/SSE 流式回传节点状态」。这是当前最大架构隐患，应优先处理。

### P1 — 高优先级（能力缺口 / 体验硬伤）

- **[P1-1] LLM 节点补齐 Dify 高级能力**：知识检索(context)、记忆(memory)、JSON 结构化输出(`response_format`)、视觉(vision)、工具调用、高级参数(top_k/seed/stop)。此为"对齐 Dify"的核心差距。
- **[P1-2] 代码节点支持 JavaScript(Node.js)**：当前仅 Python；Dify 双语言。
- **[P1-3] 节点级错误隔离（continue-on-error）**：当前任一节点报错即整流程终止；Dify 可配置"失败继续"。需引擎支持节点 `continue_on_error` 标志。
- **[P1-4] 流式运行轨迹**：长流程无实时反馈。建议逐节点高亮 + token 消耗/耗时回传（配合 P0-1 的异步通道）。
- **[P1-5] 列表增加"最近编辑时间"**：当前 `workflows` 表仅 `created_at`，无 `updated_at`；用户无法判断工作流新旧。需 `ALTER` 加列并在保存时写入（低风险 schema 变更）。
- **[P1-6] 删除增加软删/回收站或导出**：当前 `DELETE` 物理删除且级联清空运行历史，误删不可逆。至少加软删标记 + 恢复，或删除前自动导出 JSON。
- **[P1-7] HTTP 节点支持 multipart/form-data 文件上传**：对齐 Dify binary/form-data。

### P2 — 中低优先级（体验增强 / 对齐补全）

- **[P2-1] 开始节点变量类型扩展**：`select`、`file`、`array[...]`、`object`。
- **[P2-2] 编辑器交互增强**：撤销/重做(undo/redo)、小地图(minimap)、缩放比例显示、自动布局、节点搜索。
- **[P2-3] 新增 Dify 节点**：迭代(Iteration)、知识检索、问题分类、工具、参数提取器、变量聚合器。
- **[P2-4] 条件运算符扩展**：`is null`/`is not null`、数组包含。
- **[P2-5] 模板嵌套对象访问**：验证并修正 `{{ node.obj.field }}` 深层访问（当前依赖扁平冗余键）。
- **[P2-6] 全局/环境变量持久化**：Dify 的 ENV/conversation variables 语义。
- **[P2-7] 运行历史前端可视化入口**：历史接口可用（`GET /runs?workflow_id=`），但未在 UI 呈现；建议列表/编辑器内加"运行历史"面板。
- **[P2-8] 代码节点契约对齐 Dify**：要求 `main` 函数 + 声明输入/输出，降低自由式 `output={}` 误用。
- **[P2-9] 节点运行日志详情面板**：展示 token 消耗、实际请求/响应体。
- **[P2-10] 赋值运算结果类型保持**：`add` 当前返回 `float`（10.0），整数场景可配/保持 `int`。

---

## 六、验证方法与可执行证据（复现）

1. **后端单测**：`cd backend && CODEBUDDY_SAFE_DELETE_ENABLED=0 .venv/Scripts/python.exe -m pytest tests/test_workflows.py -q --basetemp="<本地临时路径>"` → 5 passed。
2. **引擎直跑（多条链路）**：构造 `start→code→assign→condition→template→end` 与 `start→http→template→end`（本地 HTTP 服务），断言输出；均通过。
3. **LLM 路径**：注入 mock provider，验证参数透传、推理分离、merged、`max_retries`（fail_once 时 `calls=2`）均通过。
4. **REST E2E**：`TestClient` 跑 创建→`POST /run`→`GET /runs?workflow_id=`→删除，全通过；确认 `engine_result.end_outputs` 契约。
5. **前端**：`cd frontend && CI=true vue-tsc -b --force` 与 `vite build` 均通过；并静态核对配置面板与 i18n 暴露度。

---

## 七、总体评价

工作流编排器已具备**可真实编排并成功执行完整流程**的 MVP 能力，8 类节点、分支、变量模型、运行/保存/历史闭环完整，且与 Dify 的交互范式基本一致。**当前最该优先处理的是 P0-1（同步运行改异步）**，它直接决定长流程在生产环境的可靠性；其次为 P1 系列的能力与体验补齐以真正"对齐 Dify"。无阻塞性缺陷，可进入下一轮增强迭代。
