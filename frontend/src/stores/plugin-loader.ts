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

  return {
    plugins,
    loaded,
    menuItems,
    activeRoutes,
    initPlugins,
    activatePlugin,
    deactivatePlugin,
    isPluginActive,
  }
})
