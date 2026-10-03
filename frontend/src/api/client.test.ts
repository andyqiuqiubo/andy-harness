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
