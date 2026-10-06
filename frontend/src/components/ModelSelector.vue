<script setup lang="ts">
import { computed } from 'vue'
import { useProviderStore } from '../stores/providers'
import { modelsForProvider } from '../utils/modelCatalog'

const providerStore = useProviderStore()

const props = defineProps<{
  modelValue?: string
  provider?: string
}>()
const emit = defineEmits<{ 'update:modelValue': [value: string] }>()

/** 所选 provider 在下拉里应展示的模型选项（目录过滤 + 显示名）。 */
const options = computed(() => {
  if (!props.provider) return []
  const p = providerStore.providers.find((x) => x.id === props.provider)
  return modelsForProvider(props.provider, p?.models || [])
})

function onChange(e: Event) {
  emit('update:modelValue', (e.target as HTMLSelectElement).value)
}
</script>

<template>
  <div class="model-selector-wrapper">
    <select :value="modelValue" @change="onChange" class="model-selector">
      <option v-for="opt in options" :key="opt.id" :value="opt.id">
        {{ opt.label }}
      </option>
    </select>
    <span class="selector-arrow">
      <svg width="12" height="12" viewBox="0 0 12 12" fill="none" xmlns="http://www.w3.org/2000/svg">
        <path d="M2.5 4.5L6 8L9.5 4.5" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
      </svg>
    </span>
  </div>
</template>

<style scoped>
.model-selector-wrapper {
  position: relative;
  display: inline-flex;
  align-items: center;
}

.model-selector {
  appearance: none;
  -webkit-appearance: none;
  -moz-appearance: none;
  padding: var(--space-sm) var(--space-xl) var(--space-sm) var(--space-md);
  border-radius: var(--radius-md);
  border: 1px solid var(--border-color);
  font-size: var(--font-size-sm);
  font-weight: 500;
  background: var(--bg-input);
  color: var(--color-text);
  cursor: pointer;
  transition: border-color var(--transition-base), box-shadow var(--transition-base), background var(--transition-base);
  min-width: 170px;
}

.model-selector:hover {
  border-color: var(--border-strong);
}

.model-selector:focus {
  border-color: var(--color-primary);
  box-shadow: 0 0 0 3px var(--color-primary-light);
}

/* Custom arrow icon */
.selector-arrow {
  position: absolute;
  right: var(--space-sm);
  pointer-events: none;
  display: flex;
  align-items: center;
  color: var(--color-text-secondary);
  transition: color var(--transition-base);
}

.model-selector:focus + .selector-arrow {
  color: var(--color-primary);
}

.model-selector :deep(option) {
  padding: var(--space-xs) var(--space-sm);
  font-weight: 400;
  color: var(--color-text);
  background: var(--bg-surface);
}

.model-selector :deep(option:checked) {
  font-weight: 600;
  color: var(--color-primary);
}
</style>
