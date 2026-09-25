import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import type { Provider, Model } from '../api/types'
import { apiClient } from '../api/client'

export const useProviderStore = defineStore('providers', () => {
  const providers = ref<Provider[]>([])
  const models = ref<Model[]>([])

  const enabledProviders = computed(() => providers.value.filter((p) => p.enabled))

  async function loadProviders() {
    try {
      providers.value = await apiClient.get<Provider[]>('/providers')
    } catch {
      providers.value = []
    }
  }

  async function loadModels() {
    try {
      models.value = await apiClient.get<Model[]>('/models')
    } catch {
      models.value = []
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

  return { providers, models, enabledProviders, loadProviders, loadModels, updateProvider, toggleProviderEnabled, modelsByProvider }
})
