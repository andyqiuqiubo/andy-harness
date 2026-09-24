<script setup lang="ts">
// 根组件
</script>

<template>
  <router-view v-slot="{ Component }">
    <transition name="fade-slide" mode="out-in">
      <component :is="Component" />
    </transition>
  </router-view>
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
