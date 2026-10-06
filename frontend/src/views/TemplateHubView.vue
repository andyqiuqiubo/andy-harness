<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { apiClient } from '../api/client'
import { useChatStore } from '../stores/chat'
import { useLanguage } from '../composables/useLanguage'

const { t } = useLanguage()
const router = useRouter()
const chatStore = useChatStore()

interface Template {
  id: string
  type: 'session' | 'prompt' | 'workflow'
  name: string
  description: string
  vars: string[]
  created_at: string
}

const templates = ref<Template[]>([])
const typeFilter = ref<string>('')
const loading = ref(false)
const loadError = ref('')
const showGuide = ref(true)

// 导入分享包
const importName = ref('')
const importType = ref<'session' | 'prompt' | 'workflow'>('prompt')
const importPayload = ref('')
const importError = ref('')

// 实例化弹层
const usingTemplate = ref<Template | null>(null)
const usingVars = ref<Record<string, string>>({})
const usingError = ref('')

const filtered = computed(() =>
  typeFilter.value ? templates.value.filter((x) => x.type === typeFilter.value) : templates.value,
)

const TYPE_LABEL: Record<string, string> = {
  session: '会话',
  prompt: '提示词',
  workflow: '工作流',
}

async function load() {
  loading.value = true
  loadError.value = ''
  try {
    templates.value = await apiClient.get<Template[]>('/templates')
  } catch (e) {
    loadError.value = e instanceof Error ? e.message : String(e)
  } finally {
    loading.value = false
  }
}

/** 填入示例：按当前选中的模板类型，给出贴合该类型的示例 payload */
function fillExample() {
  const type = importType.value
  if (type === 'session') {
    importName.value = t.value('templates.exNameSession')
    importPayload.value = JSON.stringify(
      {
        messages: [
          { role: 'system', content: t.value('templates.exSessionSystem') },
          { role: 'user', content: t.value('templates.exSessionUser') },
        ],
      },
      null,
      2,
    )
  } else if (type === 'workflow') {
    importName.value = t.value('templates.exNameWorkflow')
    const varName = t.value('templates.exWfVar')
    const graph = {
      nodes: [
        {
          id: 'start',
          type: 'start',
          position: { x: 0, y: 0 },
          data: { variables: [{ name: varName, type: 'string', default: '' }] },
        },
        {
          id: 'llm1',
          type: 'llm',
          position: { x: 320, y: 0 },
          data: {
            provider_id: 'deepseek',
            model: 'deepseek-flash',
            system_prompt: '',
            user_prompt: t.value('templates.exWfPrompt'),
          },
        },
        {
          id: 'end1',
          type: 'end',
          position: { x: 640, y: 0 },
          data: { outputs: [{ name: 'result', value: '{{llm1.text}}' }] },
        },
      ],
      edges: [
        { id: 'e1', source: 'start', target: 'llm1', sourceHandle: 'out', targetHandle: 'in' },
        { id: 'e2', source: 'llm1', target: 'end1', sourceHandle: 'out', targetHandle: 'in' },
      ],
    }
    importPayload.value = JSON.stringify({ vars: [varName], graph }, null, 2)
  } else {
    importName.value = t.value('templates.exNamePrompt')
    importPayload.value = JSON.stringify(
      {
        messages: [
          { role: 'user', content: t.value('templates.exPromptContent') },
        ],
      },
      null,
      2,
    )
  }
}

async function importTemplate() {
  importError.value = ''
  let payload: unknown
  try {
    payload = JSON.parse(importPayload.value)
  } catch {
    importError.value = t.value('templates.payloadJsonInvalid')
    return
  }
  try {
    await apiClient.post('/templates', {
      type: importType.value,
      name: importName.value,
      payload,
    })
    importName.value = ''
    await load()
  } catch (e) {
    importError.value = e instanceof Error ? e.message : String(e)
  }
}

