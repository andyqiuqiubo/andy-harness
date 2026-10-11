import { ref } from 'vue'
import { currentOrigin } from './runtime'
import { clearToken, getToken } from './token'

/** 后端 API 前缀：Tauri 为绝对地址，浏览器为相对路径。 */
function apiBase(): string {
  return `${currentOrigin()}/api`
}

/**
 * E12：401 / WS 1008 统一回调（由 auth store 注册）。
 * 触发时清除本地 token 并把界面切回登录门；无回调时仅清 token。
 */
let unauthorizedHandler: (() => void) | null = null

export function onUnauthorized(handler: (() => void) | null): void {
  unauthorizedHandler = handler
}

function handleUnauthorized(): void {
  clearToken()
  if (unauthorizedHandler) unauthorizedHandler()
}

/** 合并认证头：存在 token 时附带 Bearer（不覆盖调用方已设值）。 */
function withAuthHeaders(init?: RequestInit): RequestInit {
  const token = getToken()
  if (!token) return init ?? {}
  const headers = new Headers(init?.headers)
  if (!headers.has('Authorization')) headers.set('Authorization', `Bearer ${token}`)
  return { ...init, headers }
}

/** WebSocket 地址：Tauri 直连后端绝对地址，浏览器同源；启用认证时附带 token。 */
function wsBase(): string {
  let base: string
  const origin = currentOrigin()
  if (origin) {
    const url = new URL(origin)
    base = `${url.protocol === 'https:' ? 'wss:' : 'ws:'}//${url.host}/ws/chat`
  } else {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
    base = `${protocol}//${window.location.host}/ws/chat`
  }
  const token = getToken()
  return token ? `${base}?token=${encodeURIComponent(token)}` : base
}

export type WSConnectionStatus = 'connected' | 'connecting' | 'disconnected' | 'reconnecting'

export const wsConnectionStatus = ref<WSConnectionStatus>('disconnected')

/** 统一的 HTTP 错误：保留状态码与业务 code，便于调用方做差异化提示。 */
export class ApiError extends Error {
  status: number
  code: string
  payload?: unknown

  constructor(status: number, code: string, message: string, payload?: unknown) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
    this.payload = payload
  }

  /** 网络层失败（fetch 本身 reject：断网 / DNS / CORS），此时 status 为 0。 */
  get isNetworkError(): boolean {
    return this.status === 0
  }
}

/**
 * P2-5：统一请求入口。
 *
 * 旧实现在每个方法里重复 `throw new Error(await res.text())`，存在三个问题：
 * - 丢掉 HTTP 状态码，调用方无法区分 404 / 400 / 503（附件"数量超限"与
 *   "类型不支持"被混成同一类提示）；
 * - 直接把后端原始响应体（可能是 JSON / HTML）拼进 alert，用户看到的是未处理的错误文本；
 * - fetch 自身的 reject（断网）与 HTTP 错误混为一谈。
 */
async function request<T>(url: string, init?: RequestInit): Promise<T> {
  let res: Response
  try {
    res = await fetch(`${apiBase()}${url}`, withAuthHeaders(init))
  } catch (e) {
    throw new ApiError(0, 'NETWORK_ERROR', '网络请求失败，请检查连接后重试', String(e))
  }

  if (!res.ok) {
    let body: unknown = null
    try {
      body = await res.json()
    } catch {
      body = null
    }
    const obj = (typeof body === 'object' && body !== null ? body : {}) as Record<string, unknown>
    const code = String(obj.code ?? obj.error ?? 'HTTP_ERROR')
    const rawMessage = obj.message ?? obj.detail
    const message =
      typeof rawMessage === 'string' && rawMessage.trim()
        ? rawMessage
        : `请求失败（HTTP ${res.status}）`
    // E12：401 表示未认证或 token 失效，清 token 并回到登录门。
    if (res.status === 401) handleUnauthorized()
    throw new ApiError(res.status, code, message, body)
  }

  try {
    return (await res.json()) as T
  } catch {
    // 少数端点可能返回空 body（如 204），退回空对象避免上层 JSON 解析报错
    return undefined as T
  }
}

/**
 * P0-3：鉴权模式下 `<img>` 不会附带 Bearer，直链会被后端 401 拦下、图片破图。
 * 这里用 fetch + blob: 渲染附件图片，认证头由 `withAuthHeaders` 统一注入；
 * 若请求失败（如鉴权未启用时的网络异常），回退到相对直链，保证单用户默认
 * 部署（E12 关闭）下行为与改动前完全一致。
 *
 * 返回 blob: URL，调用方应在组件卸载时 `URL.revokeObjectURL` 释放，避免内存泄漏。
 */
