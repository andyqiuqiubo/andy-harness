/**
 * 运行环境识别测试（E11）：浏览器与 Tauri 两种后端地址解析。
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const invokeMock = vi.hoisted(() => vi.fn())

vi.mock('@tauri-apps/api/core', () => ({
  invoke: invokeMock,
}))

/** 后端就绪轮询用的 fetch 桩：默认立刻返回健康检查通过。 */
function stubHealth(ok: boolean): void {
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => ({ ok }) as Response),
  )
}

beforeEach(() => {
  invokeMock.mockReset()
  vi.resetModules()
  stubHealth(true)
  // @ts-expect-error 测试中控制 Tauri 标记
  delete window.__TAURI_INTERNALS__
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('runtime backend origin', () => {
  it('browser: uses relative origin (empty)', async () => {
    const runtime = await import('./runtime')
    expect(runtime.isTauri()).toBe(false)
    expect(await runtime.resolveBackendOrigin()).toBe('')
    await runtime.initBackendRuntime()
    expect(runtime.currentOrigin()).toBe('')
    expect(invokeMock).not.toHaveBeenCalled()
  })

  it('tauri: resolves absolute backend url from shell command', async () => {
    // @ts-expect-error 标记为 Tauri 环境
    window.__TAURI_INTERNALS__ = {}
    invokeMock.mockResolvedValue('http://127.0.0.1:8123')
    const runtime = await import('./runtime')
    expect(runtime.isTauri()).toBe(true)
    await runtime.initBackendRuntime()
    expect(runtime.currentOrigin()).toBe('http://127.0.0.1:8123')
    expect(invokeMock).toHaveBeenCalledWith('get_backend_url')
    // 挂载前必须先等后端就绪（桌面模式下窗口先于 uvicorn 就绪）
    expect(fetch).toHaveBeenCalledWith('http://127.0.0.1:8123/api/health')
  })

  it('tauri: strips trailing slashes', async () => {
    // @ts-expect-error 标记为 Tauri 环境
    window.__TAURI_INTERNALS__ = {}
    invokeMock.mockResolvedValue('http://127.0.0.1:9000///')
    const runtime = await import('./runtime')
    expect(await runtime.resolveBackendOrigin()).toBe('http://127.0.0.1:9000')
  })

  it('tauri: falls back when invoke fails', async () => {
    // @ts-expect-error 标记为 Tauri 环境
    window.__TAURI_INTERNALS__ = {}
    invokeMock.mockRejectedValue(new Error('no ipc'))
    const runtime = await import('./runtime')
    expect(await runtime.resolveBackendOrigin()).toBe(
      runtime.FALLBACK_DESKTOP_URL
    )
  })

  it('tauri: waits for backend readiness before returning (retry until ok)', async () => {
    // @ts-expect-error 标记为 Tauri 环境
    window.__TAURI_INTERNALS__ = {}
    invokeMock.mockResolvedValue('http://127.0.0.1:8123')
    // 模拟真实故障：uvicorn 尚未监听时 fetch reject / 非 2xx，
    // 窗口已打开但后端还在启动。
    let calls = 0
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => {
        calls += 1
        if (calls < 3) throw new TypeError('Failed to fetch')
        return { ok: true } as Response
      }),
    )
    const runtime = await import('./runtime')
    await runtime.initBackendRuntime()
    expect(calls).toBe(3)
    expect(runtime.currentOrigin()).toBe('http://127.0.0.1:8123')
  })
})