function openUse(tpl: Template) {
  usingTemplate.value = tpl
  usingVars.value = Object.fromEntries(tpl.vars.map((v) => [v, '']))
  usingError.value = ''
}

async function confirmUse() {
  if (!usingTemplate.value) return
  usingError.value = ''
  try {
    const res = await apiClient.post<{ type: string; session_id?: string; workflow_id?: string; pending_message?: string | null }>(
      `/templates/${usingTemplate.value.id}/instantiate`,
      { vars: usingVars.value },
    )
    usingTemplate.value = null
    await load()
    if (res.session_id) {
      // 末尾提问经正常发送链路发出（会话页 WS 就绪后自动发送），才有流式回答
      chatStore.pendingAutoSend = res.pending_message || ''
      chatStore.pendingAutoSendSession = res.pending_message ? res.session_id : null
      await chatStore.selectSession(res.session_id)
      void router.push('/chat')
    } else if (res.workflow_id) {
      // 工作流模板落地后跳转到工作流列表，让用户直接看到新建的工作流
      void router.push('/workflows')
    }
  } catch (e) {
    usingError.value = e instanceof Error ? e.message : String(e)
  }
}

async function removeTemplate(id: string) {
  if (!window.confirm(t.value('templates.confirmDelete'))) return
  await apiClient.delete(`/templates/${id}`)
  await load()
}

onMounted(load)
</script>

