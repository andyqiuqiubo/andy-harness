/**
 * 运行环境识别（E11）：同一前端在浏览器与 Tauri 桌面壳中运行。
 *
 * - 浏览器：后端走相对路径（dev 由 Vite 代理，prod 由静态服务器同源托管）；
 * - Tauri：页面由 tauri 协议加载，后端是壳启动的本地进程，需使用绝对地址
 *   （http://127.0.0.1:<port>），通过 `get_backend_url` 命令向壳查询。
 */

import { invoke } from '@tauri-apps/api/core'

type TauriWindow = Window & { __TAURI_INTERNALS__?: unknown }

/** 固定端口兜底（与启动器默认一致；正常情况下由壳动态分配端口）。 */
export const FALLBACK_DESKTOP_URL = 'http://127.0.0.1:8000'

/** 是否运行在 Tauri 桌面壳中（Tauri 2 注入 __TAURI_INTERNALS__）。 */
export function isTauri(): boolean {
  return (
    typeof window !== 'undefined' &&
    Boolean((window as TauriWindow).__TAURI_INTERNALS__)
  )
}

let originPromise: Promise<string> | null = null

/** 解析后端源：浏览器为 ''（相对路径）；Tauri 为后端绝对地址。 */
export function resolveBackendOrigin(): Promise<string> {
  if (!isTauri()) return Promise.resolve('')
  if (!originPromise) {
    originPromise = invoke<string>('get_backend_url')
      .then((url) => url.replace(/\/+$/, ''))
      .catch(() => FALLBACK_DESKTOP_URL)
  }
  return originPromise
}

let resolvedOrigin = ''

/** 桌面壳拉起后端后，uvicorn 还要做插件加载 / DB 迁移 / MCP 连接，
 *  可能耗时几十秒。窗口此刻已经打开，前端若不等待就会先 fetch 一次并
 *  拿到「网络请求失败」，provider 列表被吞成空 → 模型选择器空白。
 * 这里在挂载前轮询 /api/health，直到后端真正就绪。 */
const READY_TIMEOUT_MS = 120000
const READY_INTERVAL_MS = 500

/** 轮询后端健康检查；浏览器模式下同源请求，同样能挡住「后端刚起」的竞态。 */
async function waitForBackendReady(origin: string): Promise<boolean> {
  const deadline = Date.now() + READY_TIMEOUT_MS
  while (Date.now() < deadline) {
    try {
      const res = await fetch(`${origin}/api/health`)
      if (res.ok) return true
    } catch {
      // 后端尚未监听，继续等
    }
    await new Promise((r) => setTimeout(r, READY_INTERVAL_MS))
  }
  return false
}

/** 应用启动时调用一次：解析并缓存后端地址，供 HTTP / WS 使用。 */
export async function initBackendRuntime(): Promise<string> {
  resolvedOrigin = await resolveBackendOrigin()
  const ready = await waitForBackendReady(resolvedOrigin)
  if (!ready) {
    // 不阻塞挂载：让界面照常显示，由各 store 的错误态提示用户。
    console.warn('[andy-harness] 后端在等待窗口内未就绪，请检查启动日志')
  }
  return resolvedOrigin
}

/** 当前已解析的后端源（浏览器为 ''）。 */
export function currentOrigin(): string {
  return resolvedOrigin
}
