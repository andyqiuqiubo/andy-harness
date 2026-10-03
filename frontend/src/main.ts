import { createApp } from 'vue'
import { createPinia } from 'pinia'
import './style.css'
import App from './App.vue'
import router from './router'
import { initPluginLoader } from './core/plugin-loader'
import { initBackendRuntime } from './api/runtime'
import { useAuthStore } from './stores/auth'
import { useSettingsStore } from './stores/settings'
import { vRipple } from './composables/useRipple'

const app = createApp(App)
const pinia = createPinia()
app.use(pinia)
app.use(router)

// 注册全局指令
app.directive('ripple', vRipple)

// Initialize plugin loader before mounting
initPluginLoader(router)

// E11：先解析后端地址（Tauri 中需向壳查询；浏览器立即返回），再初始化
// E12 认证状态（默认关闭，不影响挂载），最后挂载。
initBackendRuntime().then(async () => {
  await useAuthStore(pinia).init()
  // 尽早实例化设置 store：触发 loadFromBackend，把后端持久化的主题/语言
  // 应用到任意首屏（否则主题只在访问过「设置」页后才生效）。
  useSettingsStore(pinia)
  app.mount('#app')
})

// Load and activate all UI plugins after mount, then sync with backend plugin state
import { usePluginLoaderStore } from './stores/plugin-loader'
import { usePluginStore } from './stores/plugins'
const pluginLoaderStore = usePluginLoaderStore(pinia)
pluginLoaderStore.initPlugins().then(() => {
  // 拉取后端插件状态，同步前端 UI 插件的激活/停用（停用状态在刷新后保持）
  usePluginStore(pinia).loadPlugins()
})
