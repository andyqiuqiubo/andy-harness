import { defineStore } from 'pinia'
import { ref } from 'vue'
import { apiClient } from '../api/client'
import { usePluginLoaderStore } from './plugin-loader'

export interface PluginInfo {
  id: string
  name: string
  version: string
  type: string
  activated: boolean
  core: boolean
  /** 插件来源：system（系统内置）/ marketplace（从插件市场安装） */
  source?: string
  permissions?: string[]
  config_schema?: Record<string, unknown>
  config?: Record<string, unknown>
}

export interface MarketplacePlugin {
  plugin_id: string
  name: string
  version: string
  type: string
  entry: string
  description: string
  long_description: string
  permissions: string[]
  config_schema?: Record<string, unknown> | null
  plugin_code: string
  installed: boolean
}

export const usePluginStore = defineStore('plugins', () => {
  const plugins = ref<PluginInfo[]>([])
  const marketplace = ref<MarketplacePlugin[]>([])
  const loading = ref(false)
  const error = ref<string | null>(null)

  async function loadPlugins() {
    loading.value = true
    try {
      plugins.value = await apiClient.get<PluginInfo[]>('/plugins')
      // 同步前端 UI 插件的激活状态（覆盖刷新后重新激活的问题）
      usePluginLoaderStore().syncWithBackend(plugins.value)
    } catch {
      plugins.value = []
    } finally {
      loading.value = false
    }
  }

  async function activatePlugin(id: string) {
    error.value = null
    try {
      await apiClient.post(`/plugins/${id}/activate`)
      const p = plugins.value.find((p) => p.id === id)
      if (p) p.activated = true
      usePluginLoaderStore().setBackendPluginActive(id, true)
    } catch (e) {
      error.value = String(e)
      throw e
    }
  }

  async function deactivatePlugin(id: string) {
    error.value = null
    try {
      await apiClient.post(`/plugins/${id}/deactivate`)
      const p = plugins.value.find((p) => p.id === id)
      if (p) p.activated = false
      usePluginLoaderStore().setBackendPluginActive(id, false)
    } catch (e) {
      error.value = String(e)
      throw e
    }
  }

  async function fetchMarketplace() {
    try {
      marketplace.value = await apiClient.get<MarketplacePlugin[]>('/plugins/marketplace')
    } catch {
      marketplace.value = []
    }
  }

  async function installMarketplacePlugin(pluginId: string) {
    error.value = null
    try {
      await apiClient.post(`/plugins/marketplace/${pluginId}/install`)
      // 刷新已安装列表与市场状态，联动前端 UI 插件
      await loadPlugins()
      await fetchMarketplace()
    } catch (e) {
      error.value = String(e)
      throw e
    }
  }

  async function uninstallPlugin(id: string) {
    error.value = null
    try {
      await apiClient.delete(`/plugins/${id}`)
      // 重新加载并同步，确保前端 UI 插件（如元气宠物）随之后端停用
      await loadPlugins()
      await fetchMarketplace()
    } catch (e) {
      error.value = String(e)
      throw e
    }
  }

  async function fetchPluginConfig(id: string) {
    try {
      const res = await apiClient.get<{ config_schema: Record<string, unknown>; config: Record<string, unknown> }>(`/plugins/${id}/config`)
      const p = plugins.value.find((p) => p.id === id)
      if (p) {
        p.config_schema = res.config_schema
        p.config = res.config
      }
      return res
    } catch {
      return null
    }
  }

  async function savePluginConfig(id: string, config: Record<string, unknown>) {
    await apiClient.patch(`/plugins/${id}/config`, { config })
    const p = plugins.value.find((p) => p.id === id)
    if (p) p.config = config
  }

  return {
    plugins,
    marketplace,
    loading,
    error,
    loadPlugins,
    fetchMarketplace,
    installMarketplacePlugin,
    activatePlugin,
    deactivatePlugin,
    uninstallPlugin,
    fetchPluginConfig,
    savePluginConfig,
  }
})
