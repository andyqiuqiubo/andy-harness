/**
 * 回归：会话流式状态的三个缺陷修复。
 *
 * 1) 停止生成后，已获取的回答内容必须完整保留（stopped 标记），不得消失；
 * 2) 生成途中点「新建」再返回原会话，已生成的回答文字必须完整可见；
 * 3) 跨会话迟到帧（token_delta / done）不得串写进当前会话。
 *
 * 另有模拟模式（无 API Key）下的等价保障：切走即把已生成内容作为
 * 「已停止」回答暂存，返回时落入消息列表。
 */

import { flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { Message } from '../api/types'
import { useChatStore } from './chat'

/** jsdom 无真实 WS：占位即可（store 的 ws.send 只读 readyState，未连接时静默返回 false）。 */
class NoopWebSocket {
  static readonly CONNECTING = 0
  static readonly OPEN = 1
  static readonly CLOSING = 2
  static readonly CLOSED = 3
  readyState = NoopWebSocket.CONNECTING
  url: string
  onopen: (() => void) | null = null
  onclose: (() => void) | null = null
  onerror: (() => void) | null = null
  onmessage: (() => void) | null = null
  constructor(url: string) {
    this.url = url
  }
  send() {}
  close() {}
}

/** 服务端「已持久化」的消息（按会话模拟，验证前端对账行为）。 */
const serverMessages: Record<string, Message[]> = {}
let sessionSeq = 0

function serverSession(id: string) {
  return {
    id,
    title: 'New Session',
    config: {},
    created_at: '2026-10-12 10:00:00',
    updated_at: '2026-10-12 10:00:00',
    archived: false,
    message_count: 0,
  }
}

function installFetchMock() {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      const method = (init?.method || 'GET').toUpperCase()
      if (method === 'POST' && url.endsWith('/api/sessions')) {
        sessionSeq += 1
        return { ok: true, status: 200, json: async () => serverSession(`sess-${sessionSeq}`) } as Response
      }
      const msgMatch = url.match(/\/api\/sessions\/([^/]+)\/messages/)
      if (msgMatch) {
        return {
          ok: true,
          status: 200,
          json: async () => serverMessages[msgMatch[1]] ?? [],
        } as Response
      }
      if (url.includes('/context-snapshot')) {
        return { ok: true, status: 200, json: async () => null } as Response
      }
      // 会话列表 / 会话详情 / 其他：空
      return { ok: true, status: 200, json: async () => [] } as Response
    }),
  )
}

/** 造一条服务端已持久化的 assistant 消息。 */
function persisted(sessionId: string, content: string, stopped = false): Message {
  return {
    id: `real-${sessionId}-${content.slice(0, 4)}`,
    session_id: sessionId,
    role: 'assistant',
    content,
    tokens: 0,
    created_at: '2026-10-12 10:01:00',
    stopped,
  }
}

describe('chat store：停止生成保留内容（缺陷 2）', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    installFetchMock()
    vi.stubGlobal('WebSocket', NoopWebSocket)
    vi.useFakeTimers()
  })

  afterEach(() => {
    vi.useRealTimers()
    vi.unstubAllGlobals()
  })

  it('停止后已生成内容完整保留，并带 stopped 标记', async () => {
    const store = useChatStore()
    await store.createSession('A')
    // 模拟「生成中」：归属 A，已有部分正文
    store.streamingSessionId = store.currentSessionId
    store.isStreaming = true
    store.streamingContent = '已生成的部分回答内容，必须保留'

    store.stopStreaming()
    await flushPromises()

    expect(store.messages).toHaveLength(1)
    expect(store.messages[0].role).toBe('assistant')
    expect(store.messages[0].content).toBe('已生成的部分回答内容，必须保留')
    expect(store.messages[0].stopped).toBe(true)
    // 实时态被清空（避免与新消息混排），输入框恢复可用
    expect(store.streamingContent).toBe('')
    expect(store.isStreaming).toBe(false)
    expect(store.streamingSessionId).toBeNull()
  })

  it('停止的不是当前会话时，内容不串入当前会话（由服务端持久化保证）', async () => {
    const store = useChatStore()
    await store.createSession('A')
    store.streamingSessionId = store.currentSessionId
    store.isStreaming = true
    store.streamingContent = 'A 的部分答案'

    // 切到 B 后从 B 的界面停止（安全路径）：B 不得出现 A 的内容
    await store.createSession('B')
    store.stopStreaming()
    await flushPromises()

    expect(store.messages).toHaveLength(0)
    expect(store.streamingContent).toBe('')

    // 返回 A：服务端已把部分答案落库（stopped=true）
    const aId = 'sess-1'
    serverMessages[aId] = [persisted(aId, 'A 的部分答案', true)]
    await store.selectSession(aId)
    expect(store.messages).toHaveLength(1)
    expect(store.messages[0].content).toBe('A 的部分答案')
    expect(store.messages[0].stopped).toBe(true)
  })

  it('仅处于思考过程（无正文）被中断：用独立占位提示承载，不污染 messages', async () => {
    const store = useChatStore()
    await store.createSession('A')
    store.streamingSessionId = store.currentSessionId
    store.isStreaming = true
    store.streamingReasoning = '让我先分析一下用户的问题…'
    // 尚无正文
    expect(store.streamingContent).toBe('')

    store.stopStreaming()
    await flushPromises()

    // 不向 messages 写入 stopped 消息（后端不会落库此情形，否则会被 reload 整表替换抹掉）
    expect(store.messages).toHaveLength(0)
    // 独立占位消息承载「用户已停止回答」
    expect(store.stoppedPlaceholder).not.toBeNull()
    expect(store.stoppedPlaceholder?.stopped).toBe(true)
    expect(store.stoppedPlaceholder?.reasoning).toBe('让我先分析一下用户的问题…')
  })
})

