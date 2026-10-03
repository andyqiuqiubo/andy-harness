import { defineStore } from 'pinia'
import { ref } from 'vue'
import { apiClient } from '../api/client'

export interface SkillInfo {
  name: string
  description: string
  /** 来源：builtin（内置）/ user（用户级）/ plugin（插件自带） */
  source: string
  version: string
  license: string
  allowed_tools: string
  enabled: boolean
  body_chars: number
  resources: string[]
  path: string
}

export interface SkillDetail {
  meta: SkillInfo
  body: string
  resources: string[]
}

export interface SkillMarketplaceItem {
  name: string
  package_id: string
  description: string
  version: string
  license: string
  long_description: string
  installed: boolean
}

interface SkillListResponse {
  skills: SkillInfo[]
  count: number
  available: boolean
}

export const useSkillStore = defineStore('skills', () => {
  const skills = ref<SkillInfo[]>([])
  const loading = ref(false)
  const available = ref(false)
  const error = ref<string | null>(null)
  const marketplace = ref<SkillMarketplaceItem[]>([])
  const marketplaceLoading = ref(false)

  async function loadSkills() {
    loading.value = true
    error.value = null
    try {
      const res = await apiClient.get<SkillListResponse>('/skills')
      skills.value = res.skills ?? []
      available.value = res.available === true
    } catch (e) {
      skills.value = []
      available.value = false
      error.value = String(e)
    } finally {
      loading.value = false
    }
  }

  async function reloadSkills() {
    error.value = null
    try {
      const res = await apiClient.post<{ count: number; available: boolean }>('/skills/reload')
      available.value = res.available === true
      await loadSkills()
      return res.count
    } catch (e) {
      error.value = String(e)
      throw e
    }
  }

  async function setSkillEnabled(name: string, enabled: boolean) {
    error.value = null
    try {
      await apiClient.patch(`/skills/${name}`, { enabled })
      const s = skills.value.find((s) => s.name === name)
      if (s) s.enabled = enabled
    } catch (e) {
      error.value = String(e)
      throw e
    }
  }

  async function fetchSkillDetail(name: string): Promise<SkillDetail | null> {
    try {
      return await apiClient.get<SkillDetail>(`/skills/${name}`)
    } catch {
      return null
    }
  }

  async function fetchMarketplace() {
    marketplaceLoading.value = true
    try {
      marketplace.value = await apiClient.get<SkillMarketplaceItem[]>(
        '/skills/marketplace',
      )
    } catch {
      marketplace.value = []
    } finally {
      marketplaceLoading.value = false
    }
  }

  async function installMarketplaceSkill(packageId: string) {
    error.value = null
    try {
      await apiClient.post(`/skills/marketplace/${packageId}/install`)
      // 刷新已安装列表与市场状态
      await loadSkills()
      await fetchMarketplace()
    } catch (e) {
      error.value = String(e)
      throw e
    }
  }

  async function uninstallMarketplaceSkill(packageId: string) {
    error.value = null
    try {
      await apiClient.delete(`/skills/marketplace/${packageId}`)
      await loadSkills()
      await fetchMarketplace()
    } catch (e) {
      error.value = String(e)
      throw e
    }
  }

  return {
    skills,
    loading,
    available,
    error,
    marketplace,
    marketplaceLoading,
    loadSkills,
    reloadSkills,
    setSkillEnabled,
    fetchSkillDetail,
    fetchMarketplace,
    installMarketplaceSkill,
    uninstallMarketplaceSkill,
  }
})
