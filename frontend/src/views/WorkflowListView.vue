<script setup lang="ts">
/* eslint-disable @typescript-eslint/no-explicit-any */
// 工作流列表页：后端返回的 Dify 风格图结构为动态 schema，暂用 any 承载。
// TODO(1.1): 与 WorkflowView 一起引入节点数据类型，移除本豁免。
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { apiClient } from '../api/client'
import { useLanguage } from '../composables/useLanguage'

const { t } = useLanguage()
const router = useRouter()

const loading = ref(true)
const error = ref('')
const workflows = ref<any[]>([])

async function fetchList() {
  loading.value = true
  error.value = ''
  try {
    workflows.value = (await apiClient.get('/workflows')) as any[]
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    loading.value = false
  }
}

function nodeCount(wf: any): number {
  return (wf.graph?.nodes || []).length
}

function openWf(id: string) {
  router.push(`/workflows/${id}`)
}
function createWf() {
  router.push('/workflows/new')
}
async function removeWf(wf: any) {
  if (!window.confirm(t.value('workflows.confirmDelete'))) return
  try {
    await apiClient.delete(`/workflows/${wf.id}`)
    await fetchList()
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  }
}

onMounted(fetchList)
</script>

<template>
  <div class="wf-list-page">
    <router-link to="/chat" class="wf-back">← {{ t('insights.back') }}</router-link>
    <header class="wf-list-head">
      <div class="wf-list-titles">
        <h1 class="wf-list-title">{{ t('workflows.list') }}</h1>
        <p class="wf-list-sub">{{ t('workflows.title') }}</p>
      </div>
      <button class="btn-primary" @click="createWf">{{ t('workflows.new') }}</button>
    </header>

    <div v-if="error" class="wf-list-error">{{ error }}</div>

    <div v-if="loading" class="wf-list-state">{{ t('wfEditor.loading') }}</div>

    <div v-else-if="!workflows.length" class="wf-list-empty">
      <div class="wf-empty-icon">🔀</div>
      <p class="wf-empty-text">{{ t('workflows.empty') }}</p>
      <button class="btn-primary" @click="createWf">{{ t('workflows.new') }}</button>
    </div>

    <div v-else class="wf-grid">
      <div
        v-for="wf in workflows"
        :key="wf.id"
        class="wf-card"
        @click="openWf(wf.id)"
      >
        <div class="wf-card-main">
          <div class="wf-card-name">{{ wf.name }}</div>
          <div class="wf-card-desc">{{ wf.description || '—' }}</div>
          <div class="wf-card-meta">
            <span>{{ t('workflows.nodes').replace('{0}', String(nodeCount(wf))) }}</span>
            <span class="wf-card-time">{{ t('workflows.createdAt').replace('{0}', wf.created_at || '') }}</span>
          </div>
        </div>
        <div class="wf-card-actions" @click.stop>
          <button class="btn-ghost btn-sm" @click="openWf(wf.id)">{{ t('workflows.edit') }}</button>
          <button class="btn-ghost btn-sm wf-del" @click="removeWf(wf)">{{ t('wfEditor.delete') }}</button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.wf-list-page {
  max-width: 1080px;
  margin: 0 auto;
  padding: var(--space-xl) var(--space-lg);
}
.wf-back {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: var(--font-size-sm);
  color: var(--color-text-secondary);
  text-decoration: none;
  margin-bottom: var(--space-md);
}
.wf-back:hover {
  color: var(--color-primary);
}
.wf-list-head {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: var(--space-md);
  margin-bottom: var(--space-lg);
}
.wf-list-title {
  font-size: var(--font-size-2xl, 1.6rem);
  font-weight: 700;
  color: var(--color-text);
  margin: 0;
}
.wf-list-sub {
  margin: 4px 0 0;
  font-size: var(--font-size-sm);
  color: var(--color-text-secondary);
}
.wf-list-error {
  margin-bottom: var(--space-md);
  padding: var(--space-sm) var(--space-md);
  border: 1px solid var(--border-color);
  border-radius: var(--radius-md);
  background: var(--color-error-bg, #fef2f2);
  color: var(--color-danger, #dc2626);
  font-size: var(--font-size-sm);
}
.wf-list-state {
  padding: var(--space-xl) 0;
  text-align: center;
  color: var(--color-text-tertiary);
  font-size: var(--font-size-sm);
}
.wf-list-empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: var(--space-md);
  padding: var(--space-2xl, 64px) var(--space-lg);
  border: 1px dashed var(--border-color);
  border-radius: var(--radius-lg);
  background: var(--bg-form);
  text-align: center;
}
.wf-empty-icon {
  font-size: 2.4rem;
  opacity: 0.6;
}
.wf-empty-text {
  margin: 0;
  color: var(--color-text-secondary);
  max-width: 420px;
}
.wf-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: var(--space-md);
}
.wf-card {
  display: flex;
  flex-direction: column;
  justify-content: space-between;
  gap: var(--space-sm);
  padding: var(--space-md);
  border: 1px solid var(--border-color);
  border-radius: var(--radius-lg);
  background: var(--bg-surface);
  cursor: pointer;
  transition: border-color 0.15s ease, box-shadow 0.15s ease, transform 0.15s ease;
}
.wf-card:hover {
  border-color: var(--color-primary);
  box-shadow: var(--shadow-md, 0 4px 16px rgba(0, 0, 0, 0.08));
  transform: translateY(-2px);
}
.wf-card-main { min-width: 0; }
.wf-card-name {
  font-size: var(--font-size-md, 1rem);
  font-weight: 600;
  color: var(--color-text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.wf-card-desc {
  margin-top: 4px;
  font-size: var(--font-size-sm);
  color: var(--color-text-secondary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.wf-card-meta {
  margin-top: var(--space-sm);
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-md);
  font-size: var(--font-size-xs);
  color: var(--color-text-tertiary);
}
.wf-card-actions {
  display: flex;
  gap: var(--space-xs);
  justify-content: flex-end;
}
.wf-del { color: var(--color-danger, #dc2626); }
.wf-del:hover { border-color: var(--color-danger, #dc2626); color: var(--color-danger, #dc2626); }
</style>
