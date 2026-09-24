import { ref } from 'vue'

const BASE_URL = '/api'

export type WSConnectionStatus = 'connected' | 'connecting' | 'disconnected' | 'reconnecting'

export const wsConnectionStatus = ref<WSConnectionStatus>('disconnected')

class APIClientImpl {
  async get<T>(url: string): Promise<T> {
    const res = await fetch(`${BASE_URL}${url}`)
    if (!res.ok) throw new Error(await res.text())
    return res.json()
  }

  async post<T>(url: string, body?: unknown): Promise<T> {
    const res = await fetch(`${BASE_URL}${url}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: body ? JSON.stringify(body) : undefined,
    })
    if (!res.ok) throw new Error(await res.text())
    return res.json()
  }

  async patch<T>(url: string, body: unknown): Promise<T> {
    const res = await fetch(`${BASE_URL}${url}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    })
    if (!res.ok) throw new Error(await res.text())
    return res.json()
  }

  async delete<T>(url: string): Promise<T> {
    const res = await fetch(`${BASE_URL}${url}`, { method: 'DELETE' })
    if (!res.ok) throw new Error(await res.text())
    return res.json()
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

    _onMessage(e: MessageEvent) {
      try {
        const parsed = JSON.parse(e.data)
        this.messageHandlers.forEach((h) => h(parsed))
      } catch {
        this.messageHandlers.forEach((h) => h(e.data))
      }
    },

    connect() {
      // 先关闭已有连接，避免多个 socket 同时存在导致 token 重复
      if (this.socket) {
        this.shouldReconnect = false
        try {
          this.socket.close()
        } catch {
          // ignore
        }
        this.socket = null
      }
      if (this.reconnectTimer) {
        clearTimeout(this.reconnectTimer)
        this.reconnectTimer = null
      }

      this.shouldReconnect = true
      wsConnectionStatus.value = this.reconnectAttempts > 0 ? 'reconnecting' : 'connecting'

      const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
      this.socket = new WebSocket(`${protocol}//${window.location.host}/ws/chat`)

      this.socket.onopen = () => {
        console.log('[WS] 已连接')
        this.reconnectAttempts = 0
        wsConnectionStatus.value = 'connected'
      }

      this.socket.onclose = () => {
        console.log('[WS] 已断开')
        wsConnectionStatus.value = 'disconnected'
        if (this.shouldReconnect && this.reconnectAttempts < this.maxReconnects) {
          const delay = Math.min(this.baseDelay * Math.pow(2, this.reconnectAttempts), this.maxDelay)
          this.reconnectAttempts++
          console.log(`[WS] 重连 ${this.reconnectAttempts}/${this.maxReconnects}，${delay}ms 后重试`)
          wsConnectionStatus.value = 'reconnecting'
          this.reconnectTimer = setTimeout(() => this.connect(), delay)
        } else if (this.reconnectAttempts >= this.maxReconnects) {
          console.warn('[WS] 已达最大重连次数，停止重连')
          wsConnectionStatus.value = 'disconnected'
        }
      }

      this.socket.onerror = (e) => {
        console.error('[WS] 错误', e)
      }

      // Set onmessage on the new socket every time connect() is called
      this.socket.onmessage = (e: MessageEvent) => this._onMessage(e)
    },

    disconnect() {
      this.shouldReconnect = false
      this.reconnectAttempts = 0
      if (this.reconnectTimer) {
        clearTimeout(this.reconnectTimer)
        this.reconnectTimer = null
      }
      this.socket?.close()
      this.socket = null
      wsConnectionStatus.value = 'disconnected'
    },

    send(data: unknown) {
      if (this.socket?.readyState === WebSocket.OPEN) {
        this.socket.send(JSON.stringify(data))
      }
    },

    onMessage(handler: (data: unknown) => void) {
      this.messageHandlers.add(handler)
      // Also set onmessage on the current socket if it exists
      if (this.socket) {
        this.socket.onmessage = (e: MessageEvent) => this._onMessage(e)
      }
      // 返回取消注册函数，避免组件卸载后重复处理
      return () => {
        this.messageHandlers.delete(handler)
      }
    },
  }
}

export const apiClient = new APIClientImpl()
