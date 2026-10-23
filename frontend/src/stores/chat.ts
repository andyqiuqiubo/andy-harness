import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import type { Session, Message, TokenUsage, WSFrame, Attachment, ProcessStep } from '../api/types'
import { apiClient } from '../api/client'
import { useSettingsStore } from './settings'

export const useChatStore = defineStore('chat', () => {
  const sessions = ref<Session[]>([])
  const currentSessionId = ref<string | null>(null)
  const messages = ref<Message[]>([])
  const streamingContent = ref('')
  const streamingReasoning = ref('')
  const reasoningDone = ref(false)
  const toolEvents = ref<Record<string, unknown>[]>([])
  const isStreaming = ref(false)
  const contextSnapshot = ref<Record<string, unknown> | null>(null)
  const error = ref<string | null>(null)
  const mockMode = ref(false)
  let mockTimer: ReturnType<typeof setTimeout> | null = null

  /** 正在「重新回答」的提问 id（其下会新增一条答案版本；用于 UI 显示「生成中」）。 */
  const regeneratingParentId = ref<string | null>(null)

  /**
   * 停止生成占位提示（仅「思考过程、尚无正文」被中断时使用）。
   * 真实模式下后端只在 result.content 非空时落库 stopped 消息（见 agent_loop
   * 的 `if result.content`），因此思考阶段被中断不会留下持久化消息；若直接 push
   * 进 messages，会被 400ms 后的 reloadMessages 整表替换抹掉，提示闪现即消失。
   * 故用独立 ref 承载「用户已停止回答」提示，与 reload 解耦，确保稳定可见。
   */
  const stoppedPlaceholder = ref<Message | null>(null)
  const stoppedPlaceholderUserMsgId = ref<string | null>(null)

  /**
   * 当前流式回答**归属的会话 id**。
   *
   * WS 是全局共享的单连接：后端为会话 A 生成的 token_delta / tool_event / done
   * 帧，在用户中途切到会话 B 之后仍会继续推来。旧实现不加区分地处理，导致：
   * - `done` 把 A 的答案 push 进 B 的消息列表（跨会话串数据）；
   * - `isStreaming` 迟迟不落回 false，B 的输入框与发送按钮被禁用。
   *
   * 注意：切换会话时**不清空**该值（旧实现置 null 反而使归属过滤失效，
   * A 的迟到帧全部写进 B）。它持续指向正在生成的会话，配合帧自带的
   * session_id（后端已带上）与下方暂存机制，保证：
   * - 切走时 A 的迟到帧被丢弃，不污染 B；
   * - 切回 A 时已生成内容原样恢复，继续实时追加。
   */
  const streamingSessionId = ref<string | null>(null)

  /**
   * 流式生成状态的按会话暂存。
   *
   * 生成途中切走/新建会话时，把已生成的正文、思维链、过程步骤暂存到
   * 「流式归属会话」名下；返回该会话时恢复，用户能完整看到已生成的回答
   * 文字并继续实时追加（修复「新建后返回原会话只剩提问」）。
   *
   * finalized=true 表示该会话的生成已收尾（完成 / 被停止 / 被切换中断），
   * 内容应作为一条消息落入该会话的消息列表（模拟模式专用：真实模式下
   * 服务端已落库，done 帧到达即丢弃暂存）。
   */
  interface StreamSnapshot {
    content: string
    reasoning: string
    reasoningDone: boolean
    toolEvents: Record<string, unknown>[]
    processEvents: ProcessStep[]
    regeneratingParentId: string | null
    finalized?: boolean
    stopped?: boolean
  }
  const streamStash = ref<Record<string, StreamSnapshot>>({})

  function _snapshotStream(): StreamSnapshot {
    return {
      content: streamingContent.value,
      reasoning: streamingReasoning.value,
      reasoningDone: reasoningDone.value,
      toolEvents: [...toolEvents.value],
      processEvents: [...processEvents.value],
      regeneratingParentId: regeneratingParentId.value,
    }
  }

  function _restoreStream(snap: StreamSnapshot) {
    streamingContent.value = snap.content
    streamingReasoning.value = snap.reasoning
    reasoningDone.value = snap.reasoningDone
    toolEvents.value = [...snap.toolEvents]
    processEvents.value = [...snap.processEvents]
    regeneratingParentId.value = snap.regeneratingParentId
  }

  /** 清空实时流式态（内容保留由调用方决定，走暂存或落库）。 */
  function _clearStreamLive() {
    streamingContent.value = ''
    streamingReasoning.value = ''
    reasoningDone.value = false
    toolEvents.value = []
    processEvents.value = []
    regeneratingParentId.value = null
  }

  function _stashFor(sessionId: string, snap: StreamSnapshot) {
    streamStash.value = { ...streamStash.value, [sessionId]: snap }
  }

  function _dropStash(sessionId: string | null | undefined) {
    if (!sessionId || !streamStash.value[sessionId]) return
    const next = { ...streamStash.value }
    delete next[sessionId]
    streamStash.value = next
  }

  /**
   * 离开「正在流式生成的会话」时的统一处理：
   * - 真实模式（WS）：内容暂存，返回时恢复并继续实时追加；
   * - 模拟模式：本地定时器无法在后台继续，停止生成并把已生成内容作为
   *   「已停止」回答暂存，返回时作为消息落入列表（内容不丢失）。
   */
  function _leaveStreamingSession(prevId: string | null) {
    if (!prevId || !streamingSessionId.value || streamingSessionId.value !== prevId) return
    if (mockTimer) {
      clearTimeout(mockTimer)
      mockTimer = null
      _stashFor(prevId, { ..._snapshotStream(), finalized: true, stopped: true })
      _clearStreamLive()
      isStreaming.value = false
      streamingSessionId.value = null
      return
    }
    _stashFor(prevId, _snapshotStream())
  }

  /**
   * 模板中心实例化后暂存的首条提问：跳转会话页、WS 就绪后自动经正常
   * 发送链路发出（走 AgentLoop 才有流式回答；仅落库的提问是「死消息」）。
   * 附目标会话 id：用户中途切走会话时不得误发到别的会话。
   */
  const pendingAutoSend = ref('')
  const pendingAutoSendSession = ref<string | null>(null)

  // 执行过程步骤（思维链 / 中间说明 / 工具调用），实时累积，
  // 供 ChatView 的「执行过程」过程框统一展示。
  const processEvents = ref<ProcessStep[]>([])

  /** 追加思维链文本（连续 delta 合并进同一条；新一轮自动开新条目）。 */
  function _appendReasoning(delta: string) {
    const last = processEvents.value[processEvents.value.length - 1]
    if (last && last.kind === 'reasoning') {
      last.text = (last.text || '') + delta
    } else {
      processEvents.value.push({ kind: 'reasoning', text: delta })
    }
  }

  /**
   * 记录一次工具调用，并把本轮已流式输出的正文「移交」进过程框：
   * 模型在调工具前输出的阶段性说明（如"我先加载该技能"）属于执行过程，
   * 不应留在 streamingContent 里混入最终答案。
   */
  function _pushToolEvent(ev: Record<string, unknown>) {
    if (streamingContent.value) {
      processEvents.value.push({ kind: 'text', text: streamingContent.value })
      streamingContent.value = ''
    }
    processEvents.value.push({ kind: 'tool', tool: ev as ProcessStep['tool'] })
  }

  // 待发送附件（用户选好文件后，发送前暂存；发送后清空）
  const pendingAttachments = ref<Attachment[]>([])
  const uploading = ref(false)
  /** P1-1：服务端告知「附件未随本次提问送达」时的可见提示（不阻断对话）。 */
  const attachmentWarning = ref('')

  // ── P0-2：流式响应看门狗 ────────────────────────────
  // 没有任何超时机制时，一旦服务端没回帧（请求被丢弃 / 连接已死），
  // isStreaming 会永久为 true，输入框与发送按钮随之全部禁用。
  // 这里用「首个响应超时」兜底，把界面恢复到可操作状态。
  //
  // 阈值须**大于**后端单次模型请求的超时（provider 为 60s），否则后端尚在
  // 连接/生成时就先被误判为「连接中断」，且会遮盖后端稍后回传的真实错误帧。
  const STREAM_WATCHDOG_MS = 90000
  let streamWatchdog: ReturnType<typeof setTimeout> | null = null

  function _clearWatchdog() {
    if (streamWatchdog) {
      clearTimeout(streamWatchdog)
      streamWatchdog = null
    }
  }

  function _armWatchdog() {
    _clearWatchdog()
    streamWatchdog = setTimeout(() => {
      streamWatchdog = null
      if (!isStreaming.value) return
      isStreaming.value = false
      error.value = '未收到服务端响应，可能连接已中断。请检查连接后重试。'
      _clearStreamLive()
      // 连接已断：清理暂存与流式归属，避免返回该会话时恢复出幽灵流式态。
      // 服务端若仍在生成，done 帧（重连后）会以持久化消息正确呈现。
      _dropStash(streamingSessionId.value)
      streamingSessionId.value = null
    }, STREAM_WATCHDOG_MS)
  }

  const currentSession = computed(() =>
    sessions.value.find((s) => s.id === currentSessionId.value)
  )

  async function loadSessions() {
    try {
      sessions.value = await apiClient.get<Session[]>('/sessions')
      // M17: Auto-select first non-archived session
      if (!currentSessionId.value) {
        const firstActive = sessions.value.find((s) => !s.archived)
        if (firstActive) {
          await selectSession(firstActive.id)
        }
      }
    } catch (e) {
      error.value = String(e)
    }
  }

  async function createSession(title = 'New Session') {
    const session = await apiClient.post<Session>('/sessions', { title })
    // 生成途中点「新建」：原会话的流式状态必须暂存（返回时恢复），
    // 实时态必须清空——否则原会话已生成的回答会显示在新会话里，
    // 且新会话的输入框被 isStreaming 禁用。
    _leaveStreamingSession(currentSessionId.value)
    sessions.value.unshift(session)
    currentSessionId.value = session.id
    messages.value = []
    _clearStreamLive()
    contextSnapshot.value = null
    isStreaming.value = false
    // 新会话：清空停止占位提示（属于上一会话的瞬时 UI）
    stoppedPlaceholder.value = null
    stoppedPlaceholderUserMsgId.value = null
    return session
  }

  // P1-5：切换会话的时序守卫。连续快速点击会话时，慢的那次请求可能在新的之后返回，
  // 从而把旧会话的消息写进当前已选中的会话视图。
  let selectSeq = 0

  async function selectSession(sessionId: string) {
    const seq = ++selectSeq
    const prevId = currentSessionId.value
    // 离开正在流式生成的会话：暂存已生成内容（真实模式返回时恢复并继续
    // 实时追加；模拟模式作为「已停止」回答暂存）。streamingSessionId 继续
    // 指向原会话，使迟到帧继续被归属过滤丢弃，不写进新会话。
    _leaveStreamingSession(prevId)
    currentSessionId.value = sessionId
    // 立即清空上一会话的残留（补齐原先漏掉的思维链 / 过程框，避免跨会话串显）
    messages.value = []
    _clearStreamLive()
    contextSnapshot.value = null
    // 切换会话：清空停止占位提示（属于上一会话的瞬时 UI）
    stoppedPlaceholder.value = null
    stoppedPlaceholderUserMsgId.value = null
    // P1-1：待发送附件从属于**某个会话**；切会话必须清空，
    // 否则会把 A 的附件 id 发到 B（服务端安全地丢弃，用户却以为已经附上了）。
    pendingAttachments.value = []
    attachmentWarning.value = ''
    _clearWatchdog()
    // 目标会话有未收尾的暂存流式产物 → 该会话的生成仍在进行：
    // 恢复已生成内容并保持流式态，用户返回时完整看到已生成的回答文字，
    // 并继续实时追加（修复「新建/切换后返回只剩提问」）。
    // 暂存被丢弃的时机即生成收尾（done / stop / error / 看门狗），
    // 因此「暂存存在且未 finalized」等价于「生成仍在进行」。
    const stash = streamStash.value[sessionId]
    if (stash && !stash.finalized) {
      _restoreStream(stash)
      isStreaming.value = true
      streamingSessionId.value = sessionId
    } else {
      isStreaming.value = false
    }
    error.value = null
    // 模拟模式的定时器同理，必须一并停掉
    if (mockTimer) {
      clearTimeout(mockTimer)
      mockTimer = null
    }

    // 并行加载消息与最新上下文快照（有快照则显示，无则保持 null 隐藏）
    try {
      const [msgs, snapshot] = await Promise.all([
        apiClient.get<Message[]>(`/sessions/${sessionId}/messages`),
        apiClient.get<Record<string, unknown> | null>(
          `/sessions/${sessionId}/context-snapshot`
        ),
      ])
      // 已有更新的切换请求 → 丢弃本次回包
      if (seq !== selectSeq) return
      messages.value = msgs
      contextSnapshot.value = snapshot || null
      // 暂存的是已收尾的部分回答（模拟模式切走中断）→ 作为消息补进列表，
      // 保证返回原会话时已生成的文字完整可见。
      const fin = streamStash.value[sessionId]
      if (fin?.finalized && (fin.content || fin.reasoning)) {
        messages.value = [
          ...messages.value,
          {
            id: Date.now().toString(),
            session_id: sessionId,
            role: 'assistant',
            content: fin.content,
            tokens: 0,
            stopped: fin.stopped !== false,
            created_at: new Date().toISOString(),
          },
        ]
      }
      _dropStash(sessionId)
    } catch (e) {
      if (seq === selectSeq) error.value = String(e)
    }
  }

  async function renameSession(sessionId: string, title: string) {
    const updated = await apiClient.patch<Session>(`/sessions/${sessionId}`, { title })
    const idx = sessions.value.findIndex((s) => s.id === sessionId)
    if (idx >= 0) sessions.value[idx] = updated
  }

  async function archiveSession(sessionId: string) {
    await apiClient.patch(`/sessions/${sessionId}`, { archived: true })
    const session = sessions.value.find((s) => s.id === sessionId)
    if (session) {
      session.archived = true
    }
    if (currentSessionId.value === sessionId) {
      const nextActive = sessions.value.find((s) => !s.archived)
      if (nextActive) {
        await selectSession(nextActive.id)
      } else {
        currentSessionId.value = null
        messages.value = []
        contextSnapshot.value = null
      }
    }
  }

  // 检测 theme_switcher 工具结果中的主题切换指令并应用
  function _handleThemeSwitch(toolEventData: Record<string, unknown>) {
    const toolName = toolEventData.tool_name as string
    const result = toolEventData.result as string
    if (toolName !== 'theme_switcher' || !result) return
    // 解析 __THEME__:xxx 指令
    const match = String(result).match(/__THEME__:(\w+)/)
    if (match) {
      const themeId = match[1]
      const settingsStore = useSettingsStore()
      try {
        settingsStore.setTheme(themeId as never)
      } catch {
        // 未知主题，忽略
      }
    }
  }

  function handleWSFrame(frame: WSFrame) {
    // 丢弃「非当前会话」的流式帧：用户在 A 生成途中切到 B 后，A 的
    // token_delta / tool_event / done 仍会经同一条 WS 推来。不加归属校验会把
    // A 的答案写进 B，并让 B 的 isStreaming 迟迟不落回 false（输入框被禁用）。
    // 归属优先取帧自带 session_id（后端已带上，支持多会话并发生成），
    // 旧帧回退到 streamingSessionId（本次流式归属）。
    // 控制类帧（error / confirm_* / attachment_warning / context_snapshot）不参与。
    const frameSession =
      (frame.data && typeof frame.data === 'object' && 'session_id' in frame.data
        ? (frame.data as { session_id?: unknown }).session_id
        : undefined) ?? streamingSessionId.value
    const _streamKinds = ['token_delta', 'tool_event', 'done']
    if (
      frameSession &&
      String(frameSession) !== currentSessionId.value &&
      _streamKinds.includes(frame.type)
    ) {
      // 非当前会话的迟到帧：丢弃内容。done 说明该会话的生成已在服务端
      // 收尾且完整落库，清理其暂存——返回该会话时会加载到完整答案，
      // 不会丢半段也不会重复。
      if (frame.type === 'done') _dropStash(String(frameSession))
      return
    }
    // 收到任何帧都说明服务端是活的 → 解除「首个响应超时」
    if (frame.type !== 'token_delta') _clearWatchdog()
    switch (frame.type) {
      case 'attachment_warning':
        // P1-1：服务端发现附件不属于本会话（或已失效）并拒绝采纳。
        // 这里只做提示，不影响流式状态——消息本身仍在正常生成。
        attachmentWarning.value = (frame.data.message as string) || '附件未随本次提问送达'
        break
      case 'token_delta':
        _clearWatchdog()
        if (frame.data.reasoning_content) {
          const delta = frame.data.reasoning_content as string
          // 思维链内容：拼接到同一个字符串 + 过程框步骤
          streamingReasoning.value += delta
          _appendReasoning(delta)
        }
        if (frame.data.delta) {
          // 第一次收到正文 delta 时，标记思维链完成
          if (streamingReasoning.value && !reasoningDone.value) {
            reasoningDone.value = true
          }
          streamingContent.value += frame.data.delta as string
        }
        break
      case 'tool_event':
        toolEvents.value.push(frame.data)
        // 过程框记录（含把本轮中间正文移交进过程）
        _pushToolEvent(frame.data)
        // 检测 theme_switcher 工具的主题切换指令
        _handleThemeSwitch(frame.data)
        break
      case 'context_snapshot': {
        // 快照帧也可能来自非当前会话（切换后迟到的 final snapshot），
        // 带上了 session_id：不一致则忽略，并剥离开销键后再存。
        const sid = (frame.data as { session_id?: unknown } | undefined)?.session_id
        if (sid && String(sid) !== currentSessionId.value) break
        const data = { ...(frame.data as Record<string, unknown>) }
        delete data.session_id
        contextSnapshot.value = data
        break
      }
      case 'error':
        _clearWatchdog()
        error.value = (frame.data.message as string) || '未知错误'
        isStreaming.value = false
        regeneratingParentId.value = null
        // 生成出错收尾：清理暂存，避免返回该会话时恢复出幽灵流式态
        _dropStash(streamingSessionId.value)
        streamingSessionId.value = null
        break
      case 'done':
        _clearWatchdog()
        regeneratingParentId.value = null
        // done 属于当前会话（上面已过滤掉跨会话帧）：流式结束，
        // 清理暂存——内容已由后端持久化，reload 后会以真实消息展示。
        const doneStopped = (frame.data as { stopped?: unknown } | undefined)?.stopped === true
        _dropStash(String(frameSession || currentSessionId.value || ''))
        streamingSessionId.value = null
        // 标记思维链完成
        if (streamingReasoning.value) {
          reasoningDone.value = true
        }
        // 后端返回的本次问答 token 用量（DeepSeek usage 字段）
        const usage = (frame.data.usage as TokenUsage | undefined) || undefined
        if (streamingContent.value || streamingReasoning.value) {
          messages.value.push({
            id: Date.now().toString(),
            session_id: currentSessionId.value || '',
            role: 'assistant',
            content: streamingContent.value,
            tokens: usage?.total_tokens ?? 0,
            usage,
            // 用户停止生成：后端已把部分内容落库并带 stopped 标记，
            // 乐观消息同步该标记，reload 后由持久化消息接管。
            stopped: doneStopped,
            created_at: new Date().toISOString(),
          })
        }
        streamingContent.value = ''
        streamingReasoning.value = ''
        reasoningDone.value = false
        toolEvents.value = []
        // 过程框步骤清空：流式区块隐藏，执行过程改由（reload 后的）
        // 历史消息归并展示，避免双份过程框
        processEvents.value = []
        isStreaming.value = false
        // 刷新会话列表以获取后端 AI 生成的标题
        _refreshSessionTitle()
        // 关键修复：用后端已持久化的真实消息（含真实 UUID）对账本地乐观消息。
        // 否则刚收到的问答其 id 仍是 Date.now() 临时值，点「从此处分叉 / 删除本轮」
        // 会把临时 id 传给后端，导致 FORK_FAILED「会话或消息不存在」。
        // 同时让流式结束后的视图与「重新选择会话」加载的视图一致（含 tool 消息）。
        void reloadMessages().catch(() => {
          // 对账失败时保留乐观消息，不阻断界面
        })
        break
    }
  }

  // 刷新当前会话标题（后端 AI 生成后更新到侧边栏）
  async function _refreshSessionTitle() {
    if (!currentSessionId.value) return
    try {
      const session = await apiClient.get<Session>(`/sessions/${currentSessionId.value}`)
      const idx = sessions.value.findIndex((s) => s.id === session.id)
      if (idx >= 0) {
        sessions.value[idx].title = session.title
      }
    } catch {
      // 忽略
    }
  }

  // 生成模拟回复文本
  function _generateMockReply(userContent: string): string {
    const replies = [
      `收到你的提问：「${userContent}」\n\n这是**模拟模式**下的回复。当前未配置 API Key，因此使用本地模拟代替真实模型。\n\n## 模拟模式说明\n\n- 此回复为前端本地生成，非真实模型输出\n- 流式效果模拟了真实模型的逐 token 输出\n- 关闭模拟模式后将恢复正常的 WebSocket 调用\n\n如果你需要真实对话，请在设置页面配置对应 Provider 的 API Key。`,
      `你说了：「${userContent}」\n\n我是 **andy-harness** 的模拟回复引擎。由于当前未配置 API Key，无法调用真实模型。\n\n### 你可以：\n1. 在 **设置 → 插件管理** 中配置 Provider 的 api_key\n2. 继续使用模拟模式进行界面测试\n3. 关闭模拟模式后尝试真实对话\n\n> 模拟模式仅用于开发和测试，不会产生真实费用。`,
      `好的，我收到了你的消息：「${userContent}」\n\n这是模拟回复。当前处于 **模拟模式**，无需 API Key 即可体验 andy-harness 的对话界面。\n\n---\n\n模拟模式下的回复是预设的，不会根据上下文做复杂推理。要获得真实的 AI 回复，请配置 API Key 后关闭模拟模式。`,
    ]
    return replies[Math.floor(Math.random() * replies.length)]
  }

  // 模拟流式回复
  function _runMockStream(userContent: string) {
    const fullReply = _generateMockReply(userContent)
    const tokens = fullReply.split(/(\s+)/) // 按空白拆分，保留空格
    let idx = 0

    // 模拟思维链
    const reasoning = '让我分析一下用户的问题，生成一个合适的模拟回复…'
    const reasoningTokens = reasoning.split('')
    let rIdx = 0

    // 先流式输出思维链
    mockTimer = setTimeout(function emitReasoning() {
      if (rIdx >= reasoningTokens.length) {
        reasoningDone.value = true
        // 开始输出正文
        mockTimer = setTimeout(function emitToken() {
          if (idx >= tokens.length) {
            // 完成
            messages.value.push({
              id: Date.now().toString(),
              session_id: currentSessionId.value || '',
              role: 'assistant',
              content: streamingContent.value,
              tokens: 0,
              created_at: new Date().toISOString(),
            })
            streamingContent.value = ''
            streamingReasoning.value = ''
            reasoningDone.value = false
            processEvents.value = []
            isStreaming.value = false
            streamingSessionId.value = null
            return
          }
          streamingContent.value += tokens[idx]
          idx++
          mockTimer = setTimeout(emitToken, 30 + Math.random() * 40)
        }, 300)
        return
      }
      const rDelta = reasoningTokens[rIdx]
      streamingReasoning.value += rDelta
      _appendReasoning(rDelta)
      rIdx++
      mockTimer = setTimeout(emitReasoning, 20 + Math.random() * 30)
    }, 500)
  }

  async function sendMessage(
    content: string,
    providerId: string,
    model?: string,
    attachments?: Attachment[],
  ) {
    const atts = attachments && attachments.length ? attachments : []
    // 无会话时自动创建一条新会话
    if (!currentSessionId.value) {
      const title = content.slice(0, 20) || (atts[0] ? atts[0].filename : 'New Session')
      await createSession(title)
    }
    // 发送新消息：清空上一轮的停止占位提示（新一轮回答会另行呈现）
    stoppedPlaceholder.value = null
    stoppedPlaceholderUserMsgId.value = null
    const sessionId = currentSessionId.value
    if (!sessionId) return // 会话创建失败时终止
    // 既无文本也无附件时不发送
    if (!content.trim() && atts.length === 0) return

    // 标记本次流式归属：用于丢弃切会话后迟到的旧会话帧
    streamingSessionId.value = sessionId

    isStreaming.value = true
    error.value = null
    streamingContent.value = ''
    streamingReasoning.value = ''
    reasoningDone.value = false
    toolEvents.value = []
    processEvents.value = []
    attachmentWarning.value = ''

    const settingsStore = useSettingsStore()
    const settings = settingsStore.sessionSettings

    // P0-2：先把「这条消息能否真的发出去」确认清楚，再落地乐观状态。
    // 旧实现先 push 乐观消息并把 isStreaming 置 true，最后才调用 ws.send；
    // 而 ws.send 在连接不可用时是静默丢弃的，于是界面被永久锁死。
    const payload = {
      session_id: sessionId,
      content,
      provider_id: providerId,
      model: model || settings.model || undefined,
      temperature: settings.temperature,
      system_prompt: settings.system_prompt || undefined,
      attachments: atts.map((a) => ({ id: a.id })),
    }
    if (!mockMode.value && !apiClient.ws.send(payload)) {
      error.value = '未连接到服务，消息未发送。请等待右下角连接状态恢复后重试。'
      // 必须撤销乐观状态：isStreaming 在上面已被置 true，若这里直接 return，
      // 服务端根本没收到请求、不会回 done/error，输入框与发送按钮将永久禁用。
      // 同时清空流式过程中累积的中间态，避免下次发送时混入上一轮的残留。
      isStreaming.value = false
      streamingSessionId.value = null
      streamingContent.value = ''
      streamingReasoning.value = ''
      reasoningDone.value = false
      toolEvents.value = []
      processEvents.value = []
      return
    }
    // 已在传输中（或在握手队列里）→ 启动看门狗兜底
    _armWatchdog()

    messages.value.push({
      id: Date.now().toString(),
      session_id: sessionId,
      role: 'user',
      content,
      attachments: atts.length ? atts : undefined,
      tokens: 0,
      created_at: new Date().toISOString(),
    })

    if (mockMode.value) {
      _runMockStream(content)
      // 模拟模式下也清空待发送附件（仅前端展示用，无真实模型消费）
      pendingAttachments.value = []
      return
    }

    // 发送后清空待发送附件
    pendingAttachments.value = []
  }

  /**
   * 重新回答：保留该提问下的全部历史答案版本，仅针对同一提问再生成一条新答案。
   * 不删除任何旧问答对；后端据此新建一条 parent_id = userMsgId 的 assistant 答案。
   */
  async function regenerateAnswer(userMsgId: string, content: string, providerId: string) {
    if (!currentSessionId.value) return
    if (!userMsgId) return
    if (isStreaming.value) return

    const sessionId = currentSessionId.value
    streamingSessionId.value = sessionId
    isStreaming.value = true
    error.value = null
    streamingContent.value = ''
    streamingReasoning.value = ''
    reasoningDone.value = false
    toolEvents.value = []
    processEvents.value = []
    attachmentWarning.value = ''
    regeneratingParentId.value = userMsgId
    // 重新回答：清空上一轮的停止占位提示
    stoppedPlaceholder.value = null
    stoppedPlaceholderUserMsgId.value = null

    const settingsStore = useSettingsStore()
    const settings = settingsStore.sessionSettings

    const payload = {
      session_id: sessionId,
      content,
      provider_id: providerId || 'deepseek',
      model: settings.model || undefined,
      temperature: settings.temperature,
      system_prompt: settings.system_prompt || undefined,
      attachments: [],
      // 关键：指向被重新生成的 user 消息，后端据此复用提问、仅新增一条答案版本
      parent_message_id: userMsgId,
    }

    if (!mockMode.value && !apiClient.ws.send(payload)) {
      error.value = '未连接到服务，消息未发送。请等待右下角连接状态恢复后重试。'
      regeneratingParentId.value = null
      streamingSessionId.value = null
      isStreaming.value = false
      return
    }
    _armWatchdog()

    if (mockMode.value) {
      _runMockRegenerate(content, userMsgId)
      pendingAttachments.value = []
      return
    }
  }

  /** 模拟模式下「重新回答」：本地直接生成一条新答案版本（parent_id = userMsgId）。 */
  function _runMockRegenerate(userContent: string, userMsgId: string) {
    const fullReply = _generateMockReply(userContent)
    const tokens = fullReply.split(/(\s+)/)
    let idx = 0
    mockTimer = setTimeout(function emitToken() {
      if (idx >= tokens.length) {
        messages.value.push({
          id: Date.now().toString(),
          session_id: currentSessionId.value || '',
          role: 'assistant',
          content: streamingContent.value,
          tokens: 0,
          parent_id: userMsgId,
          created_at: new Date().toISOString(),
        })
        streamingContent.value = ''
        streamingReasoning.value = ''
        reasoningDone.value = false
        processEvents.value = []
        isStreaming.value = false
        regeneratingParentId.value = null
        streamingSessionId.value = null
        return
      }
      streamingContent.value += tokens[idx]
      idx++
      mockTimer = setTimeout(emitToken, 30 + Math.random() * 40)
    }, 300)
  }

  /** 上传若干文件为待发送附件（已在后端完成类型/大小/数量校验）。 */
  async function uploadPending(files: File[]) {
    if (!currentSessionId.value) {
      // 尚未建会话时，先建一条，避免 404
      await createSession('New Session')
    }
    const sessionId = currentSessionId.value
    if (!sessionId) return
    uploading.value = true
    try {
      const res = await apiClient.uploadAttachments<{ attachments: Attachment[] }>(
        sessionId,
        files,
      )
      const got = res.attachments || []
      // 合并，避免重复 id
      const seen = new Set(pendingAttachments.value.map((a) => a.id))
      for (const a of got) {
        if (!seen.has(a.id)) pendingAttachments.value.push(a)
      }
    } finally {
      uploading.value = false
    }
  }

    /** 移除一条待发送附件（仅前端移除，后端已落盘，后续会话删除会一并清理）。 */
    function removePendingAttachment(id: string) {
      pendingAttachments.value = pendingAttachments.value.filter((a) => a.id !== id)
    }

    /**
     * 清空待发送附件（附件已被消费时调用，如生成类模型发送后）。
     * 与对话路径一致：发送即从输入框消失，避免「已发送但附件还在」的困惑。
     */
    function clearPendingAttachments() {
      pendingAttachments.value = []
    }

  function stopStreaming() {
    _clearWatchdog()
    // 模拟模式：清除定时器
    if (mockTimer) {
      clearTimeout(mockTimer)
      mockTimer = null
    }
    // S24: Send stop control message to server before stopping locally
    if (!mockMode.value) {
      apiClient.ws.send({ type: 'stop', session_id: currentSessionId.value })
    }
    // 流式归属会话：被停止的是它的生成。可能正是当前会话，也可能是用户
    // 切到别的会话后点停止（后端按连接维度停止当前 loop，归属以帧为准）。
    const stoppedSession = streamingSessionId.value || currentSessionId.value
    const isCurrentStream =
      !streamingSessionId.value || streamingSessionId.value === currentSessionId.value
    isStreaming.value = false
    streamingSessionId.value = null
    regeneratingParentId.value = null
    if (isCurrentStream) {
      if (streamingContent.value) {
        // 已生成正文：作为 stopped 消息进入列表，由 MessageItem 在回答下方标注
        // 「用户已停止回答」。后端会落库（stopped=1），reload 后由真实消息接管。
        messages.value.push({
          id: Date.now().toString(),
          session_id: currentSessionId.value || '',
          role: 'assistant',
          content: streamingContent.value,
          reasoning: streamingReasoning.value || undefined,
          tokens: 0,
          stopped: true,
          created_at: new Date().toISOString(),
        })
      } else if (streamingReasoning.value) {
        // 仅处于「思考过程」、尚未输出答案正文时被中断：后端不会落库 stopped 消息
        // （agent_loop 仅在 result.content 非空时持久化），但用户应当看到明确的
        // 「用户已停止回答」提示。用独立占位消息承载，与 reloadMessages 的整表替换
        // 解耦，避免 400ms 后提示被抹掉。
        const lastUser = [...messages.value].reverse().find((m) => m.role === 'user')
        stoppedPlaceholderUserMsgId.value = lastUser?.id ?? null
        stoppedPlaceholder.value = {
          id: 'stopped-placeholder-' + Date.now().toString(),
          session_id: currentSessionId.value || '',
          role: 'assistant',
          content: '',
          reasoning: streamingReasoning.value,
          tokens: 0,
          stopped: true,
          created_at: new Date().toISOString(),
        }
      }
      _clearStreamLive()
      // 后端已把部分内容落库（stopped=1）。延迟片刻，等其处理完 stop
      // 指令后拉取真实状态对账（临时 id → 真实 UUID），stopped 标记随之保留。
      if (!mockMode.value) {
        setTimeout(() => {
          void reloadMessages().catch(() => {})
        }, 400)
      }
    } else {
      // 停止的是其它会话的生成：其部分内容已由后端持久化，清暂存即可；
      // 返回该会话时从服务端加载（含 stopped 标记），内容不丢失。
      _clearStreamLive()
      _dropStash(stoppedSession)
    }
  }

  async function deleteSession(sessionId: string) {
    await apiClient.delete(`/sessions/${sessionId}`)
    sessions.value = sessions.value.filter((s) => s.id !== sessionId)
    if (currentSessionId.value === sessionId) {
      const nextActive = sessions.value.find((s) => !s.archived)
      if (nextActive) {
        await selectSession(nextActive.id)
      } else {
        currentSessionId.value = null
        messages.value = []
        contextSnapshot.value = null
      }
    }
  }

  /** 重新拉取当前会话消息（不做快照刷新）。 */
  // 代数计数器：慢网络下若期间切换会话或再次触发 reload，过期的响应
  // 不应覆盖已流式完成的新消息（FE-01）。
  const reloadNonce = ref(0)
  async function reloadMessages() {
    if (!currentSessionId.value) return
    const sid = currentSessionId.value
    const myNonce = ++reloadNonce.value
    const data = await apiClient.get<Message[]>(`/sessions/${sid}/messages`)
    // 竞态判定：期间已切换会话或发起更新的 reload → 丢弃过期结果
    if (myNonce !== reloadNonce.value || currentSessionId.value !== sid) return
    messages.value = data
    // 若服务端已为该轮返回「已停止 / 已回答」的 assistant 消息（权威来源），
    // 则丢弃前端独立占位提示，避免重复展示；思考阶段中断、后端无落库时保留占位。
    const uid = stoppedPlaceholderUserMsgId.value
    if (
      uid &&
      data.some(
        (m) =>
          m.role === 'assistant' &&
          (m.stopped || (m.content || '').trim() !== '') &&
          m.parent_id === uid,
      )
    ) {
      stoppedPlaceholder.value = null
      stoppedPlaceholderUserMsgId.value = null
    }
  }

  /** 物理删除某条 user 消息开始的整轮问答，返回删除条数。 */
  async function deleteTurn(messageId: string): Promise<number> {
    if (!currentSessionId.value) return 0
    const res = await apiClient.delete<{ removed: number }>(
      `/sessions/${currentSessionId.value}/messages/${messageId}?with_turn=true`,
    )
    await reloadMessages()
    return res?.removed ?? 0
  }

  /**
   * 分叉会话：从 `atMessageId`（默认从头）复制历史为新会话并切换过去。
   */
  async function forkSession(
    sessionId: string,
    atMessageId?: string | null,
    title?: string,
  ) {
    void title
    const forked = await apiClient.post<Session>(`/sessions/${sessionId}/fork`, {
      at_message_id: atMessageId ?? null,
    })
    sessions.value = [forked, ...sessions.value]
    await selectSession(forked.id)
    return forked
  }

  /** 导出的会话包导入为新会话并切换过去。 */
  async function importSession(payload: unknown, title?: string) {
    const session = await apiClient.post<Session>('/sessions/import', {
      payload,
      title,
    })
    sessions.value = [session, ...sessions.value]
    await selectSession(session.id)
    return session
  }

  return {
    sessions,
    currentSessionId,
    messages,
    streamingContent,
    streamingReasoning,
    reasoningDone,
    toolEvents,
    isStreaming,
    contextSnapshot,
    error,
    mockMode,
    regeneratingParentId,
    streamingSessionId,
    stoppedPlaceholder,
    stoppedPlaceholderUserMsgId,
    pendingAutoSend,
    pendingAutoSendSession,
    pendingAttachments,
    uploading,
    attachmentWarning,
    processEvents,
    currentSession,
    loadSessions,
    createSession,
    selectSession,
    renameSession,
    archiveSession,
    deleteSession,
    reloadMessages,
    deleteTurn,
    regenerateAnswer,
    forkSession,
    importSession,
    handleWSFrame,
    sendMessage,
    stopStreaming,
    uploadPending,
    removePendingAttachment,
    clearPendingAttachments,
  }
})
