import { defineStore } from 'pinia'
import { ref } from 'vue'

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
  return 'matrix'
}

export const useSettingsStore = defineStore('settings', () => {
  const theme = ref<Theme>(loadTheme())
  const language = ref<Language>(
    (localStorage.getItem('language') as Language) || 'zh'
  )

  const sessionSettings = ref<SessionSettings>({
    model: localStorage.getItem('session_model') || 'deepseek-v4-flash',
    temperature: parseFloat(localStorage.getItem('session_temperature') || '0.7'),
    system_prompt: localStorage.getItem('session_system_prompt') || '',
  })

  function setTheme(t: Theme) {
    theme.value = t
    localStorage.setItem('theme', t)
    applyTheme(t)
  }

  function setLanguage(lang: Language) {
    language.value = lang
    localStorage.setItem('language', lang)
  }

  function applyTheme(t: Theme) {
    document.documentElement.setAttribute('data-theme', t)
  }

  function setSessionSettings(settings: Partial<SessionSettings>) {
    sessionSettings.value = { ...sessionSettings.value, ...settings }
    localStorage.setItem('session_model', sessionSettings.value.model)
    localStorage.setItem('session_temperature', String(sessionSettings.value.temperature))
    localStorage.setItem('session_system_prompt', sessionSettings.value.system_prompt)
  }

  // 初始应用主题
  applyTheme(theme.value)

  return { theme, language, sessionSettings, setTheme, setLanguage, setSessionSettings }
})
