export interface UIPluginManifest {
  id: string
  name: string
  version: string
  type: 'ui'
  entry: string
  core_api?: string
  /** 对应的后端插件 id（用于联动后端插件的激活状态） */
  backend_plugin_id?: string
  contributes?: {
    views?: PluginView[]
    settings_panels?: PluginSettingsPanel[]
    menu_items?: PluginMenuItem[]
    /** 全局悬浮层组件（固定定位，跨页面常驻显示） */
    overlays?: PluginOverlay[]
  }
}

export interface PluginOverlay {
  id: string
  component: string
}

export interface PluginView {
  id: string
  route: string
  title: string
  component: string
}

export interface PluginSettingsPanel {
  id: string
  title: string
  component: string
}

export interface PluginMenuItem {
  id: string
  parent: string
  label: string
  icon?: string
  view_id: string
}

export interface UIPluginContext {
  logger: PluginLogger
  config: Record<string, unknown>
  events: EventBusBridge
  api: APIClient
  router: PluginRouter
  stores: PluginStoreRegistry
}

export interface PluginLogger {
  info: (...args: unknown[]) => void
  warn: (...args: unknown[]) => void
  error: (...args: unknown[]) => void
}

export interface EventBusBridge {
  on: (event: string, handler: (data: unknown) => void) => void
  off: (event: string, handler: (data: unknown) => void) => void
  emit: (event: string, data: unknown) => void
}

export interface APIClient {
  get: <T>(url: string) => Promise<T>
  post: <T>(url: string, body?: unknown) => Promise<T>
  patch: <T>(url: string, body: unknown) => Promise<T>
  delete: <T>(url: string) => Promise<T>
  ws: {
    connect: () => void
    disconnect: () => void
    send: (data: unknown) => void
    onMessage: (handler: (data: unknown) => void) => void
  }
}

export interface PluginRouter {
  addRoute: (path: string, component: unknown, name: string) => void
  removeRoute: (name: string) => void
}

export interface PluginStoreRegistry {
  register: (id: string, store: unknown) => void
  unregister: (id: string) => void
}
