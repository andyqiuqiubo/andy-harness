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

// Load and activate all UI plugins after mount
import { usePluginLoaderStore } from './stores/plugin-loader'
const pluginLoaderStore = usePluginLoaderStore(pinia)
pluginLoaderStore.initPlugins()
