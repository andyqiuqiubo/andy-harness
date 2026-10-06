import { defineStore } from 'pinia'
import { ref } from 'vue'
import { apiClient } from '../api/client'

export type Theme = 'matrix' | 'ocean' | 'sunset' | 'dark' | 'light'
export type Language = 'zh' | 'en'

export interface SessionSettings {
  model: string
  temperature: number
  system_prompt: string
}

// 主题元数据（供 UI 展示名称和色块）
export interface ThemeMeta {
  id: Theme
  name: string
  color: string
}

export const THEME_LIST: ThemeMeta[] = [
  { id: 'matrix', name: '黑客帝国', color: '#00ff88' },
  { id: 'ocean', name: '深海蓝', color: '#00bfff' },
  { id: 'sunset', name: '日落紫红', color: '#ff44aa' },
  { id: 'dark', name: '中性深灰', color: '#8888ff' },
  { id: 'light', name: '亮色', color: '#00aa5f' },
]

const VALID_THEMES: Theme[] = ['matrix', 'ocean', 'sunset', 'dark', 'light']

function loadTheme(): Theme {
  const stored = localStorage.getItem('theme') as Theme | null
  // 兼容旧值：dark -> matrix（之前 dark 是默认黑绿，现在 dark 是深灰）
  if (stored === 'dark' && !localStorage.getItem('theme_migrated')) {
    // 旧 dark 实际是 matrix 黑绿，迁移
    localStorage.setItem('theme', 'matrix')
    localStorage.setItem('theme_migrated', '1')
    return 'matrix'
  }
  if (stored && VALID_THEMES.includes(stored)) return stored
  // 默认亮色主题
  return 'light'
}

/** 归一化后端语言（如 zh-CN）到前端枚举，非法值返回 null。 */
function normalizeLanguage(lang?: string): Language | null {
  if (!lang) return null
  if (lang.startsWith('zh')) return 'zh'
  if (lang.startsWith('en')) return 'en'
  return null
}

/** 后端 /api/settings 返回结构（与 rest/settings.py 对齐）。 */
interface BackendSettings {
  theme?: string
  language?: string
  default_model?: string
  default_budget?: number
}

export const useSettingsStore = defineStore('settings', () => {
  const theme = ref<Theme>(loadTheme())
  const language = ref<Language>(
    (localStorage.getItem('language') as Language) || 'zh'
  )

  const sessionSettings = ref<SessionSettings>({
    model: localStorage.getItem('session_model') || 'deepseek-flash',
    temperature: parseFloat(localStorage.getItem('session_temperature') || '0.7'),
    system_prompt: localStorage.getItem('session_system_prompt') || '',
  })

  function setTheme(t: Theme) {
    theme.value = t
    localStorage.setItem('theme', t)
    applyTheme(t)
    void persistBackend()
  }

  function setLanguage(lang: Language) {
    language.value = lang
    localStorage.setItem('language', lang)
    void persistBackend()
  }

  function applyTheme(t: Theme) {
    document.documentElement.setAttribute('data-theme', t)
  }

  function setSessionSettings(settings: Partial<SessionSettings>) {
    sessionSettings.value = { ...sessionSettings.value, ...settings }
    localStorage.setItem('session_model', sessionSettings.value.model)
    localStorage.setItem('session_temperature', String(sessionSettings.value.temperature))
    localStorage.setItem('session_system_prompt', sessionSettings.value.system_prompt)
    void persistBackend()
  }

  // D-1：把主题 / 语言 / 默认模型同步到后端 /api/settings（持久化到 settings.json），
  // localStorage 仍作为即时与离线兜底。后端不可用时静默忽略，不阻塞 UI。
  async function persistBackend() {
    try {
      await apiClient.put('/settings', {
        theme: theme.value,
        language: language.value,
        default_model: sessionSettings.value.model,
      })
    } catch {
      // 离线 / 后端未启用：忽略，localStorage 已兜底
    }
  }

  // D-1：启动时尝试从后端拉取已持久化的设置。
  // 关键：仅当本地无值时才用后端填充，避免覆盖用户既有的 localStorage 偏好
  // （此前前端从未写过后端，后端 settings.json 可能只是默认值，直接全覆盖会回退用户配置）。
  async function loadFromBackend() {
    try {
      const s = await apiClient.get<BackendSettings>('/settings')
      if (!localStorage.getItem('theme') && s.theme && VALID_THEMES.includes(s.theme as Theme)) {
        theme.value = s.theme as Theme
        localStorage.setItem('theme', s.theme)
        applyTheme(theme.value)
      }
      const lang = normalizeLanguage(s.language)
      if (!localStorage.getItem('language') && lang) {
        language.value = lang
        localStorage.setItem('language', lang)
      }
      if (!localStorage.getItem('session_model') && s.default_model) {
        sessionSettings.value = { ...sessionSettings.value, model: s.default_model }
        localStorage.setItem('session_model', s.default_model)
      }
    } catch {
      // 离线 / 后端未启用：保留 localStorage 默认值
    }
  }

  // 初始应用主题
  applyTheme(theme.value)
  // 异步从后端拉取持久化设置（不阻塞首屏）
  void loadFromBackend()

  return {
    theme,
    language,
    sessionSettings,
    setTheme,
    setLanguage,
    setSessionSettings,
    loadFromBackend,
  }
})
