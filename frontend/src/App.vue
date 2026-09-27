<script setup lang="ts">
// 根组件
import PluginOverlayHost from './components/PluginOverlayHost.vue'
</script>

<template>
  <router-view v-slot="{ Component }">
    <transition name="fade-slide" mode="out-in">
      <component :is="Component" />
    </transition>
  </router-view>
  <!-- 插件全局悬浮层（常驻所有页面） -->
  <PluginOverlayHost />
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
