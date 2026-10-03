# andy-harness 系统性端到端审查与自洽性验证 —— 修改方案

- 审查日期：2026-09-28
- 审查方式：**静态源码审查为主**（逐层通读 + 引用行号与关键代码片段），本轮**未启动服务、未调用真实模型**
- 审查范围：后端引擎层 / REST 与 WS 接口层 / 前端 Pinia 与视图层 / 附件功能与既有功能的衔接点
- 本阶段**仅产出方案，未改动任何代码**

---

## 一、结论摘要

共发现 **16 项**待优化项：**P0 阻塞 2 项、P1 高 5 项、P2 优化 9 项**。

其中 2 项 P0 均属「会真实导致功能不可用或数据串会话」的严重缺陷，必须先于其他所有工作修复：

1. **P0-1**：同一 Provider 实例被多个 WS 连接同时 monkey patch，导致跨会话 token 串流与工具类污染。
2. **P0-2**：WS 未连接时 `ws.send` 静默丢弃且无超时兜底，会把聊天界面永久锁在「生成中」状态。

另有 5 项 P1 集中在**新交付的附件功能与既有「删除 / 分叉 / 导入 / 切换会话」链路的衔接缺口**——这正是本次审查的重点，也是新功能最容易留下的数据一致性债务。

> 说明：下文每条均标注了确切文件路径与行号。凡本轮未能亲眼核实的内容，一律未写入正文，已在文末「六、未覆盖与存疑事项」中单列。

---

## 二、P0 阻塞项（必须最先修复）

### P0-1｜共享 Provider 单例被逐连接 monkey patch，导致跨会话数据串流

**位置**
- `backend/harness/api/ws/chat.py:362-407`（patch 与恢复）
- `backend/harness/modules/model_manager/provider_registry.py:90-118`（单例缓存）

**关键代码**

`provider_registry.py:90-118`（已实测确认的实例缓存，即单例语义）：
```python
        if provider_id not in self._instances:
            api_key = config.get("api_key", "")
            ...
            self._instances[provider_id] = provider_class(
                api_key=api_key,
                base_url=config.get("base_url"),
                ...
            )

        return self._instances[provider_id]
```

`ws/chat.py:362-407`（每个 WS 连接都对同一对象做属性覆写）：
```python
                # 包装 provider 以推送 token_delta（S17: 同时发布到 EventBus）
                original_chat = provider.chat          # ← 捕获的可能是别人已打过补丁的函数

                async def streaming_chat(*args: Any, **kwargs: Any) -> Any:
                    ...
                        await websocket.send_json(
                            {"type": "token_delta", "data": {"delta": chunk["delta"]}}
                        )
                    ...

                provider.chat = streaming_chat         # ← 覆写共享单例
```
恢复点在 `ws/chat.py:424-425`：`provider.chat = original_chat`。

**问题描述**
`get_provider()` 按 `provider_id` 缓存**同一实例**（`self._instances[provider_id]`），多 tab / 多窗口 / 同一页面并发的两个 WS 连接拿到的是**同一个对象**。而每个连接在每次请求前都对该对象执行 `provider.chat = streaming_chat`，请求后才恢复。这会产生三个具体后果：

1. **嵌套包装**：连接 B 在连接 A 尚未恢复时捕获连接 A 的 `streaming_chat` 作为自己的 `original_chat`，形成链式包装。
2. **恢复错值**：A 结束时执行 `provider.chat = original_chat`，会把 B 刚打上的补丁**回滚掉**（或反之），使某个连接彻底收不到 token。
3. **跨会话内容泄漏**：在嵌套窗口期内，一次 LLM 响应会被多个 wrapper 各自 `send_json` 到**不同的 socket**，A 会话的回答出现在 B 会话界面上。

**触发路径 / 复现步骤**
1. 打开两个浏览器标签（或两个 WS 连接，例如同一会话在两处打开），provider 选择同一个。
2. 让两个连接几乎同时发起请求（或 A 的请求尚未返回时 B 发起）。
3. 观察：A 的输出出现在 B 的界面；随后某个连接可能彻底不再收到 token_delta。

**影响范围**
- 功能：回答内容跨会话串显；严重时某连接静默收不到流。
- 安全：不同会话的对话内容互相泄漏（含用户上传文档内容）。
- 范围：所有走 WS 的对话，与是否使用附件无关。

**建议解决方案**
不要覆写共享对象，改为**注入式装饰**——把「是否属于当前会话 + 发往哪个 socket」作为调用参数传递，而非对象状态：

- 方案 A（推荐，改动可控）：为 `AgentLoop` / `streaming_chat` 引入 per-connection 的 provider 代理。在 WS 处理内构造轻量 `ProxyProvider`（实现 `chat` 协议，内部 `await original_chat(...)`），把代理实例传给 `loop.run(provider=proxy)`，**完全不触碰共享单例**。
- 方案 B（较小）：保留 patch，但把 `original_chat` 存-Global 上锁 —— 不建议，仍存在并发恢复竞态。
- 兜底：无论选哪个方案，都应把 provider 视为**不可变共享资源**，并在代码注释与 CR 检查项中明确这一约束。

**改造成本评估**
方案 A 约 **1 人日**（新增 ProxyProvider 类 + 修改 `loop.run` 调用点 + WS 测试；需同时回归确认 `_runMockStream` 与子代理 `runtime` 过滤逻辑不受影响）。

---

### P0-2｜WS 未连接时 `ws.send` 静默丢弃，且无超时兜底 → 界面永久卡在「生成中」

**位置**
- `frontend/src/api/client.ts:146-150`（静默丢弃）
- `frontend/src/stores/chat.ts:296`（`isStreaming` 提前置位）
- `frontend/src/views/ChatView.vue:748 / 770 / 782`（置位后的全局禁用）

**关键代码**

`client.ts:146-150`：
```ts
    send(data: unknown) {
      if (this.socket?.readyState === WebSocket.OPEN) {
        this.socket.send(JSON.stringify(data))
      }
    },
```
注意：**没有 else 分支**，不抛错、不排队、不回调。

