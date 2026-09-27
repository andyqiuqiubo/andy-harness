<script setup lang="ts">
import { usePluginLoaderStore } from '../stores/plugin-loader'

const pluginLoaderStore = usePluginLoaderStore()
</script>

<template>
  <div class="plugin-overlay-host">
    <component
      v-for="p in pluginLoaderStore.overlayPlugins"
      :key="p.manifest.id"
      :is="p.overlayComponent"
    />
  </div>
</template>

<style scoped>
/* 宿主本身不拦截任何事件，由插件内部自行管理 pointer-events */
.plugin-overlay-host {
  position: fixed;
  inset: 0;
  z-index: 9000;
  pointer-events: none;
  overflow: hidden;
}
</style>
