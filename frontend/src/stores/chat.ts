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
    sessions.value.unshift(session)
    currentSessionId.value = session.id
    messages.value = []
    contextSnapshot.value = null
    return session
  }

  async function selectSession(sessionId: string) {
    currentSessionId.value = sessionId
    // 并行加载消息与最新上下文快照（有快照则显示，无则保持 null 隐藏）
    const [msgs, snapshot] = await Promise.all([
      apiClient.get<Message[]>(`/sessions/${sessionId}/messages`),
      apiClient.get<Record<string, unknown> | null>(
        `/sessions/${sessionId}/context-snapshot`
      ),
    ])
    messages.value = msgs
    streamingContent.value = ''
    toolEvents.value = []
    contextSnapshot.value = snapshot || null
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
    switch (frame.type) {
      case 'token_delta':
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
      case 'context_snapshot':
        contextSnapshot.value = frame.data
        break
      case 'error':
        error.value = (frame.data.message as string) || '未知错误'
        isStreaming.value = false
        break
      case 'done':
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
    const sessionId = currentSessionId.value
    if (!sessionId) return // 会话创建失败时终止
    // 既无文本也无附件时不发送
    if (!content.trim() && atts.length === 0) return

    isStreaming.value = true
    error.value = null
    streamingContent.value = ''
    streamingReasoning.value = ''
    reasoningDone.value = false
    toolEvents.value = []
    processEvents.value = []

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

    // S26: Include temperature and system_prompt from session settings
    const settingsStore = useSettingsStore()
    const settings = settingsStore.sessionSettings

    apiClient.ws.send({
      session_id: sessionId,
      content,
      provider_id: providerId,
      model: model || settings.model || undefined,
      temperature: settings.temperature,
      system_prompt: settings.system_prompt || undefined,
      attachments: atts.map((a) => ({ id: a.id })),
    })
    // 发送后清空待发送附件
    pendingAttachments.value = []
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

  function stopStreaming() {
    // 模拟模式：清除定时器
    if (mockTimer) {
      clearTimeout(mockTimer)
      mockTimer = null
    }
    // S24: Send stop control message to server before stopping locally
    if (!mockMode.value) {
      apiClient.ws.send({ type: 'stop', session_id: currentSessionId.value })
    }
    isStreaming.value = false
    if (streamingContent.value || streamingReasoning.value) {
      messages.value.push({
        id: Date.now().toString(),
        session_id: currentSessionId.value || '',
        role: 'assistant',
        content: streamingContent.value + '\n[已中断]',
        tokens: 0,
        created_at: new Date().toISOString(),
      })
    }
    streamingContent.value = ''
    streamingReasoning.value = ''
    reasoningDone.value = false
    toolEvents.value = []
    // 与 'done' 同理：停止后过程框隐藏，由 reload 后的历史消息归并展示
    processEvents.value = []
    // 停止后用户消息已持久化，需用后端真实 UUID 对账，
    // 否则对该轮「删除本轮 / 分叉」会拿到临时 id 而失败。
    // 延迟片刻，等后端处理完 stop 指令与失败轮次清理后再拉取真实状态。
    if (!mockMode.value) {
      setTimeout(() => {
        void reloadMessages().catch(() => {})
      }, 400)
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
  async function reloadMessages() {
    if (!currentSessionId.value) return
    messages.value = await apiClient.get<Message[]>(
      `/sessions/${currentSessionId.value}/messages`,
    )
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
    pendingAttachments,
    uploading,
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
    forkSession,
    importSession,
    handleWSFrame,
    sendMessage,
    stopStreaming,
    uploadPending,
    removePendingAttachment,
  }
})
