import type { UIPluginContext, PluginLogger, EventBusBridge, PluginRouter, PluginStoreRegistry } from './plugin-types'
import { apiClient } from '../api/client'

const eventListeners = new Map<string, Set<(data: unknown) => void>>()
const registeredStores = new Map<string, unknown>()

const pluginLogger: PluginLogger = {
  info: (...args: unknown[]) => console.log('[Plugin]', ...args),
  warn: (...args: unknown[]) => console.warn('[Plugin]', ...args),
  error: (...args: unknown[]) => console.error('[Plugin]', ...args),
}

const eventBusBridge: EventBusBridge = {
  on(event: string, handler: (data: unknown) => void) {
    if (!eventListeners.has(event)) eventListeners.set(event, new Set())
    eventListeners.get(event)!.add(handler)
  },
  off(event: string, handler: (data: unknown) => void) {
    eventListeners.get(event)?.delete(handler)
  },
  emit(event: string, data: unknown) {
    eventListeners.get(event)?.forEach((h) => h(data))
  },
}

const storeRegistry: PluginStoreRegistry = {
  register(id: string, store: unknown) {
    registeredStores.set(id, store)
  },
  unregister(id: string) {
    registeredStores.delete(id)
  },
}

export function createPluginContext(
  pluginId: string,
  router: PluginRouter,
  config: Record<string, unknown> = {}
): UIPluginContext {
  return {
    logger: {
      info: (...args: unknown[]) => console.log(`[Plugin:${pluginId}]`, ...args),
      warn: (...args: unknown[]) => console.warn(`[Plugin:${pluginId}]`, ...args),
      error: (...args: unknown[]) => console.error(`[Plugin:${pluginId}]`, ...args),
    },
    config,
    events: eventBusBridge,
    api: apiClient,
    router,
    stores: storeRegistry,
  }
}

export { pluginLogger, eventBusBridge, storeRegistry }
