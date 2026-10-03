<script setup lang="ts">
// 根组件
import { computed } from 'vue'
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
}
</style>
