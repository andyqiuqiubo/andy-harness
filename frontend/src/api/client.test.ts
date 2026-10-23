/**
 * E12：HTTP Authorization 头与 WS token 参数注入测试。
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { clearToken, setToken } from './token'

function jsonResponse(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    json: async () => body,
  } as Response
}

describe('client auth wiring（E12）', () => {
  beforeEach(() => {
    vi.resetModules()
    clearToken()
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    clearToken()
  })

  it('attaches Bearer header when token exists', async () => {
    setToken('tok.http')
    const fetchMock = vi.fn(
      async (_url: string, _init?: RequestInit): Promise<Response> =>
        jsonResponse({}),
    )
    vi.stubGlobal('fetch', fetchMock)

    const { apiClient } = await import('./client')
    await apiClient.get('/x')

    const headers = fetchMock.mock.calls[0]?.[1]?.headers as Headers
    expect(headers.get('Authorization')).toBe('Bearer tok.http')
  })

  it('does not attach Authorization when no token', async () => {
    const fetchMock = vi.fn(
      async (_url: string, _init?: RequestInit): Promise<Response> =>
        jsonResponse({}),
    )
    vi.stubGlobal('fetch', fetchMock)

    const { apiClient } = await import('./client')
    await apiClient.get('/x')

    const init = fetchMock.mock.calls[0]?.[1]
    const auth = init?.headers
      ? new Headers(init.headers).get('Authorization')
      : null
    expect(auth).toBeNull()
  })

  it('ws connect url contains token query param', async () => {
    setToken('tok.ws')
    const urls: string[] = []
    class MockWebSocket {
      static readonly OPEN = 1
      static readonly CONNECTING = 0
      onopen: (() => void) | null = null
      onclose: ((e: CloseEvent) => void) | null = null
      onmessage: ((e: MessageEvent) => void) | null = null
      onerror: (() => void) | null = null
      readyState = 0

      constructor(url: string) {
        urls.push(url)
      }

      close() {}
    }
    vi.stubGlobal('WebSocket', MockWebSocket)

    const { apiClient } = await import('./client')
    apiClient.ws.connect()
    expect(urls[0]).toContain('?token=tok.ws')
    apiClient.ws.disconnect()
  })
})

/**
 * 回归：WS 重连状态机（防「重连中」风暴）。
 *
 * 用户实测现象：没做任何设置/操作，会话界面时不时显示「重连中」。
 *
 * 根因：`WebSocket.close()` 是**异步**的，其 `onclose` 稍后才派发。旧实现在
 * `connect()` 里关闭旧连接后**同步**把 `shouldReconnect` 改回 true，于是旧连接
 * 迟到的 `onclose` 又排一次重连 → 1s 后把刚建好的健康连接关掉 → 新连接的
 * `onclose` 再排一次……形成永不停止的重连风暴；且每次 `onopen` 都会把
 * `reconnectAttempts` 归零，`maxReconnects` 永远触达不到，界面就一直闪「重连中」。
 */