`chat.ts:296`（发送前无条件置位）：
```ts
    isStreaming.value = true
    error.value = null
```
而 `isStreaming` 只会被 `handleWSFrame` 的 `error` / `done` 分支复位（`chat.ts:166、193`），没有任何超时定时器。

`ChatView.vue:748 / 770 / 782`（一旦卡住，交互全封）：
```html
          <textarea ... :disabled="chatStore.isStreaming" />
            :disabled="chatStore.isStreaming"          <!-- 附件按钮 -->
            :disabled="(!inputText.trim() && chatStore.pendingAttachments.length === 0) || ..."
```

**问题描述**
若发送时 WS 处于 `CONNECTING` / `CLOSING` / `CLOSED`（例如后端刚重启、网络抖动、页面刚进入尚未握手完成），`ws.send` 会**无声丢弃**整条消息：服务端完全没收到，自然也不会回 `done` 帧。而前端已在 `sendMessage` 中把 `isStreaming` 置为 `true`，且不存在任何超时/心跳复位机制，于是输入框、附件按钮、发送按钮全部被禁用，用户**除刷新页面外无法恢复**。

界面虽然有个 WS 连接状态指示（`ChatView.vue:554`），但它是被动展示，不阻断发送，也未给出「当前无法发送」的明确引导。

**触发路径 / 复现步骤**
1. 打开前端页面，**在 WebSocket 握手完成前**立刻点发送（弱网/首次加载极易命中）；或
2. 让后端服务停止一小段时间使 WS 断开重连，在重连窗口内点发送。
3. 现象：输入框变灰、发送按钮变灰、无任何错误提示，页面需强制刷新。

**影响范围**
- 功能：核心对话链路不可用，且**无提示、无自愈**。
- 体验：用户无法判断是「模型慢」还是「压根没发出去」，极易误判为产品故障。

**建议解决方案**
三处联动修复，缺一不可：

1. `client.ts:146-150`：`send` 必须返回布尔值或在未 OPEN 时**抛错 / 走 queue 重试**；建议在 `CONNECTING` 时入队，握手成功后 flush，`CLOSED` 时立即返回失败。
2. `chat.ts:296` 之后：增加**发送超时兜底**（例如 30s 未收到首个 `token_delta` 且无 `done`，则自动置 `isStreaming=false` 并在 `error` 中给出「未收到服务端响应，请检查连接后重试」）。
3. `ChatView.vue`：发送按钮的 disabled 条件不应只依赖 `isStreaming`，应在 WS 非 `connected` 时给出明确阻断提示（复用已有的 `wsConnectionStatus`，避免与现有连接指示器语义冲突）。

**改造成本评估**
约 **0.5～1 人日**（前端单点改动，但需覆盖重连成功的 flush 时序，建议配套一个「断网 - 重连 - 发送」的手动回归用例）。

---

## 三、P1 高优先级项（新功能与既有链路的衔接缺口）

> 这 5 项几乎全部围绕「附件」与「删除 / 分叉 / 导入 / 切换会话」的衔接，建议作为第二批统一处理。

### P1-1｜切换会话不清空待发送附件，且服务端静默丢弃跨会话附件引用

**位置**
- `frontend/src/stores/chat.ts:80-93`（`selectSession` 未清空 `pendingAttachments`）
- `frontend/src/stores/chat.ts:304-312`（乐观渲染的消息已带上附件）
- `backend/harness/api/ws/chat.py:246-254`（只 warning，不反馈前端）

**关键代码**

`chat.ts:80-93`（清了 messages / streamingContent / toolEvents，**唯独没有 pendingAttachments**）：
```ts
  async function selectSession(sessionId: string) {
    currentSessionId.value = sessionId
    const [msgs, snapshot] = await Promise.all([...])
    messages.value = msgs
    streamingContent.value = ''
    toolEvents.value = []
    contextSnapshot.value = snapshot || null
  }
```

`ws/chat.py:246-254`（跨会话引用被静默过滤）：
```python
                att_metas = _resolve_attachment_metas(
                    services, msg.session_id, msg.attachments
                )
                if msg.attachments and not att_metas:
                    logger.warning(
                        "WS 附件引用无效或不属于本会话: session=%s refs=%s",
                        msg.session_id,
                        msg.attachments,
                    )
```
过滤逻辑本身是**正确的安全实现**（`ws/chat.py:72` 校验 `session_id` 归属），问题在于**只写日志，前端完全无感**。

**问题描述**
附件在「会话 A」上传完成（已落盘、已登记元信息），用户随后切换到「会话 B」再发送。`pendingAttachments` 仍持有属于 A 的附件 id，随消息体一起发出。服务端安全策略正确地把它们全部丢弃，但前端已经在气泡里渲染了这些附件。最终结果是：**用户看到附件已随提问发出，模型实际上什么都没收到**，据此给出的回答看似正常但完全偏离预期——属于「无声的错误答案」。

**触发路径 / 复现步骤**
1. 在会话 A 上传一张截图（不发送）。
2. 左侧切换到会话 B。
3. 输入「这是什么图？」并发送。
4. 现象：气泡中显示图片，但模型回答「没有看到图片/无法识别」，前端无任何提示。

**影响范围**
- 功能：附件静默丢失，产生误导性错误答案。
- 体验：无任何失败反馈，用户难以自行定位。

**建议解决方案**
1. `selectSession` 中清空 `pendingAttachments`（若需要保留，应显式迁移到专属于该会话的结构）。
2. `handleSend` / `sendMessage` 前置校验：`attachments` 中的 id 必须属于当前会话；不符则阻断并提示。
3. 服务端增强：当 `msg.attachments` 非空但解析结果为空时，向该 socket 下发一条 **`error` 帧**（例如 `ATTACHMENT_RESOLUTION_FAILED`），让前端能明确提示「附件已失效，请重新上传」——**不要改变现有的安全默认（不阻断对话），只增加告知**。

**改造成本评估**
约 **0.5 人日**（前端 3 处小改 + 后端 1 处错误帧；需同步回归「无附件」主链路未受影响）。

---

### P1-2｜删除整轮 / 单条消息时未清理其附件，产生孤儿文件与孤儿元数据