describe('chat store：新建/切换会话不丢生成内容（缺陷 3）', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    installFetchMock()
    vi.stubGlobal('WebSocket', NoopWebSocket)
    vi.useFakeTimers()
  })

  afterEach(() => {
    vi.useRealTimers()
    vi.unstubAllGlobals()
  })

  it('生成途中新建会话：新会话无流式残留、输入可用；返回原会话内容完整恢复', async () => {
    const store = useChatStore()
    await store.createSession('A')
    const aId = store.currentSessionId as string
    store.streamingSessionId = aId
    store.isStreaming = true
    store.streamingContent = '第一段已生成内容'

    // 点「新建」→ 进入 B
    await store.createSession('B')
    const bId = store.currentSessionId as string
    expect(bId).not.toBe(aId)
    // 新会话不得显示旧会话的流式气泡，输入框不得被禁用
    expect(store.streamingContent).toBe('')
    expect(store.isStreaming).toBe(false)

    // A 的迟到帧不得写进 B
    store.handleWSFrame({ type: 'token_delta', data: { delta: '（A 的迟到帧）', session_id: aId } })
    expect(store.streamingContent).toBe('')
    expect(store.messages).toHaveLength(0)

    // 返回 A：已生成内容完整恢复，流式态继续（继续实时追加）
    await store.selectSession(aId)
    expect(store.isStreaming).toBe(true)
    expect(store.streamingContent).toBe('第一段已生成内容')

    // A 生成完成：完整答案进入列表，并与服务端对账
    serverMessages[aId] = [persisted(aId, '第一段已生成内容')]
    store.handleWSFrame({ type: 'done', data: { stopped: false, session_id: aId, usage: null } })
    await flushPromises()
    const assistants = store.messages.filter((m) => m.role === 'assistant')
    expect(assistants).toHaveLength(1)
    expect(assistants[0].content).toBe('第一段已生成内容')
    expect(store.isStreaming).toBe(false)
  })

  it('切到 B 后 A 的 done 帧不写进 B；返回 A 加载到完整答案', async () => {
    const store = useChatStore()
    await store.createSession('A')
    const aId = store.currentSessionId as string
    store.streamingSessionId = aId
    store.isStreaming = true
    store.streamingContent = 'A 的部分答案'
    await store.createSession('B')

    // A 在服务端完成并落库，done 帧此刻才到达（用户在 B）
    serverMessages[aId] = [persisted(aId, 'A 的完整答案')]
    store.handleWSFrame({ type: 'done', data: { stopped: false, session_id: aId, usage: null } })
    await flushPromises()
    // B 的消息列表不得混入 A 的内容
    expect(store.messages).toHaveLength(0)

    // 返回 A：从服务端加载到完整答案
    await store.selectSession(aId)
    expect(store.messages.map((m) => m.content)).toEqual(['A 的完整答案'])
    expect(store.isStreaming).toBe(false)
  })
})

describe('chat store：模拟模式切走保留已生成内容', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    installFetchMock()
    vi.stubGlobal('WebSocket', NoopWebSocket)
    vi.useFakeTimers()
  })

  afterEach(() => {
    vi.useRealTimers()
    vi.unstubAllGlobals()
  })

  it('模拟流式中途切走再返回，已生成内容作为已停止回答保留', async () => {
    const store = useChatStore()
    store.mockMode = true
    await store.createSession('A')
    const aId = store.currentSessionId as string
    await store.sendMessage('你好', 'deepseek')
    // 推进定时器：思维链 + 一部分正文
    await vi.advanceTimersByTimeAsync(2000)
    const partial = store.streamingContent
    expect(partial.length).toBeGreaterThan(0)

    // 切走（新建 B）→ 实时态清空
    await store.createSession('B')
    expect(store.streamingContent).toBe('')
    expect(store.isStreaming).toBe(false)

    // 返回 A：已生成内容作为 stopped 消息落入列表（内容不丢失）
    await store.selectSession(aId)
    const stopped = store.messages.filter((m) => m.role === 'assistant' && m.stopped)
    expect(stopped).toHaveLength(1)
    expect(stopped[0].content).toBe(partial)
  })
})
