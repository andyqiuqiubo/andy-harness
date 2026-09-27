import { createApp } from 'vue'
import { createPinia } from 'pinia'
import './style.css'
import App from './App.vue'
import router from './router'
import { initPluginLoader } from './core/plugin-loader'
import { vRipple } from './composables/useRipple'

const app = createApp(App)
const pinia = createPinia()
app.use(pinia)
app.use(router)

// 注册全局指令
app.directive('ripple', vRipple)

// Initialize plugin loader before mounting
initPluginLoader(router)
app.mount('#app')

// Load and activate all UI plugins after mount, then sync with backend plugin state
import { usePluginLoaderStore } from './stores/plugin-loader'
import { usePluginStore } from './stores/plugins'
const pluginLoaderStore = usePluginLoaderStore(pinia)
pluginLoaderStore.initPlugins().then(() => {
  // 拉取后端插件状态，同步前端 UI 插件的激活/停用（停用状态在刷新后保持）
  usePluginStore(pinia).loadPlugins()
})