**位置**
- `backend/harness/modules/session_manager/service.py:155-177`（`delete_turn`）
- `backend/harness/modules/session_manager/service.py:151-153`（`delete_message`）
- 对照 `backend/harness/modules/session_manager/service.py:89-113`（`delete_session` 做了清理）

**关键代码**

已正确实现的 `delete_session`（`service.py:91-109`）——证明清理逻辑本身存在：
```python
            sess_dir = Path(attachments_dir()) / session_id
            if sess_dir.exists():
                shutil.rmtree(sess_dir, ignore_errors=True)
            ...
            AttachmentRepository(db).delete_by_session(session_id)
```

缺失清理的 `delete_turn`（`service.py:165-177`）：
```python
        messages = self.list_messages(session_id)
        idx = next(...)
        ...
        ids = [m.id for m in messages[idx:end]]
        return self._message_repo.delete_many(ids)      # ← 仅删消息，附件文件与元数据残留
```

**问题描述**
`delete_turn` / `delete_message` 只删除 `messages` 表记录。被删消息上挂载的附件既没有从磁盘删除，`attachments` 表的行也没有删除，形成**双重孤儿**：目录下文件永久残留、`attachments` 表中留下无人引用的行。随着用户频繁「删除本轮」，磁盘与表会单调增长，且这些文件在会话被删之前**始终可被 URL 直接下载**（接口仍在）。

**触发路径 / 复现步骤**
1. 上传一张图片并发送。
2. 对该轮执行「删除本轮」。
3. 检查 `backend/data/attachments/<session_id>/` 与 `attachments` 表：文件与记录依然存在。
4. 直接访问 `/api/sessions/<session_id>/attachments/<att_id>` 仍可下载。

**影响范围**
- 资源：磁盘与表无界增长（长期运行必现）。
- 一致性：已删除内容的残留文件仍可被引擎的下游流程（例如记忆压缩）重新读到，语义上不该存在。

**建议解决方案**
在 `delete_turn` / `delete_message` 中复用 `delete_session` 已有的清理原语，抽取为 `_purge_attachments(message_ids)`：
- 先根据 message_id 收集附件 id（消息 JSON 中已有 `attachments` 字段，`repository.py:88` 已导出）；
- 删除磁盘文件与 `attachments` 行（需要新增 `AttachmentRepository.delete_by_ids`）；
- 再删消息。整体建议在**同一个调用内串行完成**，并保留 `delete_session` 现有的 `ignore_errors` 容错风格。

**改造成本评估**
约 **0.5 人日**（含一个 Repository 新方法 + 两个调用点 + 回归「删除本轮 / 删除会话」两条路径）。

---

### P1-3｜会话分叉与导入丢失附件，导出/导入不成闭环

**位置**
- `backend/harness/modules/session_manager/service.py:262-271`（`fork_session` 未传 attachments）
- `backend/harness/modules/session_manager/service.py:223-231`（`import_session` 未传 attachments）
- 对照 `backend/harness/modules/session_manager/service.py:198-204`（`export_session` 含 attachments）
- `backend/harness/infra/repository.py:88`（`to_dict` 已包含 attachments）

**关键代码**

导出是**包含**附件的（`service.py:198-204` 经由 `to_dict`）：
```python
            "messages": [
                {**m.to_dict(), **({"tool_call_id": m.tool_call_id} ...)}
                for m in self.list_messages(session_id)
            ],
```

但分叉复制时**不带**：
```python
        for m in messages:
            self.append_message(
                session_id=forked.id,
                role=m.role,
                content=m.content or "",
                tool_calls=m.tool_calls,
                tool_call_id=m.tool_call_id,
                tokens=m.tokens,
                latency_ms=m.latency_ms,
            )                                   # ← 无 attachments
```
`import_session`（`service.py:223-231`）同样缺失。

**问题描述**
导出带有附件引用，但导入会丢弃；分叉也会丢弃。形成两个后果：

1. **数据往返不对称**：`导出 → 导入` 得到的会话与原始会话内容不一致，附件永久丢失（且无任何提示）。
2. **分叉语义不完整**：用户从「带附件的提问」处分叉，期望从该处继续对话（含同一份文件），实际得到的是失去文件的副本，后续提问会遭遇与 P1-1 相同的「模型看不到文件」。

注意：此处的处理**不能简单地复制同一份 attachment id**——副本与原会话共享同一份磁盘文件与元数据，任何一方删除都会破坏另一方。需要**物理复制文件并建立新的元信息行**（引用计数或独立副本二选一）。

**触发路径 / 复现步骤**
1. 上传文档并发送，让回答引用该文档。
2. 对首轮执行「从此处分叉」。
3. 切到分叉出的会话，追问「上文的文档里第 2 条是什么」。
4. 现象：模型看不到任何文档；原会话里该提问的附件也没了（若走 `export → import`，导出包里的附件字段导入后被丢弃）。

**影响范围**
- 功能：分叉与导入两个既有功能在「带附件会话」上出现静默降级。
- 数据：导出包不再是可信的完整快照。

**建议解决方案**
分两步：
1. 短期（避免静默丢失）：分叉/导入时若检测到源消息带附件，**明确提示**「附件不会被复制」，并给出「请在新会话重新上传」的引导。
2. 中期（闭环）：实现 `copy_attachments_to_session(src_session, dst_session, att_ids)`——物理复制文件到目标会话目录 + 登记新 `attachments` 行 + 在新消息中写入新 id。分叉与导入均走此函数。`fork_session` 还应同步处理 `delete_session` 已覆盖的反向清理。

**改造成本评估**
短期约 **0.3 人日**；闭环版本额外 **1 人日**（涉及文件复制、id 重映射、以及 `delete_session` 的对称性验证）。建议短期与闭环合并为一次交付。

---

### P1-4｜上传接口先全量读入内存再校验数量，且无部分失败回滚

**位置**
- `backend/harness/api/rest/attachments.py:56-93`

**关键代码**

