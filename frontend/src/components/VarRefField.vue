<script setup lang="ts">
import { nextTick, ref, watch } from 'vue'
import { useLanguage } from '../composables/useLanguage'

const { t } = useLanguage()

const props = defineProps<{
  modelValue: string
  vars: { nodeId: string; title: string; fields: (string | { name: string; label?: string })[] }[]
  multiline?: boolean
  rows?: number
  placeholder?: string
  mono?: boolean
}>()

function fieldName(f: string | { name: string; label?: string }): string {
  return typeof f === 'string' ? f : f.name
}
function fieldLabel(f: string | { name: string; label?: string }): string {
  return typeof f === 'string' ? f : f.label || f.name
}
const emit = defineEmits<{ (e: 'update:modelValue', v: string): void }>()

const local = ref(props.modelValue)
const open = ref(false)
const elRef = ref<HTMLElement | null>(null)
const popRef = ref<HTMLElement | null>(null)

watch(
  () => props.modelValue,
  (v) => { local.value = v },
)

function onInput(e: Event) {
  local.value = (e.target as HTMLInputElement | HTMLTextAreaElement).value
  emit('update:modelValue', local.value)
}

function toggle() {
  open.value = !open.value
}

function pick(nodeId: string, field: string | { name: string; label?: string }) {
  const name = fieldName(field)
  const ref = `{{${nodeId}.${name}}}`
  const el = elRef.value as HTMLInputElement | HTMLTextAreaElement | null
  const cur = local.value || ''
  let s = cur.length
  let en = cur.length
  if (el && typeof (el as HTMLInputElement).selectionStart === 'number') {
    s = (el as HTMLInputElement).selectionStart as number
    en = (el as HTMLInputElement).selectionEnd as number
  }
  const nv = cur.slice(0, s) + ref + cur.slice(en)
  local.value = nv
  emit('update:modelValue', nv)
  open.value = false
  nextTick(() => {
    if (el) {
      try {
        el.focus()
        ;(el as HTMLInputElement).selectionStart = (el as HTMLInputElement).selectionEnd = s + ref.length
      } catch {
        /* noop */
      }
    }
  })
}

watch(
  () => open.value,
  (isOpen) => {
    if (!isOpen) return
    const handler = (e: MouseEvent) => {
      const tgt = e.target as HTMLElement
      if (popRef.value && popRef.value.contains(tgt)) return
      if (tgt.classList && tgt.classList.contains('wf-insert')) return
      open.value = false
    }
    setTimeout(() => document.addEventListener('mousedown', handler, { once: true }), 0)
  },
)
</script>

<template>
  <div class="var-ref">
    <textarea
      v-if="multiline"
      ref="elRef"
      class="form-input"
      :class="{ 'wf-code': mono }"
      :rows="rows || 3"
      :placeholder="placeholder"
      :value="local"
      @input="onInput"
    ></textarea>
    <input
      v-else
      ref="elRef"
      class="form-input"
      :placeholder="placeholder"
      :value="local"
      @input="onInput"
    />
    <button type="button" class="wf-insert" :title="t('wfEditor.insertVar')" @click="toggle">⤓</button>
    <div v-if="open" ref="popRef" class="wf-var-pop">
      <div class="wf-var-pop-head">
        <span>{{ t('wfEditor.varHint') }}</span>
        <button type="button" class="wf-var-pop-x" @click="open = false">×</button>
      </div>
      <div v-if="!vars.length" class="wf-var-empty">{{ t('wfEditor.noVars') }}</div>
      <div v-for="grp in vars" :key="grp.nodeId" class="wf-var-grp">
        <div class="wf-var-grp-title">{{ grp.title }} <code>{{ grp.nodeId }}</code></div>
        <div class="wf-var-fields">
          <button
            v-for="f in grp.fields"
            :key="fieldName(f)"
            type="button"
            class="wf-var-chip"
            :title="fieldName(f)"
            @click="pick(grp.nodeId, f)"
          >
            {{ fieldLabel(f) }}<code v-if="fieldLabel(f) !== fieldName(f)" class="wf-var-fname">{{ fieldName(f) }}</code>
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.var-ref { position: relative; }
.var-ref .form-input { width: 100%; }
.var-ref input.form-input { padding-right: 30px; }
.wf-insert {
  position: absolute;
  top: 4px;
  right: 4px;
  width: 24px;
  height: 24px;
  border: 1px solid var(--border-color);
  border-radius: var(--radius-sm);
  background: var(--bg-input);
  color: var(--color-text-secondary);
  cursor: pointer;
  font-size: 0.85rem;
  line-height: 1;
  display: flex;
  align-items: center;
  justify-content: center;
}
.wf-insert:hover { border-color: var(--color-primary); color: var(--color-primary); }
.wf-var-pop {
  position: absolute;
  top: calc(100% + 4px);
  right: 0;
  z-index: 30;
  width: 260px;
  max-height: 240px;
  overflow-y: auto;
  background: var(--bg-surface);
  border: 1px solid var(--border-color);
  border-radius: var(--radius-md);
  box-shadow: var(--shadow-xl);
  padding: var(--space-xs);
}
.wf-var-pop-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  font-size: var(--font-size-xs);
  color: var(--color-text-secondary);
  padding: 2px 2px 6px;
  border-bottom: 1px solid var(--border-color);
  margin-bottom: 6px;
}
.wf-var-pop-x {
  border: none;
  background: transparent;
  color: var(--color-text-tertiary);
  cursor: pointer;
  font-size: 1rem;
  line-height: 1;
}
.wf-var-empty { font-size: var(--font-size-xs); color: var(--color-text-tertiary); padding: 6px 2px; }
.wf-var-grp { margin-bottom: 8px; }
.wf-var-grp-title { font-size: var(--font-size-xs); color: var(--color-text); font-weight: 600; margin-bottom: 4px; }
.wf-var-grp-title code { font-size: 0.7rem; color: var(--color-text-tertiary); background: var(--bg-input); padding: 1px 4px; border-radius: var(--radius-sm); }
.wf-var-fields { display: flex; flex-wrap: wrap; gap: 4px; }
.wf-var-chip {
  border: 1px solid var(--border-color);
  background: var(--bg-form);
  color: var(--color-text);
  border-radius: var(--radius-sm);
  padding: 3px 8px;
  font-size: var(--font-size-xs);
  cursor: pointer;
}
.wf-var-chip:hover { border-color: var(--color-primary); color: var(--color-primary); }
.wf-var-fname { margin-left: 4px; font-size: 0.68rem; color: var(--color-text-tertiary); font-family: var(--font-mono, monospace); }
</style>