<template>
  <div class="tpl-view">
    <header class="tpl-header">
      <router-link to="/chat" class="tpl-back">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="15 18 9 12 15 6" /></svg>
        {{ t('insights.back') }}
      </router-link>
      <h2 class="tpl-title">{{ t('templates.title') }}</h2>
      <p class="tpl-sub">{{ t('templates.subtitle') }}</p>
    </header>

    <div v-if="loading" class="empty-hint">{{ t('insights.loading') }}</div>
    <div v-else-if="loadError" class="tpl-error">{{ loadError }}</div>

    <template v-else>
      <!-- 使用示例：让第一次来的用户知道完整玩法 -->
      <section class="card tpl-guide">
        <div class="section-head">
          <h3 class="section-title">📖 {{ t('templates.guideTitle') }}</h3>
          <button class="btn-ghost btn-xs" @click="showGuide = !showGuide">
            {{ showGuide ? t('evals.hideCases') : t('evals.viewCases') }}
          </button>
        </div>
        <ol v-if="showGuide" class="guide-steps">
          <li>{{ t('templates.guideStep1') }}</li>
          <li>{{ t('templates.guideStep2') }}</li>
          <li>{{ t('templates.guideStep3') }}</li>
        </ol>
        <div v-if="showGuide" class="guide-example">
          <p class="guide-example-title">{{ t('templates.guideExampleTitle') }}</p>
          <pre class="guide-example-body">{{ t('templates.guideExampleBody') }}</pre>
        </div>
      </section>

      <!-- 导入模板（唯一创建入口） -->
      <section class="card tpl-section">
        <div class="section-head">
          <h3 class="section-title">{{ t('templates.import') }}</h3>
          <button class="btn-ghost btn-xs" @click="fillExample">{{ t('templates.fillExample') }}</button>
        </div>
        <div class="tpl-import-form">
          <div class="import-row">
            <select v-model="importType" class="form-input import-type">
              <option value="prompt">{{ t('templates.typePrompt') }}</option>
              <option value="session">{{ t('templates.typeSession') }}</option>
              <option value="workflow">{{ t('templates.typeWorkflow') }}</option>
            </select>
            <input v-model="importName" class="form-input" :placeholder="t('templates.namePh')" />
          </div>
          <textarea v-model="importPayload" class="form-input tpl-payload" rows="8" spellcheck="false" :placeholder="t('templates.payloadPh')"></textarea>
          <p class="tpl-hint">{{ t('templates.payloadHint') }}</p>
          <p v-if="importError" class="tpl-error">{{ importError }}</p>
          <button class="btn-primary btn-sm" :disabled="!importName.trim()" @click="importTemplate">
            {{ t('templates.importBtn') }}
          </button>
        </div>
      </section>

      <!-- 模板列表 -->
      <section class="card tpl-section">
        <div class="section-head">
          <h3 class="section-title">{{ t('templates.list') }}</h3>
          <select v-model="typeFilter" class="form-input tpl-filter">
            <option value="">{{ t('templates.allTypes') }}</option>
            <option value="session">{{ t('templates.typeSession') }}</option>
            <option value="prompt">{{ t('templates.typePrompt') }}</option>
            <option value="workflow">{{ t('templates.typeWorkflow') }}</option>
          </select>
        </div>
        <div v-if="filtered.length" class="tpl-grid">
          <div v-for="tpl in filtered" :key="tpl.id" class="tpl-card">
            <div class="tpl-card-head">
              <span :class="['tpl-type', 'type-' + tpl.type]">{{ TYPE_LABEL[tpl.type] || tpl.type }}</span>
              <button class="btn-ghost btn-xs" @click="removeTemplate(tpl.id)">✕</button>
            </div>
            <h4 class="tpl-name">{{ tpl.name }}</h4>
            <p v-if="tpl.description" class="tpl-desc">{{ tpl.description }}</p>
            <div v-if="tpl.vars.length" class="tpl-vars">
              <span v-for="v in tpl.vars" :key="v" class="tpl-var" v-text="'{{' + v + '}}'"></span>
            </div>
            <button class="btn-primary btn-sm tpl-use" @click="openUse(tpl)">
              {{ t('templates.use') }}
            </button>
          </div>
        </div>
        <p v-else class="empty-hint">{{ t('templates.empty') }}</p>
      </section>

      <!-- 实例化弹层 -->
      <div v-if="usingTemplate" class="tpl-overlay" @click.self="usingTemplate = null">
        <div class="tpl-dialog">
          <h3 class="tpl-dialog-title">{{ t('templates.useTitle').replace('{0}', usingTemplate.name) }}</h3>
          <div v-if="usingTemplate.vars.length" class="tpl-dialog-vars">
            <div v-for="v in usingTemplate.vars" :key="v" class="form-row">
              <label class="form-label">{{ v }}</label>
              <input v-model="usingVars[v]" class="form-input" />
            </div>
          </div>
          <p v-else class="empty-hint">{{ t('templates.noVars') }}</p>
          <p v-if="usingError" class="tpl-error">{{ usingError }}</p>
          <div class="tpl-dialog-actions">
            <button class="btn-ghost" @click="usingTemplate = null">{{ t('providers.cancel') }}</button>
            <button class="btn-primary" @click="confirmUse">{{ t('templates.use') }}</button>
          </div>
        </div>
      </div>
    </template>
  </div>
</template>