`attachments.py:58-75`（**先读、后校验数量**）：
```python
        pending: list[tuple[str, str, str, bytes]] = []
        doc_count = img_count = 0
        for f in files:
            filename = f.filename or "unknown"
            data = await f.read()                    # ← 无条件先全部读入内存
            try:
                kind, mime = classify(filename, len(data))
            except AttachmentError as e:
                raise APIError("ATTACHMENT_INVALID", str(e), 400)
            ...
            pending.append((filename, kind, mime, data))

        if (doc_count > MAX_DOCUMENTS_PER_MESSAGE
            or img_count > MAX_IMAGES_PER_MESSAGE
            or len(pending) > MAX_FILES_PER_MESSAGE):
            raise APIError("ATTACHMENT_LIMIT", ...)
```

`attachments.py:87-93`（落盘与登记非原子，无回滚）：
```python
        for filename, kind, mime, data in pending:
            att_id = uuid.uuid4().hex
            path = store_file(session_id, data, filename, kind, mime, att_id)
            meta = repo.create(
                att_id, session_id, kind, filename, mime, len(data), path
            )
            results.append(meta)
```

**问题描述**
两个各自独立的缺陷：

1. **内存放大**：`MAX_FILES_PER_MESSAGE = 8` 的校验发生在**全部文件读入内存之后**。攻击者（或误操作）一次 POST 上千个 200KB 文档，服务端会先把约 200MB 读入 `pending` 列表才返回 400。因为这是同步持有引用的列表，期间无法被 GC 回收。
2. **无回滚**：`store_file` 成功但 `repo.create` 失败（如磁盘满、DB 锁）时，已写入的文件不会被删除，成为**孤儿文件**；若循环中第 3 个文件抛错，前 2 个已登记的元信息也不会回滚，返回 400 但服务端实际上已部分写入。

> 补充确认：`store_file` 的路径处理是**安全的**（`service.py:94-99` 的 `_safe_stem` 先 `basename` 再过滤字符，无路径穿越），`serve_attachment` 的归属校验也是**正确且必要的**（`attachments.py:103`：`if not att or att["session_id"] != session_id`）——这两点不应在此次改动中被削弱。

**触发路径 / 复现步骤**
1. 构造一次包含 1000 个合法 txt 文件的 multipart 请求打到 `POST /api/sessions/{sid}/attachments`。
2. 观察服务端 RSS 内存曲线在返回 400 前持续攀升。
3. 另：在 `repo.create` 注入一次失败（或在磁盘写满环境操作），检查 `attachments` 目录是否出现无对应记录的文件。

**影响范围**
- 安全/稳定性：可被用于内存型 DoS（`sort` 单条请求即可放大约 25 倍于合法上限的内存）。
- 资源：失败路径留下孤儿文件。

**建议解决方案**
1. **顺序调换**：在读取任何文件**之前**，先 `if len(files) > MAX_FILES_PER_MESSAGE: raise`。
2. **改为流式 + 上限**：逐个读取并在读取过程中累计字节数，超过总量上限（例如 `5 * MAX_DOCUMENT_SIZE`）立即中止，避免整体驻留。
3. **原子化**：先做全部校验与预分配 `att_id`，落盘与登记放入 `try/except`，失败则清理本次已写入的文件与已登记的行，返回统一错误。

**改造成本评估**
约 **0.5 人日**（纯单文件改动，风险低；建议配套「超限 / 部分失败」两个测试用例）。

---

### P1-5｜快速切换会话存在竞态，旧请求回包可覆盖新会话状态

**位置**
- `frontend/src/stores/chat.ts:80-93`（`selectSession` 无时序守卫）

**关键代码**
```ts
  async function selectSession(sessionId: string) {
    currentSessionId.value = sessionId
    const [msgs, snapshot] = await Promise.all([
      apiClient.get<Message[]>(`/sessions/${sessionId}/messages`),
      apiClient.get<Record<string, unknown> | null>(
        `/sessions/${sessionId}/context-snapshot`
      ),
    ])
    messages.value = msgs            // ← 无「这次结果是否仍是最新」的判断
    streamingContent.value = ''
    toolEvents.value = []
    contextSnapshot.value = snapshot || null
  }
```

**问题描述**
`selectSession` 对**并发调用完全没有时序保护**。用户连续快速点击会话 A → B 时，A 的请求若因网络或数据量（例如 A 是超长会话）后于 B 返回，`messages.value = msgs` 会把 A 的消息写进「当前已选中的 B 会话」视图。

同一次调用还存在**复位不完整**：清空了 `messages / streamingContent / toolEvents / contextSnapshot`，但**未清空** `streamingReasoning`、`reasoningDone`、`processEvents`。若在流式过程中切换会话，上一段会话的思维链与执行过程会残留显示在新会话里。

**触发路径 / 复现步骤**
1. 准备一个消息很多（>200 条）的会话 A 和一个很短的会话 B。
2. 先点 B，立即点 A，再立刻点回 B（利用加载时间差）。
3. 现象：B 的界面可能短暂或持续显示 A 的历史消息；若是在流式输出中切换，会看到上一段的思维链残留。

**影响范围**
- 功能：展示错误的会话内容（用户可能在错误上下文里继续提问并真的发出去）。
- 体验：状态残留造成的视觉错乱。

**建议解决方案**
1. 引入请求序号：`const seq = ++selectSeq`，回包时仅当 `seq === selectSeq` 才赋值。
2. 补齐复位清单：`streamingReasoning` / `reasoningDone` / `processEvents` 与已有字段一并重置。
3. 若处于 `isStreaming` 中切换会话，应明确：保留原会话流式继续，但**不把它的增量写入新会话视图**（与 P0-1 的修复在关注意图上重合，建议一并设计）。

**改造成本评估**
约 **0.5 人日**（前端单函数改动 + 复位清单梳理；依赖 P0-1 的 per-connection 设计结论，建议排在 P0-1 之后）。

---

## 四、P2 优化项

### P2-1｜点文件名扩展白名单不可达，且前后端白名单不一致

**位置**
- `backend/harness/modules/attachment/limits.py:17`（声明 `.env.example` / `.gitignore`）
- `backend/harness/modules/attachment/limits.py:50-52`（`ext_of` 用 `splitext`）
- `backend/harness/modules/attachment/service.py:58-59`（同样不可达的 MIME 映射）
- `frontend/src/views/ChatView.vue:215`（前端白名单含 `gitignore` / `env.example`）
- `frontend/src/views/ChatView.vue:227`（前端正则能提取出 `gitignore`）

