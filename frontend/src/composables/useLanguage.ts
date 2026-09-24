import { computed } from 'vue'
import { useSettingsStore } from '../stores/settings'
import zh from '../i18n/zh'
import en from '../i18n/en'

const translations: Record<string, Record<string, string>> = {
  zh,
  en,
}

export function useLanguage() {
  const settingsStore = useSettingsStore()

  const t = computed(() => {
    return (key: string): string => {
      const lang = settingsStore.language
      return translations[lang]?.[key] ?? translations['zh']?.[key] ?? key
    }
  })

  return { t, language: computed(() => settingsStore.language) }
}
