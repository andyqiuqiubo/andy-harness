import { defineStore } from 'pinia'
import { ref } from 'vue'
import { apiClient } from '../api/client'
import type { PermissionAction, PermissionMode, PermissionsState } from '../api/types'

export const usePermissionStore = defineStore('permissions', () => {
  const state = ref<PermissionsState>({
    available: false,
    mode: null,
    modes: [],
    actions: [],
    overrides: {},
    tools: [],
  })
  const loading = ref(false)
  const error = ref<string | null>(null)

  async function loadPermissions() {
    loading.value = true
    error.value = null
    try {
      state.value = await apiClient.get<PermissionsState>('/permissions')
    } catch (e) {
      state.value.available = false
      error.value = String(e)
    } finally {
      loading.value = false
    }
  }

  async function setMode(mode: PermissionMode) {
    error.value = null
    try {
      const res = await apiClient.put<PermissionsState>('/permissions', { mode })
      // ⚠️ 合并而非整体替换：后端 PUT 响应只回传 mode/overrides/tools，
      // 不含 modes/actions/available。若整体替换，modes 清空会让 <option>
      // 列表为空、select 选中文字消失且 available 丢失导致下拉被禁用。
      // 合并保留已加载的 modes/actions/available，仅覆盖变更字段。
      state.value = { ...state.value, ...res }
    } catch (e) {
      error.value = String(e)
      throw e
    }
  }

  async function setOverride(toolName: string, action: PermissionAction | null) {
    error.value = null
    try {
      await apiClient.put(`/permissions/${toolName}`, { action })
      await loadPermissions()
    } catch (e) {
      error.value = String(e)
      throw e
    }
  }

  return { state, loading, error, loadPermissions, setMode, setOverride }
})