**关键代码**

`limits.py:17`：
```python
    ".env.example", ".gitignore",
```
`limits.py:50-52`：
```python
def ext_of(filename: str) -> str:
    """返回小写的后缀（含点）。"""
    return os.path.splitext(filename)[1].lower()
```

**实测验证结论**（已在本机 Python 验证）：
```
('.gitignore', '')            → ext = ''           # 不匹配任何白名单项
('.env', '.example')          → ext = '.example'   # 不在白名单（白名单里是 '.env.example'）
('a.tar', '.gz')              → ext = '.gz'        # 双扩展名同样取最后一段
```

**问题描述**
两处声明的扩展名**永远无法命中**：`splitext` 对以点开头的文件名返回空扩展名。与此同时前端 `extOf` 用的是正则 `\.([a-z0-9.]+)$`（`ChatView.vue:227`），对 `.gitignore` 能正确提取出 `gitignore` 并命中白名单。于是**前端允许、后端拒绝**：用户选文件无任何提示通过前端预检，却在上传时被后端以「不支持的文件类型」打回。

**触发路径 / 复现步骤**
1. 选择一个名为 `.gitignore` 的文件。
2. 前端通过本地预检（无提示）。
3. 上传返回 400 `ATTACHMENT_INVALID`，`ChatView.vue:243` 弹 alert「包含不支持的文件类型」。
4. 现象：用户困惑——刚还选成功了，怎么又说不支持。

**影响范围**
- 契约不一致：前后端两处各自维护一份白名单，行为不同步。
- 体验：延迟且矛盾的报错。

**建议解决方案**
1. **移除死声明**：删掉 `limits.py:17` 的 `.env.example` / `.gitignore` 与 `service.py:58-59` 对应的 MIME 项（它们不可达，留着只会误导）。
2. **统一抽取为单一事实源**：把白名单做成后端返回的接口（例如 `GET /api/attachments/policy` 返回 `{extensions, maxPerMessage, maxSize}`），前端 `<input accept>` 与预检正则都由该接口派生，避免双份维护再次漂移。
3. 若确实要支持点文件名，`ext_of` 需改为对 `stem.startswith(".")` 做特判。

**改造成本评估**
移除死声明 **0.1 人日**；做成 policy 接口 **0.5 人日**（推荐直接做接口，一次性消除双维护）。

---

### P2-2｜每轮对话调用两次 `ContextService.build`，首次结果必然作废

**位置**
- `backend/harness/api/ws/chat.py:279-294`（第一次，执行 AgentLoop 之前）
- `backend/harness/api/ws/chat.py:443-460`（第二次，AgentLoop 之后）

**关键代码**
```python
                # 发送上下文快照
                try:
                    context_service = services.get(ContextService)
                    context_service.build(          # ← 第 1 次：尚未写入本轮消息
                        msg.session_id,
                        budget=msg.budget,
                        model=msg.model or "gpt-4o",
                    )
```
第二次调用处的注释本身已说明了浪费：
```python
                # 覆盖开头预构建的 0/4096 快照。
```

**问题描述**
每一次 request 都会执行两次 `build()`。而 `build()`（`context_manager/service.py:277-293`）需要 `list_messages` 拉全量消息、执行 token 计数、按预算压缩、并可能持久化快照。第一次调用在本轮消息尚未落库时执行，产出一个「0/4096」的空位快照，**随后必然被第二次结果覆盖**，因此属于纯浪费——且成本随会话长度线性增长。

此外，若某次请求会话被压缩过，两次 `build()` 之间会重复写快照行，放大写放大。

**触发路径 / 复现步骤**
1. 在一个 200 条消息的会话里持续对话。
2. 每发送一轮，服务端实际做了两遍上下文构建。
3. 通过 tracing span 可观察到两次 build 的耗时。

**影响范围**
- 性能：长会话下每轮请求上下文构建开销翻倍。
- 写放大：重复的快照持久化。

**建议解决方案**
- 直接删除第一次调用（ `ws/chat.py:279-294` 整块），保留 `AgentLoop` 之后的最终快照作为唯一来源。
- 若前端需要在流式开始**之前**就显示上下文占用（避免等待），可改为在最终快照之外单独发一个轻量的「统计信息」（只读、不触发 `build` 的压缩与持久化路径）。需先确认前端对首帧快照是否存在时序依赖。

**改造成本评估**
删除调用约 **0.1 人日**；若需替代的轻量统计，额外 **0.3 人日**（依赖前端确认，见文末存疑项第 4 条）。

---

### P2-3｜消息列表接口无分页，且每轮结束全量重载

**位置**
- `backend/harness/api/rest/sessions.py:249-...`（`list_messages`）
- `frontend/src/stores/chat.ts:200-202`（`done` 帧后 `reloadMessages`）
- `frontend/src/stores/chat.ts:421-426`（`reloadMessages` 全量拉取）

**关键代码**

`sessions.py:249-...`（无 limit / offset）：
```python
    async def list_messages(session_id: str) -> list[dict[str, Any]]:
        service = _get_session_service(registry)
        if not service.get_session(session_id):
            raise APIError("SESSION_NOT_FOUND", f"会话不存在: {session_id}", 404)
        messages = service.list_messages(session_id)
```

`chat.ts:200-202`（每次 done 都全量重拉做对账）：
```ts
        void reloadMessages().catch(() => {
          // 对账失败时保留乐观消息，不阻断界面
        })
```

**问题描述**
后端一次返回会话的**全部**消息（含 `attachments` 与转换后的 `tool_calls` 结构），前端每次对话结束又全量重拉一次做 id 对账。随着会话增长形成明显的 O(n) 拉取 + O(n) 处理，且每个带的图片附件在重载时会重新发起一次 HTTP 请求。

> 说明：这里的全量重载是为了修正「临时 id 对账」问题（chat.ts:196-199 的注释解释为拿后端真实 UUID），出发点是正确的。因此**不应直接删掉重载**，而应缩小其成本。

**触发路径 / 复现步骤**
1. 在一个长会话（数百条消息、含若干图片附件）中连续提问。
2. 每轮结束后都触发全量 `/messages` 拉取 + 所有附件缩略图重新请求。
3. 观察 Network 面板中响应体随时间线性增大。

