<script setup lang="ts">
// 根组件
import { computed, onMounted, onUnmounted } from 'vue'
import PluginOverlayHost from './components/PluginOverlayHost.vue'
import LoginView from './views/LoginView.vue'
import { useAuthStore } from './stores/auth'

const authStore = useAuthStore()

// E12：认证启用且未登录时，全站锁定到登录页（不渲染路由与插件悬浮层）。
const showLogin = computed(
  () =>
    authStore.ready &&
    authStore.enabled &&
    !authStore.isAuthenticated
)

// 页面级滚动条：仅在滚动时显示（由 body.is-scrolling 控制显隐，见 style.css）
let scrollTimer: number | undefined
function onAppScroll() {
  document.body.classList.add('is-scrolling')
  if (scrollTimer) window.clearTimeout(scrollTimer)
  scrollTimer = window.setTimeout(() => {
    document.body.classList.remove('is-scrolling')
  }, 800)
}
onMounted(() => {
  document.getElementById('app')?.addEventListener('scroll', onAppScroll, { passive: true })
})
onUnmounted(() => {
  document.getElementById('app')?.removeEventListener('scroll', onAppScroll)
  if (scrollTimer) window.clearTimeout(scrollTimer)
})
</script>

<template>
  <LoginView v-if="showLogin" />
  <template v-else>
    <router-view v-slot="{ Component }">
      <transition name="fade-slide" mode="out-in">
        <component :is="Component" />
      </transition>
    </router-view>
    <!-- 插件全局悬浮层（常驻所有页面） -->
    <PluginOverlayHost />
  </template>
</template>

<style>
body {
  margin: 0;
  font-family: var(--font-sans);
}
#app {
  width: 100%;
  height: 100vh;
  overflow-y: auto;
  overflow-x: hidden;
}
</style>
