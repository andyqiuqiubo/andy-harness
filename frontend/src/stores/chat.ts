import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import type { Session, Message, WSFrame } from '../api/types'
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
    return session
  }

  async function selectSession(sessionId: string) {
    currentSessionId.value = sessionId
    messages.value = await apiClient.get<Message[]>(`/sessions/${sessionId}/messages`)
    streamingContent.value = ''
    toolEvents.value = []
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
      currentSessionId.value = nextActive ? nextActive.id : null
      messages.value = []
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
          // 思维链内容：拼接到同一个字符串
          streamingReasoning.value += frame.data.reasoning_content as string
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
        if (streamingContent.value || streamingReasoning.value) {
          messages.value.push({
            id: Date.now().toString(),
            session_id: currentSessionId.value || '',
            role: 'assistant',
            content: streamingContent.value,
            tokens: 0,
            created_at: new Date().toISOString(),
          })
        }
        streamingContent.value = ''
        streamingReasoning.value = ''
        reasoningDone.value = false
        toolEvents.value = []
        isStreaming.value = false
        // 刷新会话列表以获取后端 AI 生成的标题
        _refreshSessionTitle()
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
            isStreaming.value = false
            return
          }
          streamingContent.value += tokens[idx]
          idx++
          mockTimer = setTimeout(emitToken, 30 + Math.random() * 40)
        }, 300)
        return
      }
      streamingReasoning.value += reasoningTokens[rIdx]
      rIdx++
      mockTimer = setTimeout(emitReasoning, 20 + Math.random() * 30)
    }, 500)
  }

  function sendMessage(content: string, providerId: string, model?: string) {
    if (!currentSessionId.value) return
    isStreaming.value = true
    error.value = null
    streamingContent.value = ''
    streamingReasoning.value = ''
    reasoningDone.value = false
    toolEvents.value = []

    messages.value.push({
      id: Date.now().toString(),
      session_id: currentSessionId.value,
      role: 'user',
      content,
      tokens: 0,
      created_at: new Date().toISOString(),
    })

    if (mockMode.value) {
      _runMockStream(content)
      return
    }

    // S26: Include temperature and system_prompt from session settings
    const settingsStore = useSettingsStore()
    const settings = settingsStore.sessionSettings

    apiClient.ws.send({
      session_id: currentSessionId.value,
      content,
      provider_id: providerId,
      model: model || settings.model || undefined,
      temperature: settings.temperature,
      system_prompt: settings.system_prompt || undefined,
    })
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
  }

  async function deleteSession(sessionId: string) {
    await apiClient.delete(`/sessions/${sessionId}`)
    sessions.value = sessions.value.filter((s) => s.id !== sessionId)
    if (currentSessionId.value === sessionId) {
      const nextActive = sessions.value.find((s) => !s.archived)
      currentSessionId.value = nextActive ? nextActive.id : null
      messages.value = []
    }
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
    currentSession,
    loadSessions,
    createSession,
    selectSession,
    renameSession,
    archiveSession,
    deleteSession,
    handleWSFrame,
    sendMessage,
    stopStreaming,
  }
})