<style scoped>
.tpl-view { min-height: 100vh; background: var(--bg-main); padding: var(--space-lg); max-width: 1080px; margin: 0 auto; }
.tpl-header { display: flex; align-items: baseline; gap: var(--space-md); margin-bottom: var(--space-lg); flex-wrap: wrap; }
.tpl-back { display: inline-flex; align-items: center; gap: 4px; font-size: var(--font-size-sm); color: var(--color-text-secondary); text-decoration: none; }
.tpl-back:hover { color: var(--color-primary); }
.tpl-title { margin: 0; font-size: var(--font-size-lg); font-weight: 700; color: var(--color-text); }
.tpl-sub { margin: 0; font-size: var(--font-size-xs); color: var(--color-text-tertiary); }
.tpl-section { padding: var(--space-md) var(--space-lg); margin-bottom: var(--space-lg); }
/* 使用示例卡 */
.tpl-guide { padding: var(--space-md) var(--space-lg); margin-bottom: var(--space-lg); border-left: 3px solid var(--color-primary); }
.guide-steps { margin: var(--space-sm) 0; padding-left: var(--space-lg); color: var(--color-text); font-size: var(--font-size-sm); line-height: 1.9; }
.guide-example { margin-top: var(--space-sm); }
.guide-example-title { margin: 0 0 6px; font-size: var(--font-size-xs); font-weight: 700; color: var(--color-text-secondary); }
.guide-example-body {
  margin: 0; padding: var(--space-sm) var(--space-md);
  background: var(--bg-code-block, var(--bg-input)); border: 1px solid var(--border-color);
  border-radius: var(--radius-md); font-family: var(--font-mono, monospace);
  font-size: var(--font-size-xs); line-height: 1.7; color: var(--color-text);
  white-space: pre-wrap; word-break: break-word;
}
.section-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: var(--space-sm); }
.section-title { margin: 0; font-size: var(--font-size-md); font-weight: 700; color: var(--color-text); }
.tpl-create-grid { display: grid; grid-template-columns: 1fr 1fr; gap: var(--space-lg); }
.tpl-import-form { display: flex; flex-direction: column; gap: var(--space-sm); }
.import-row { display: flex; gap: var(--space-sm); }
.import-type { width: 170px; flex-shrink: 0; }
.tpl-hint { margin: 0; font-size: 11px; line-height: 1.7; color: var(--color-text-tertiary); }
.tpl-payload { font-family: var(--font-mono, monospace); font-size: var(--font-size-xs); }
.tpl-filter { width: 140px; }
.tpl-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(240px, 1fr)); gap: var(--space-md); }
.tpl-card { border: 1px solid var(--border-color); border-radius: var(--radius-md); padding: var(--space-md); display: flex; flex-direction: column; gap: 8px; }
.tpl-card-head { display: flex; align-items: center; justify-content: space-between; }
.tpl-type { font-size: var(--font-size-xs); font-weight: 700; padding: 2px 10px; border-radius: var(--radius-full); }
.type-session { color: var(--color-primary); background: var(--color-primary-light); }
.type-prompt { color: var(--color-success, #16a34a); background: var(--color-success-light); }
.type-workflow { color: var(--color-warning); background: var(--color-warning-light); }
.tpl-name { margin: 0; font-size: var(--font-size-sm); font-weight: 700; color: var(--color-text); }
.tpl-desc { margin: 0; font-size: var(--font-size-xs); color: var(--color-text-secondary); }
.tpl-vars { display: flex; flex-wrap: wrap; gap: 4px; }
.tpl-var { font-size: 11px; font-family: var(--font-mono, monospace); color: var(--color-primary); background: var(--bg-tag); padding: 1px 6px; border-radius: var(--radius-sm); }
.tpl-use { margin-top: auto; }
.tpl-overlay { position: fixed; inset: 0; background: rgba(0,0,0,0.6); display: flex; align-items: center; justify-content: center; z-index: 1000; padding: var(--space-md); }
.tpl-dialog { width: min(480px, 100%); background: var(--bg-surface); border: 1px solid var(--border-strong); border-radius: var(--radius-lg); padding: var(--space-lg); box-shadow: var(--shadow-xl); }
.tpl-dialog-title { margin: 0 0 var(--space-md); font-size: var(--font-size-md); color: var(--color-text); }
.tpl-dialog-vars { display: flex; flex-direction: column; gap: var(--space-sm); }
.tpl-dialog-actions { display: flex; justify-content: flex-end; gap: var(--space-sm); margin-top: var(--space-lg); }
.tpl-error { color: var(--color-danger); font-size: var(--font-size-xs); margin: 0; }
.empty-hint { color: var(--color-text-tertiary); font-size: var(--font-size-sm); padding: var(--space-sm) 0; }
@media (max-width: 768px) {
  .tpl-create-grid { grid-template-columns: 1fr; }
}
</style>
