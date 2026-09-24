import { defineStore } from 'pinia'
import { ref } from 'vue'
import { apiClient } from '../api/client'

export interface PluginInfo {
  id: string
  name: string
  version: string
  type: string
  activated: boolean
  core: boolean
  permissions?: string[]
  config_schema?: Record<string, unknown>
  config?: Record<string, unknown>
}

export interface InstallPluginRequest {
  plugin_id: string
  name: string
  version?: string
  type?: string
  entry: string
  permissions?: string[]
  description?: string
  config_schema?: Record<string, unknown>
  plugin_code: string
}

export const usePluginStore = defineStore('plugins', () => {
  const plugins = ref<PluginInfo[]>([])
  const loading = ref(false)
  const error = ref<string | null>(null)

  async function loadPlugins() {
    loading.value = true
    try {
      plugins.value = await apiClient.get<PluginInfo[]>('/plugins')
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
    } catch (e) {
      error.value = String(e)
      throw e
    }
  }

  async function installPlugin(req: InstallPluginRequest) {
    error.value = null
    try {
      await apiClient.post('/plugins/install', req)
      await loadPlugins()
    } catch (e) {
      error.value = String(e)
      throw e
    }
  }

  async function uninstallPlugin(id: string) {
    error.value = null
    try {
      await apiClient.delete(`/plugins/${id}`)
      plugins.value = plugins.value.filter((p) => p.id !== id)
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
    loading,
    error,
    loadPlugins,
    activatePlugin,
    deactivatePlugin,
    installPlugin,
    uninstallPlugin,
    fetchPluginConfig,
    savePluginConfig,
  }
})
