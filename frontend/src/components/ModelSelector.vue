<script setup lang="ts">
import { onMounted, computed } from 'vue'
import { useProviderStore } from '../stores/providers'

const providerStore = useProviderStore()

const props = defineProps<{
  modelValue?: string
  provider?: string
}>()
const emit = defineEmits<{ 'update:modelValue': [value: string] }>()

// H9: Filter models by selected provider
const filteredModels = computed(() => {
  if (!props.provider) return providerStore.models
  return providerStore.models.filter((m) => m.provider === props.provider)
})

const groupedModels = computed(() => {
  const groups: Record<string, { provider: string; models: { id: string; name: string }[] }> = {}
  for (const model of filteredModels.value) {
    if (!groups[model.provider]) {
      groups[model.provider] = { provider: model.provider, models: [] }
    }
    groups[model.provider].models.push({ id: model.id, name: model.name })
  }
  return Object.values(groups)
})

function onChange(e: Event) {
  emit('update:modelValue', (e.target as HTMLSelectElement).value)
}

onMounted(() => {
  providerStore.loadModels()
})

/** 无可选模型时的原因：加载失败 vs 确实没有模型（未配置 API Key）。 */
const emptyHint = computed(() => {
  if (providerStore.loadError) return providerStore.loadError
  if (providerStore.models.length === 0) return '暂无可用模型，请先在设置中配置 Provider 的 API Key'
  return ''
})
</script>

<template>
  <div class="model-selector-wrapper">
    <span v-if="emptyHint" class="empty-hint" :title="emptyHint">{{ emptyHint }}</span>
    <select :value="modelValue" @change="onChange" class="model-selector">
      <option value="">选择模型...</option>
      <optgroup v-for="group in groupedModels" :key="group.provider" :label="group.provider">
        <option v-for="model in group.models" :key="model.id" :value="model.id">
          {{ model.name }}
        </option>
      </optgroup>
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

/* 无模型可选时的原因提示（加载失败 / 未配置 API Key） */
.empty-hint {
  max-width: 220px;
  margin-right: var(--space-sm);
  font-size: var(--font-size-xs);
  color: var(--color-text-secondary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
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
  min-width: 160px;
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

/* Group headers styled differently */
.model-selector :deep(optgroup) {
  font-weight: 700;
  font-size: var(--font-size-xs);
  text-transform: uppercase;
  letter-spacing: 0.04em;
  color: var(--color-primary);
  background: var(--bg-form);
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
