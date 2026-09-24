<script setup lang="ts">
// 示例前端插件视图
</script>

<template>
  <div class="hello-view">
    <h2>Hello from Plugin!</h2>
    <p>这是一个示例前端 UI 插件，验证前端插件机制完整可用。</p>
    <p>停用此插件后，此视图和侧栏菜单项将消失；重新启用后恢复。</p>
  </div>
</template>

<style scoped>
.hello-view {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  min-height: 100vh;
  gap: 0.5rem;
  text-align: center;
}
.hello-view h2 {
  font-size: 2rem;
  color: var(--color-primary, #1976d2);
}
.hello-view p {
  color: var(--color-text-secondary, #666);
  max-width: 400px;
}
</style>
