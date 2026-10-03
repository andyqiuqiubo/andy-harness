import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import type { Provider, Model } from '../api/types'
import { apiClient } from '../api/client'

export const useProviderStore = defineStore('providers', () => {
  const providers = ref<Provider[]>([])
  const models = ref<Model[]>([])

  const enabledProviders = computed(() => providers.value.filter((p) => p.enabled))

  /** 最近一次加载失败的原因（供 UI 提示，避免「静默空白」）。 */
  const loadError = ref<string | null>(null)

  async function loadProviders() {
    try {
      providers.value = await apiClient.get<Provider[]>('/providers')
      loadError.value = null
    } catch (e) {
      providers.value = []
      loadError.value = e instanceof Error ? e.message : String(e)
    }
  }

  async function loadModels() {
    try {
      models.value = await apiClient.get<Model[]>('/models')
      if (loadError.value) loadError.value = null
    } catch (e) {
      models.value = []
      loadError.value = e instanceof Error ? e.message : String(e)
    }
  }

  async function updateProvider(id: string, updates: Partial<Provider>) {
    await apiClient.patch<Provider>(`/providers/${id}`, updates)
    // 更新后重新加载，确保 enabled / has_api_key 等状态同步
    await loadProviders()
  }

  async function toggleProviderEnabled(id: string) {
    const provider = providers.value.find((p) => p.id === id)
    if (!provider) return
    await updateProvider(id, { enabled: !provider.enabled })
  }

  function modelsByProvider(providerId: string): Model[] {
    return models.value.filter((m) => m.provider === providerId)
  }

  return { providers, models, enabledProviders, loadError, loadProviders, loadModels, updateProvider, toggleProviderEnabled, modelsByProvider }
})