describe('WS 重连状态机（防重连风暴）', () => {
  /** 可控 WebSocket 替身：由测试决定何时握手成功 / 何时断开。 */
  class ControlledWebSocket {
    static instances: ControlledWebSocket[] = []
    // client.ts 的 send() 会引用这两个静态常量
    static readonly CONNECTING = 0
    static readonly OPEN = 1
    static readonly CLOSING = 2
    static readonly CLOSED = 3

    url: string
    readyState = ControlledWebSocket.CONNECTING
    sent: string[] = []
    onopen: (() => void) | null = null
    onclose: ((e: CloseEvent) => void) | null = null
    onmessage: ((e: MessageEvent) => void) | null = null
    onerror: ((e: Event) => void) | null = null

    constructor(url: string) {
      this.url = url
      ControlledWebSocket.instances.push(this)
    }

    send(data: string) {
      this.sent.push(data)
    }

    close() {
      this.readyState = ControlledWebSocket.CLOSED
    }

    /** 测试助手：模拟握手成功。 */
    handshakeOk() {
      this.readyState = ControlledWebSocket.OPEN
      this.onopen?.()
    }

    /** 测试助手：模拟断开（浏览器派发 onclose）。 */
    fireClose(code = 1006) {
      this.readyState = ControlledWebSocket.CLOSED
      this.onclose?.({ code } as CloseEvent)
    }
  }

  beforeEach(() => {
    vi.resetModules()
    clearToken()
    ControlledWebSocket.instances = []
    vi.stubGlobal('WebSocket', ControlledWebSocket)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    vi.useRealTimers()
  })

  it('离开会话页立即再进入：旧连接迟到的 onclose 不应引发额外重连', async () => {
    vi.useFakeTimers()
    const { apiClient, wsConnectionStatus } = await import('./client')

    apiClient.ws.connect()
    const s1 = ControlledWebSocket.instances[0]!
    s1.handshakeOk()
    expect(wsConnectionStatus.value).toBe('connected')

    // 用户离开会话页 → 立刻再进入（ChatView 卸载 + 重挂载）
    apiClient.ws.disconnect()
    apiClient.ws.connect()
    const s2 = ControlledWebSocket.instances[1]!
    s2.handshakeOk()
    expect(wsConnectionStatus.value).toBe('connected')

    // 旧连接 S1 的 onclose 迟到（真实浏览器里 close 是异步的）
    s1.fireClose(1006)

    // 修复前：状态被改成 reconnecting，且 1s 后把健康连接 s2 关掉，开始无限重连
    expect(wsConnectionStatus.value).toBe('connected')
    await vi.advanceTimersByTimeAsync(120_000)
    expect(ControlledWebSocket.instances.length).toBe(2)

    apiClient.ws.disconnect()
  })

  it('真实掉线仍会重连一次；重连成功后不再空转新建连接', async () => {
    vi.useFakeTimers()
    const { apiClient, wsConnectionStatus } = await import('./client')

    apiClient.ws.connect()
    const s1 = ControlledWebSocket.instances[0]!
    s1.handshakeOk()

    // 服务端真实断开（如后端重启 / 网络抖动）
    s1.fireClose(1006)
    expect(wsConnectionStatus.value).toBe('reconnecting')

    await vi.advanceTimersByTimeAsync(1100)
    expect(ControlledWebSocket.instances.length).toBe(2)
    const s2 = ControlledWebSocket.instances[1]!
    s2.handshakeOk()
    expect(wsConnectionStatus.value).toBe('connected')

    // 再等很久也不应出现多余连接（修复前这里会一直重连）
    await vi.advanceTimersByTimeAsync(120_000)
    expect(ControlledWebSocket.instances.length).toBe(2)

    apiClient.ws.disconnect()
  })
})

describe('fetchAttachmentDataUri（图生视频 Base64 通道）', () => {
  beforeEach(() => {
    vi.resetModules()
    clearToken()
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    clearToken()
  })

  it('把附件字节转成带 mime 的 data URI', async () => {
    const bytes = new Uint8Array([0xff, 0xd8, 0xff, 0xe0, 1, 2, 3])
    const fetchMock = vi.fn(
      async (_url: string | URL | Request, _init?: RequestInit): Promise<Response> =>
        ({
          ok: true,
          status: 200,
          arrayBuffer: async () => bytes.buffer,
        }) as Response,
    )
    vi.stubGlobal('fetch', fetchMock)

    const { fetchAttachmentDataUri } = await import('./client')
    const uri = await fetchAttachmentDataUri('sess1', 'att1', 'image/jpeg')

    expect(uri.startsWith('data:image/jpeg;base64,')).toBe(true)
    // 与 btoa 手工编码结果逐字节一致
    const expected = btoa(Array.from(bytes, (b) => String.fromCharCode(b)).join(''))
    expect(uri).toBe(`data:image/jpeg;base64,${expected}`)
    expect(fetchMock.mock.calls[0]?.[0]).toContain('/sessions/sess1/attachments/att1')
  })

  it('mime 缺失时回退 image/png', async () => {
    const bytes = new Uint8Array([1, 2, 3])
    vi.stubGlobal(
      'fetch',
      vi.fn(
        async (): Promise<Response> =>
          ({ ok: true, status: 200, arrayBuffer: async () => bytes.buffer }) as Response,
      ),
    )
    const { fetchAttachmentDataUri } = await import('./client')
    const uri = await fetchAttachmentDataUri('s', 'a', '')
    expect(uri.startsWith('data:image/png;base64,')).toBe(true)
  })

  it('分块拼接可正确编码超 32KB 的图片（不触 apply 爆栈）', async () => {
    const bytes = new Uint8Array(40_000)
    for (let i = 0; i < bytes.length; i++) bytes[i] = i % 256
    vi.stubGlobal(
      'fetch',
      vi.fn(
        async (): Promise<Response> =>
          ({ ok: true, status: 200, arrayBuffer: async () => bytes.buffer }) as Response,
      ),
    )
    const { fetchAttachmentDataUri } = await import('./client')
    const uri = await fetchAttachmentDataUri('s', 'a', 'image/png')
    const expected = btoa(Array.from(bytes, (b) => String.fromCharCode(b)).join(''))
    expect(uri).toBe(`data:image/png;base64,${expected}`)
  })

  it('下载失败（非 2xx）抛出错误', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(
        async (): Promise<Response> => ({ ok: false, status: 404 }) as Response,
      ),
    )
    const { fetchAttachmentDataUri } = await import('./client')
    await expect(fetchAttachmentDataUri('s', 'missing', 'image/png')).rejects.toThrow('HTTP 404')
  })
})