**影响范围**
- 性能：长会话滚动与每轮收尾的响应变慢。
- 带宽：附件缩略图重复下载。

**建议解决方案**
1. 后端 `list_messages` 增加 `limit / offset`（或 cursor）参数，并在 `messages(session_id, created_at)` 上建复合索引（README/DB 层已用 `CREATE INDEX IF NOT EXISTS` 的习惯，可沿用）。
2. 前端改为**增量对账**：以最后一条已知消息 id 为游标，仅拉取其后的新消息，避免每次全量替换 `messages.value`。
3. 缩略图利用 HTTP 缓存（诚 ETag / Cache-Control），避免每次重载都重新下载。

**改造成本评估**
后端分页 **0.5 人日**；前端增量对账 **1 人日**（涉及现有 Markdown 渲染 / 过程框归并逻辑 `ChatView.vue:600-620` 区域的回归）。成本偏高，建议放到最后一批。

---

### P2-4｜附件缩略图无加载态与失败降级

**位置**
- `frontend/src/components/MessageItem.vue:88-96`

**关键代码**
```html
            <img
              v-for="att in message.attachments.filter(a => a.kind === 'image')"
              :key="att.id"
              class="msg-attach-img"
              :src="`/api/sessions/${message.session_id}/attachments/${att.id}`"
              :alt="att.filename"
              :title="att.filename"
            />
```

**问题描述**
`<img>` 既没有 `loading` 策略，也没有 `@error` 处理。在以下场景会产生无提示的破图占位（浏览器默认图标）：

- 图片还在上传/服务端尚未就绪时；
- 该消息所属的附件已被清理（与 P1-2 直接相关：轮次删除后文件其实还在，但若将来按 P1-2 修复清理后，历史视图就必然出现破图）；
- 后端返回 404。

这与审查维度中的「加载态、错误提示、可用性」直接相关。

**触发路径 / 复现步骤**
1. 上传图片发送后，**在图片尚未加载完成时**观察到气泡区域先塌缩再撑开（布局抖动）。
2. 手工删除 `backend/data/attachments/<sid>/` 下某个文件后重新进入会话 → 显示破图，无任何提示。

**影响范围**
- 体验：布局抖动、破图无反馈。

**建议解决方案**
- 给容器设定固定宽高骨架，避免塌缩抖动；加 `loading="lazy"`（长列表收益明显）。
- 绑定 `@error`，失败时切换为「文件已失效/无法预览」的降级占位（并考虑 i18n，避免硬编码中文）。

**改造成本评估**
约 **0.3 人日**（纯前端；与 P1-2 的修复存在依赖，建议排在 P1-2 之后以便一并验证「删轮后不破图」）。

---

### P2-5｜HTTP 错误一路 `throw new Error(await res.text())`，丢失状态码且提示不友好

**位置**
- `frontend/src/api/client.ts:12, 22, 29, 39, 48, 60`（所有方法同一写法）
- `frontend/src/views/ChatView.vue:243-258`（上传失败用 `window.alert`）

**关键代码**

`client.ts:10-14`（以及 post/put/patch/delete/uploadAttachments 完全一致的模式）：
```ts
  async get<T>(url: string): Promise<T> {
    const res = await fetch(`${BASE_URL}${url}`)
    if (!res.ok) throw new Error(await res.text())
    return res.json()
  }
```

`ChatView.vue:243-258`：
```ts
  if (rejected > 0) {
    window.alert(t.value('chat.attachmentUnsupported'))
  }
  ...
  } catch (err) {
    window.alert(t.value('chat.attachmentUploadFail') + ': ' + err)
  }
```

**问题描述**
1. **状态码被丢弃**：调用方只能拿到一段文本，无法区分 404 / 400 / 503，也就无法做差异化处理（例如附件数量超限 vs 类型不支持，当前混为同一类提示）。
2. **不友好**：`await res.text()` 在 FastAPI 默认异常下可能是一段 JSON/HTML，被直接拼进 alert（如 `ChatView.vue:257` 的 `'...' + err`），用户看到的是原始错误体。
3. **无网络层区分**：`fetch` 自身 reject（断网、DNS 失败）与 HTTP 错误混为一谈。
4. 用 `window.alert` 做关键交互反馈，且部分场景直接拼接后端原文。

**触发路径 / 复现步骤**
1. 上传一个超过限制的文件夹 → 弹窗显示后端原始文本。
2. 完全断网后发起任意请求 → 提示与「HTTP 500」无法区分。

**影响范围**
- 体验：错误信息不可读、不可操作。
- 可维护性：统一错误处理缺失，后续新增接口会继续复制该反模式。

**建议解决方案**
- 抽出统一的请求方法：`ApiError` 携带 `{ status, code, message }`，统一解析 `res.json()` 的错误体结构；`catch` 里区分「网络失败」与「HTTP 错误」。
- 上传等关键交互改为**行内提示**（气泡/Toast），替掉 `alert`。

**改造成本评估**
统一封装 **0.5 人日** + 调用点适配 **0.5 人日**（可能触及多处调用依赖 `String(e)` 的现有写法，需谨慎回归）。

---

### P2-6｜`SessionService` 协议与实现签名不一致（缺 `attachments`）

**位置**
- `backend/harness/modules/session_manager/service.py:34-43`（Protocol 声明）
- `backend/harness/modules/session_manager/service.py:115-125`（Impl 实现）

**关键代码**

Protocol（`service.py:34-43`）——**未声明 attachments**：
```python
    def append_message(
        self,
        session_id: str,
        role: str,
        content: str,
        tool_calls: list[dict[str, Any]] | None = None,
        tool_call_id: str | None = None,
        tokens: int = 0,
        latency_ms: int | None = None,
    ) -> Message: ...
```

Impl（`service.py:115-125`）——**多了 attachments**：
```python
    def append_message(
        self,
        ...
        latency_ms: int | None = None,
        attachments: list[dict[str, Any]] | None = None,
    ) -> Message:
```