export async function fetchAttachmentBlobUrl(
  sessionId: string,
  attachmentId: string,
): Promise<string> {
  const directUrl = `/api/sessions/${sessionId}/attachments/${attachmentId}`
  try {
    const res = await fetch(`${apiBase()}/sessions/${sessionId}/attachments/${attachmentId}`, withAuthHeaders())
    if (!res.ok) throw new Error(`HTTP ${res.status}`)
    const blob = await res.blob()
    return URL.createObjectURL(blob)
  } catch {
    // 回退直链（E12 未启用时直链即可，<img> 原生加载）
    return directUrl
  }
}

class APIClientImpl {
  async get<T>(url: string): Promise<T> {
    return request<T>(url)
  }

  async post<T>(url: string, body?: unknown): Promise<T> {
    return request<T>(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: body ? JSON.stringify(body) : undefined,
    })
  }

  async put<T>(url: string, body?: unknown): Promise<T> {
    return request<T>(url, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: body ? JSON.stringify(body) : undefined,
    })
  }

  async patch<T>(url: string, body: unknown): Promise<T> {
    return request<T>(url, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    })
  }

  async delete<T>(url: string): Promise<T> {
    return request<T>(url, { method: 'DELETE' })
  }

  /** 上传附件（multipart），返回后端登记后的公开元信息列表。 */
  async uploadAttachments<T>(sessionId: string, files: File[]): Promise<T> {
    const form = new FormData()
    for (const f of files) form.append('files', f, f.name)
    // 注意：multipart 不设 Content-Type，让浏览器自带 boundary
    return request<T>(`/sessions/${sessionId}/attachments`, {
      method: 'POST',
      body: form,
    })
  }

  ws = {
    socket: null as WebSocket | null,
    reconnectTimer: null as ReturnType<typeof setTimeout> | null,
    shouldReconnect: true,
    messageHandlers: new Set<(data: unknown) => void>(),
    reconnectAttempts: 0,
    maxReconnects: 10,
    baseDelay: 1000,
    maxDelay: 30000,
    /** 握手完成前暂存的待发消息（见 send 的说明） */
    pendingQueue: [] as unknown[],

    _onMessage(e: MessageEvent) {
      try {
        const parsed = JSON.parse(e.data)
        this.messageHandlers.forEach((h) => h(parsed))
      } catch {
        this.messageHandlers.forEach((h) => h(e.data))
      }
    },

    /**
     * 关闭当前 socket 并**摘除其事件处理器**。
     *
     * ⚠️ 重连风暴根因（本次修复）：
     * `WebSocket.close()` 是**异步**的 —— 它的 `onclose` 会在稍后的事件循环里才触发。
     * 旧实现在关闭前把 `shouldReconnect` 置 false、紧接着又同步改回 true，等旧 socket
     * 的 `onclose` 真正执行时看到的已是 `true`，于是它又排一次重连；1s 后那次重连会把
     * **刚建立好的健康连接**关掉，新连接的 `onclose` 再排一次……如此往复。
     * 更糟的是每次 `onopen` 都会把 `reconnectAttempts` 归零，`maxReconnects` 永远
     * 触达不到，连接会无限空转、界面持续闪「重连中」。
     *
     * 因此主动关闭前必须先把处理器置 null（让这次关闭**不进入**重连判定），
     * 并把 socket 引用清空；各回调内的归属校验是第二道防线。
     */
    _teardown() {
      if (this.reconnectTimer) {
        clearTimeout(this.reconnectTimer)
        this.reconnectTimer = null
      }
      const old = this.socket
      if (!old) return
      this.socket = null
      old.onopen = null
      old.onclose = null
      old.onerror = null
      old.onmessage = null
      try {
        old.close()
      } catch {
        // ignore
      }
    },

    /** 排一次重连。保证同一时刻只有一个定时器，避免旧 handle 被覆盖成孤儿定时器。 */
    _scheduleReconnect(delay: number) {
      if (this.reconnectTimer) {
        clearTimeout(this.reconnectTimer)
        this.reconnectTimer = null
      }
      this.reconnectTimer = setTimeout(() => {
        this.reconnectTimer = null
        this.connect()
      }, delay)
    },

    connect() {
      // 先关闭已有连接（并摘除其处理器），避免多个 socket 同时存在导致 token 重复
      this._teardown()

      this.shouldReconnect = true
      wsConnectionStatus.value = this.reconnectAttempts > 0 ? 'reconnecting' : 'connecting'

      const sock = new WebSocket(wsBase())
      this.socket = sock

      sock.onopen = () => {
        // 归属校验：已被 connect() / disconnect() 替换掉的旧 socket 一律忽略
        if (this.socket !== sock) return
        console.log('[WS] 已连接')
        this.reconnectAttempts = 0
        wsConnectionStatus.value = 'connected'
        // P0-2：把握手期间排队的消息补发出去
        if (this.pendingQueue.length) {
          for (const data of this.pendingQueue.splice(0)) {
            try {
              sock.send(JSON.stringify(data))
            } catch {
              // 极端情况下 socket 已失效，忽略即可（上层有超时兜底）
            }
          }
        }
      }

      sock.onclose = (ev: CloseEvent) => {
        // 归属校验：主动替换 / 主动断开产生的 onclose 不参与重连决策
        if (this.socket !== sock) return
        console.log('[WS] 已断开')
        // E12：1008 Policy Violation —— token 无效 / 过期，回到登录门。
        if (ev.code === 1008) {
          handleUnauthorized()
          return
        }
        wsConnectionStatus.value = 'disconnected'
        if (this.shouldReconnect && this.reconnectAttempts < this.maxReconnects) {
          const delay = Math.min(this.baseDelay * Math.pow(2, this.reconnectAttempts), this.maxDelay)
          this.reconnectAttempts++
          console.log(`[WS] 重连 ${this.reconnectAttempts}/${this.maxReconnects}，${delay}ms 后重试`)
          wsConnectionStatus.value = 'reconnecting'
          this._scheduleReconnect(delay)
        } else if (this.reconnectAttempts >= this.maxReconnects) {
          console.warn('[WS] 已达最大重连次数，停止重连')
          wsConnectionStatus.value = 'disconnected'
        }
      }

      sock.onerror = (e) => {
        if (this.socket !== sock) return
        console.error('[WS] 错误', e)
      }

      // 绑定到本次新建的 socket（旧 socket 的处理器已在 _teardown 中摘除）
      sock.onmessage = (e: MessageEvent) => {
        if (this.socket !== sock) return
        this._onMessage(e)
      }
    },

    disconnect() {
      this.shouldReconnect = false
      this.reconnectAttempts = 0
      this._teardown()
      // 主动断开时丢弃未发出的排队消息，避免它们被「续」到下一次连接
      this.pendingQueue.length = 0
      wsConnectionStatus.value = 'disconnected'
    },

    /**
     * 发送控制/对话消息。
     *
     * @returns 是否已交付（true=已发送或已排队待握手后发送；false=连接不可用，消息被丢弃）
     *
     * P0-2：旧实现是
     * ```ts
     * if (this.socket?.readyState === WebSocket.OPEN) { this.socket.send(...) }
     * ```
     * ——没有 else 分支，不抛错、不排队、不回调。而调用方（chat store）事先
     * 已经把 `isStreaming` 置为 true，服务端却压根没收到请求、不会回 done/error，
     * 于是输入框与发送按钮被永久禁用，用户只能刷新页面。
     * 现在改为：OPEN 直接发；CONNECTING 排队并在 onopen 补发；
     * CLOSED / CLOSING 明确返回 false，由调用方给出可见反馈并撤销乐观状态。
     */
    send(data: unknown): boolean {
      const ready = this.socket?.readyState
      if (ready === WebSocket.OPEN) {
        this.socket?.send(JSON.stringify(data))
        return true
      }
      if (ready === WebSocket.CONNECTING) {
        this.pendingQueue.push(data)
        return true
      }
      return false
    },

    onMessage(handler: (data: unknown) => void) {
      this.messageHandlers.add(handler)
      // 注意：socket.onmessage 统一由 connect() 绑定（带归属校验），
      // 这里不要再覆盖它——旧的覆盖写法会摘掉归属校验，让陈旧 socket 的消息也能进来。
      // 返回取消注册函数，避免组件卸载后重复处理
      return () => {
        this.messageHandlers.delete(handler)
      }
    },
  }
}

export const apiClient = new APIClientImpl()
