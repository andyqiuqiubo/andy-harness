import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import { apiClient, onUnauthorized } from '../api/client'
import { ApiError } from '../api/client'
import { clearToken, getToken, setToken } from '../api/token'

export interface AuthUser {
  id: string
  username: string
  is_admin: boolean
  created_at?: string
}

/**
 * E12：认证状态。
 *
 * - 认证默认关闭（enabled=false），登录门不渲染，行为与旧版一致；
 * - HARNESS_AUTH=1 后，init() 拉取 status 并在本地有 token 时回填用户；
 * - 401 / WS 1008 经 client 回调清空状态，由 App.vue 切回登录门。
 */
export const useAuthStore = defineStore('auth', () => {
  const enabled = ref(false)
  const token = ref(getToken())
  const user = ref<AuthUser | null>(null)
  const loading = ref(false)
  const error = ref('')
  /** status 是否已拉取（避免登录门在初始化前闪烁）。 */
  const ready = ref(false)

  const isAuthenticated = computed(() => enabled.value && !!token.value)

  function registerUnauthorizedHandler(): void {
    onUnauthorized(() => {
      token.value = ''
      user.value = null
    })
  }

  async function init(): Promise<void> {
    registerUnauthorizedHandler()
    try {
      const status = await apiClient.get<{ enabled: boolean }>('/auth/status')
      enabled.value = status.enabled
      if (enabled.value && token.value) {
        try {
          user.value = await apiClient.get<AuthUser>('/auth/me')
        } catch {
          // 本地 token 已失效（服务端重启 / 过期），清除并要求重新登录。
          clearToken()
          token.value = ''
          user.value = null
        }
      }
    } catch (e) {
      // 后端不可达时不强制登录门，保留旧行为，仅记录状态。
      enabled.value = false
      error.value = e instanceof ApiError ? e.message : ''
    } finally {
      ready.value = true
    }
  }

  async function login(username: string, password: string): Promise<boolean> {
    loading.value = true
    error.value = ''
    try {
      const result = await apiClient.post<{
        token: string
        user: AuthUser
        expires_in_seconds: number
      }>('/auth/login', { username, password })
      token.value = result.token
      user.value = result.user
      setToken(result.token)
      return true
    } catch (e) {
      error.value =
        e instanceof ApiError ? e.message : e instanceof Error ? e.message : '登录失败'
      return false
    } finally {
      loading.value = false
    }
  }

  function logout(): void {
    apiClient.post('/auth/logout').catch(() => {
      // 无状态登出，接口失败不影响本地清理。
    })
    clearToken()
    token.value = ''
    user.value = null
  }

  return {
    enabled,
    token,
    user,
    loading,
    error,
    ready,
    isAuthenticated,
    init,
    login,
    logout,
  }
})