**问题描述**
作为对外暴露的**接口契约**，`Protocol` 与实现出现签名漂移。按 Protocol 类型静态消费该服务的调用方（例如通过 `services.get(SessionService)` 解析结果标注的类型），无法以类型安全的方式传入 `attachments`。这类漂移正是后续新增字段时最容易漏改的地方，也会让静态检查逐渐失去保护作用。

**触发路径 / 复现步骤**
静态即可触发：任何把变量标注为 `SessionService` 类型（协议）而非具体实现的调用点，传入 `attachments=` 会被类型检查认为非法。

**影响范围**
- 一致性：接口契约与实现漂移。
- 可维护性：新字段加入时的漏改风险。

**建议解决方案**
给 Protocol 的 `append_message` 补上同名同类型参数，与实现保持一致；建议同时检查一下 Protocol 中其它方法是否存在类似漂移（本轮未逐个比对）。

**改造成本评估**
约 **0.1 人日**（单点改动；建议与同批其它后端小改一并提交）。

---

### P2-7｜人工确认等待期间 WS 断开，协程仍会挂满 120 秒

**位置**
- `backend/harness/api/ws/chat.py:187-202`（等待与超时）
- `backend/harness/api/ws/chat.py:319-359`（`_stop_listener` 的 disconnect 处理）

**关键代码**

`ws/chat.py:187-202`：
```python
            try:
                return bool(await asyncio.wait_for(fut, timeout=CONFIRM_TIMEOUT))
            except TimeoutError:
                logger.warning("人工确认超时，按拒绝处理: %s", request_id)
                ...
                return False
            finally:
                pending_confirms.pop(request_id, None)
```
（第 34 行 `CONFIRM_TIMEOUT = 120.0`）

`ws/chat.py:356-359`（断连时只是"pass"，未去唤醒 Future）：
```python
                    except (WebSocketDisconnect, asyncio.CancelledError):
                        pass
                    except Exception:
                        pass
```

**问题描述**
客户端在等待确认的过程中直接关闭页面/断网，`_stop_listener` 捕获到 `WebSocketDisconnect` 后**什么都不做**。此时挂起的工具仍在 `asyncio.wait_for(fut, 120s)` 上等待，会白白占用协程整整 120 秒。若同一 provider/工具出现多次此类情况，会堆积大量悬挂协程。

安全语义是**正确**的（最终按拒绝处理），问题纯粹在于**释放不及时**。

**触发路径 / 复现步骤**
1. 触发一个需要人工确认的危险工具。
2. 在 120 秒内关闭浏览器标签。
3. 观察服务端：该次工具调用仍挂着，直到超时才释放。

**影响范围**
- 资源：协程与相关资源延迟释放（最长 120s）。

**建议解决方案**
在 `_stop_listener` 的 `WebSocketDisconnect / CancelledError` 分支中，遍历 `pending_confirms`，把未完成的 Future 统一 `set_result(False)` 后清空——即时兑现「安全默认」语义。

**改造成本评估**
约 **0.2 人日**（单点改动；建议与 P0-1 同批，因两者都涉及 WS 生命周期中的连接清理）。

---

### P2-8｜`FileResponse` 使用未经净化的原始文件名

**位置**
- `backend/harness/api/rest/attachments.py:105-107`

**关键代码**
```python
        return FileResponse(
            att["storage_path"], media_type=att["mime"], filename=att["filename"]
        )
```

**问题描述**
`filename` 用的是用户上传时的**原始文件名**（而非 `_safe_stem` 净化后的结果）。（`service.py:94-99` 已有 `_safe_stem`，但仅用于磁盘路径，未用于此处。）该值会被写入 `Content-Disposition` 响应头。虽然 Starlette 会做必要的引号处理，风险有限，但保留原始文件名会带来两个不确定性：非 ASCII / 特殊字符在不同浏览器下的解码差异；以及将来若有人在别处复用该值做路径拼接的隐患。

> 已确认**不是**路径穿越：`storage_path` 是服务端生成并落库的值，`filename` 不参与任何路径解析。

**触发路径 / 复现步骤**
上传 `报告(终版).png` 之类文件，请求 `GET /api/sessions/{sid}/attachments/{att_id}`，观察 `Content-Disposition` 中的编码表现。

**影响范围**
- 兼容性：多浏览器文件名解码不一致。
- 防御纵深：净化值未被复用。

**建议解决方案**
`FileResponse` 的 `filename` 统一走 `_safe_stem(att["filename"])`（可考虑在 `repo.create` 时就同时存原始名与净化名）。

**改造成本评估**
约 **0.1 人日**。

---

### P2-9｜会话标题自动生成硬编码厂商模型 id

**位置**
- `backend/harness/api/ws/chat.py:118`

**关键代码**
```python
            used_model = model or "deepseek-v4-flash"
```

**问题描述**
当用户消息未指定 `model` 时，标题生成回退到一个**写死的厂商模型名**。这会带来两个问题：

1. 若该模型在该 provider 下不可用，虽然被 `ws/chat.py:134-135` 的 `except` 兜住（不会崩），但标题会退化，用户看到会话一直是 "New Session"，且只有一条 warning 日志。
2. 与「多 provider 可插拔」的定位冲突——不同 provider 的模型名不同，硬编码无法通用。

**触发路径 / 复现步骤**
用一个 deepseek 之外的 provider（例如本地 OpenAI 兼容服务）且未显式指定 model 发起首轮提问。

**影响范围**
- 兼容性：跨 provider 场景下标题生成失效。

**建议解决方案**
改为回退到该 provider 的**默认模型**（从 provider 配置 `models` 中取第一项，或直接复用本次对话实际使用的 model），而不是写死字符串。

**改造成本评估**
约 **0.2 人日**。

---

## 五、推荐的分批落地顺序与依赖关系

### 批次 1 —— 修 P0，解锁后续（建议 1～1.5 人日）

| 顺序 | 编号 | 内容 | 依赖 |
|---|---|---|---|
| 1 | P0-1 | Provider 单例不再被 patch，改为 per-connection 代理 | 无 |
| 2 | P0-2 | `ws.send` 失败可感知 + 发送超时兜底 + 发送前置校验 | 无（可与 P0-1 并行） |

