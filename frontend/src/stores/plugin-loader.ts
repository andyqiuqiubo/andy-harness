import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import { getPluginLoader, type LoadedPlugin } from '../core/plugin-loader'
import type { PluginMenuItem } from '../core/plugin-types'

export const usePluginLoaderStore = defineStore('plugin-loader', () => {
  const plugins = ref<LoadedPlugin[]>([])
  const loaded = ref(false)

  const menuItems = computed<PluginMenuItem[]>(() => {
    return plugins.value
      .filter((p) => p.active)
      .flatMap((p) => p.menuItems)
  })

  const activeRoutes = computed(() => {
    return plugins.value
      .filter((p) => p.active)
      .flatMap((p) => p.manifest.contributes?.views ?? [])
  })

  /** 已激活插件的全局悬浮层组件 */
  const overlayPlugins = computed<LoadedPlugin[]>(() => {
    return plugins.value.filter((p) => p.active && p.overlayComponent)
  })

  async function initPlugins() {
    const loader = getPluginLoader()
    if (!loader) return
    await loader.initAll()
    plugins.value = loader.getLoadedPlugins()
    loaded.value = true
  }

  function activatePlugin(pluginId: string) {
    const loader = getPluginLoader()
    if (!loader) return
    loader.activate(pluginId)
    plugins.value = loader.getLoadedPlugins()
  }

  function deactivatePlugin(pluginId: string) {
    const loader = getPluginLoader()
    if (!loader) return
    loader.deactivate(pluginId)
    plugins.value = loader.getLoadedPlugins()
  }

  function isPluginActive(pluginId: string): boolean {
    return plugins.value.some((p) => p.manifest.id === pluginId && p.active)
  }

  /** 根据后端插件 id 激活/停用对应的前端 UI 插件 */
  function setBackendPluginActive(backendId: string, active: boolean): void {
    const loader = getPluginLoader()
    if (!loader) return
    const matched = plugins.value.find((p) => p.manifest.backend_plugin_id === backendId)
    if (!matched) return
    if (active && !matched.active) {
      loader.activate(matched.manifest.id)
    } else if (!active && matched.active) {
      loader.deactivate(matched.manifest.id)
    }
    plugins.value = loader.getLoadedPlugins()
  }

  /** 用后端插件激活状态同步所有前端 UI 插件（供列表加载/刷新后调用） */
  function syncWithBackend(backendPlugins: { id: string; activated: boolean }[]): void {
    const loader = getPluginLoader()
    if (!loader) return
    let changed = false
    for (const p of plugins.value) {
      const backendId = p.manifest.backend_plugin_id
      if (!backendId) continue
      const backend = backendPlugins.find((b) => b.id === backendId)
      // 后端插件不存在（如市场插件未安装/被删除）视为停用
      const backendActive = backend ? backend.activated : false
      if (backendActive && !p.active) {
        loader.activate(p.manifest.id)
        changed = true
      } else if (!backendActive && p.active) {
        loader.deactivate(p.manifest.id)
        changed = true
      }
    }
    if (changed) plugins.value = loader.getLoadedPlugins()
  }

  return {
    plugins,
    loaded,
    menuItems,
    activeRoutes,
    overlayPlugins,
    initPlugins,
    activatePlugin,
    deactivatePlugin,
    isPluginActive,
    setBackendPluginActive,
    syncWithBackend,
  }
})