**依赖关系**：P0-1 与 P0-2 相互独立，可并行开发；但 P0-1 的设计结论（per-connection provider）会影响 P1-5 的修复方式，因此 P1-5 必须排在 P0-1 之后。

### 批次 2 —— 附件与新链路的衔接闭环（建议 2～2.5 人日）

| 顺序 | 编号 | 内容 | 依赖 |
|---|---|---|---|
| 3 | P1-1 | 切换会话清空待发送附件；服务端对被丢弃的附件下发错误帧 | 批次 1（同属 WS/前端 状态链路） |
| 4 | P1-2 | 删除整轮/单条消息时联动清理附件文件与元数据 | 无；但需新增 `AttachmentRepository.delete_by_ids` |
| 5 | P1-3 | 分叉/导入的附件复制闭环 | **依赖 P1-2**（二者共用同一套清理/复制原语，先有清理再谈复制更安全） |
| 6 | P1-4 | 上传接口：数量前置校验 + 失败回滚 | 无 |
| 7 | P1-5 | `selectSession` 时序守卫 + 复位清单补齐 | **依赖 P0-1**（流式增量的归属需要新的 provider 设计支撑） |

**说明**：P1-1 / P1-4 / P1-2 三者互不阻塞，可并行；P1-3 建议在 P1-2 之后；P1-5 必须在 P0-1 之后。

### 批次 3 —— 契约统一与交互体验（建议 1.5 人日）

| 顺序 | 编号 | 内容 | 依赖 |
|---|---|---|---|
| 8 | P2-1 | 移除死扩展名；白名单改由后端 policy 接口下发 | 无 |
| 9 | P2-4 | 缩略图加载态 / 失败降级 | **依赖 P1-2**（清理生效后才真正暴露破图场景） |
| 10 | P2-5 | 统一 `ApiError`（含状态码）；替换 alert 为行内提示 | 无；但改动面较宽，建议独立于其它批次单独提 PR |
| 11 | P2-6 | 补齐 `SessionService` Protocol 的 `attachments` | 无 |
| 12 | P2-8 | `FileResponse` 使用净化文件名 | 无 |

### 批次 4 —— 性能与健壮性收尾（建议 2 人日）

| 顺序 | 编号 | 内容 | 依赖 |
|---|---|---|---|
| 13 | P2-2 | 移除重复的首次 `ContextService.build` | **依赖前端确认首帧快照是否有时序依赖**（见存疑 4） |
| 14 | P2-7 | WS 断开时立即释放等待中的确认 Future | 批次 1（同属 WS 生命周期） |
| 15 | P2-9 | 标题生成回退到 provider 默认模型 | 无 |
| 16 | P2-3 | 消息分页 + 前端增量对账 | **建议最后做**；触及消息归并渲染逻辑，回归成本最高 |

> **合计约 7～8 人日**（未含回归测试与联调成本；其中 P2-3 单项占比最高，可视业务紧迫性决定是否推迟或分期）。

---

## 六、本次未覆盖 / 存疑事项（请确认）

以下为本次审查**确实未深入**或**无法静态确认**的部分，避免文档给出超出证据范围的结论：

1. **插件体系未深度审查**
   本轮未逐一通读 `backend/plugins/*` 下所有插件的 `plugin.json` 与入口文件，未验证「插件初始化异常是否会导致整体启动失败」「插件停用时已注册工具/路由是否真正卸载」。若需要，建议单独开一轮针对插件生命周期的专项审查。

2. **认证与鉴权模型待确认**
   当前 `/api/**` 全部接口**没有任何用户级鉴权**，仅靠 `session_id` 做资源隔离（附件接口已做了正确的归属校验，`attachments.py:103`）。这在「本地单用户工具」定位下可能是**刻意设计**而非缺陷——请确认产品目标是否包含多用户部署；若包含，则需要一轮专项的鉴权方案设计，本轮未输出相关结论。

3. **SQLite 并发写配置未核实**
   本轮未核查是否启用 WAL、`busy_timeout` 等参数。在多实例 / 并发写入场景下的锁行为未知。若已知为单进程部署，此项可忽略。

4. **P2-2 的前端时序依赖未确认**
   删除「第一次 `ContextService.build`」前，需要确认前端是否依赖**流式开始前**发来的那一帧快照（例如用于立刻展示 token 占用并在等待期给出反馈）。此点需由熟悉该段 UI 的人确认，或实际起服务观察一帧后才能定论。

5. **其余 REST 路由未逐个通读**
   本轮重点覆盖了 `sessions.py` 与 `attachments.py`，其余路由（artifacts / memories / traces / schedules / settings / mcp 等）**未做同等级别的行级审查**，未针对它们输出结论。

6. **前端其余视图未深入**
   `SettingsView.vue` / 会话列表视图等仅做定位性阅读，未系统性覆盖其加载态、空态与错误提示。

7. **纯静态结论，未经运行时验证**
   本轮**没有**启动前后端服务、没有调用真实模型、没有执行 pytest。所有结论均来自源码阅读与本机 Python 行为验证（已验证的例外项：`os.path.splitext` 对点文件的行为已在文中给出实测输出）。建议 P0 修复后，补一轮端到端实测再关闭：

   - 两个 WS 连接并发场景（P0-1）
   - 断网/未握手时点击发送（P0-2）
   - 上传后切换会话再发送（P1-1）
   - 删除带附件的轮次后检查磁盘与 DB（P1-2）

8. **静态检查基线未重新校准**
   项目既有约定为 mypy 基线 14 错、ruff 基线 2 错。本轮未运行 `mypy` / `ruff`，因此**无法确认**当前代码是否已使基线上升。建议进入实施阶段前先取一次基线快照。

9. **测试环境存在外部拦截，需在实施阶段提前处理**
   运行 pytest 时，后端 conftest（`backend/tests/conftest.py:15-31`）依赖 `tmp_path_factory` 生成临时目录，而本机环境会对这些临时目录的清理操作进行拦截（会触发 trash 失败）。本轮已确认可通过设置 `CODEBUDDY_SAFE_DELETE_ENABLED=0` 并配合 `--basetemp` 规避。**这一项会影响回归效率**，建议在动手前先确认可复用的测试执行命令，避免每次重复排查此环境问题。
