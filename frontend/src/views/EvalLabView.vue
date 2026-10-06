<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { apiClient } from '../api/client'
import { useProviderStore } from '../stores/providers'
import { useLanguage } from '../composables/useLanguage'
import { modelsForProvider } from '../utils/modelCatalog'

const { t } = useLanguage()
const providerStore = useProviderStore()

// ── 数据集 ──────────────────────────────────────
interface Dataset {
  id: string
  name: string
  description: string
  cases: { id: string; prompt: string; expect_keywords?: string[]; expect_tools?: string[]; forbid_tools?: string[]; tags?: string[] }[]
  case_count: number
  created_at: string
}

const datasets = ref<Dataset[]>([])
const creatingDataset = ref(false)
const dsName = ref('')
const dsDesc = ref('')
const dsCasesJson = ref(
  JSON.stringify(
    [
      { id: 'greet', prompt: '你好，请用一句话介绍你自己。', expect_keywords: ['你好'], tags: ['chat'] },
      { id: 'calc', prompt: '请用计算器算 123*456。', expect_keywords: ['56088'], expect_tools: ['calculator'], tags: ['tool'] },
    ],
    null,
    2,
  ),
)
const dsError = ref('')

async function loadDatasets() {
  datasets.value = await apiClient.get<Dataset[]>('/evals/datasets')
}

async function createDataset() {
  dsError.value = ''
  let cases: unknown
  try {
    cases = JSON.parse(dsCasesJson.value)
  } catch {
    dsError.value = t.value('evals.casesJsonInvalid')
    return
  }
  try {
    await apiClient.post('/evals/datasets', { name: dsName.value, description: dsDesc.value, cases })
    creatingDataset.value = false
    dsName.value = ''
    await loadDatasets()
  } catch (e) {
    dsError.value = e instanceof Error ? e.message : String(e)
  }
}

async function deleteDataset(id: string) {
  if (!window.confirm(t.value('evals.confirmDeleteDataset'))) return
  await apiClient.delete(`/evals/datasets/${id}`)
  await loadDatasets()
}

// ── 对比矩阵与运行 ────────────────────────────────
interface Combo {
  provider_id: string
  model: string
  system_prompt: string
}
interface RunSummary {
  provider_id: string
  model: string
  status: string
  pass_rate?: number
  total?: number
  passed?: number
}
interface EvalRun {
  id: string
  dataset_id: string
  status: string
  error?: string
  created_at: string
  finished_at?: string | null
  summary: RunSummary[]
  results?: {
    provider_id: string
    model: string
    status: string
    pass_rate?: number
    total?: number
    passed?: number
    failed?: number
    avg_latency_ms?: number
    error?: string
    cases?: { id: string; passed: boolean; error?: string | null }[]
  }[]
}

const combos = ref<Combo[]>([])
const selectedDatasetId = ref('')
const runs = ref<EvalRun[]>([])
const runDetail = ref<EvalRun | null>(null)
const runError = ref('')
let pollTimer: ReturnType<typeof setInterval> | null = null

const usableProviders = computed(() => providerStore.providers.filter((p) => p.enabled !== false && p.has_api_key))
const selectedDataset = computed(() => datasets.value.find((d) => d.id === selectedDatasetId.value))

/** 目录化模型选项（与对话页同一份目录：DeepSeek 只显示两款） */
function modelOptions(providerId: string) {
  const p = providerStore.providers.find((x) => x.id === providerId)
  return modelsForProvider(providerId, p?.models || [])
}
function onComboProviderChange(c: Combo) {
  const opts = modelOptions(c.provider_id)
  if (opts.length && !opts.some((o) => o.id === c.model)) {
    c.model = opts[0].id
  }
}

// 数据集内容查看（点击展开）
const expandedDatasetId = ref('')
const datasetDetails = ref<Record<string, Dataset>>({})
const detailLoading = ref('')

async function toggleDatasetDetail(id: string) {
  if (expandedDatasetId.value === id) {
    expandedDatasetId.value = ''
    return
  }
  expandedDatasetId.value = id
  if (!datasetDetails.value[id]) {
    detailLoading.value = id
    try {
      datasetDetails.value[id] = await apiClient.get<Dataset>(`/evals/datasets/${id}`)
    } finally {
      detailLoading.value = ''
    }
  }
}

function addCombo() {
  const p = usableProviders.value[0]
  combos.value.push({
    provider_id: p?.id || 'deepseek',
    model: p?.models?.[0] || 'deepseek-flash',
    system_prompt: '',
  })
}
function removeCombo(i: number) {
  combos.value.splice(i, 1)
}

async function loadRuns() {
  runs.value = await apiClient.get<EvalRun[]>('/evals/runs')
  // 有运行中任务时保持轮询
  const anyRunning = runs.value.some((r) => r.status === 'running')
  if (anyRunning && !pollTimer) {
    pollTimer = setInterval(async () => {
      await loadRuns()
      if (runDetail.value) {
        runDetail.value = await apiClient.get<EvalRun>(`/evals/runs/${runDetail.value.id}`)
      }
    }, 2000)
  }
  if (!anyRunning && pollTimer) {
    clearInterval(pollTimer)
    pollTimer = null
  }
}

async function startRun() {
  runError.value = ''
  if (!selectedDatasetId.value) {
    runError.value = t.value('evals.pickDataset')
    return
  }
  if (combos.value.length === 0) {
    runError.value = t.value('evals.pickCombos')
    return
  }
  try {
    const run = await apiClient.post<EvalRun>('/evals/runs', {
      dataset_id: selectedDatasetId.value,
      combos: combos.value,
    })
    combos.value = []
    runDetail.value = run
    await loadRuns()
  } catch (e) {
    runError.value = e instanceof Error ? e.message : String(e)
  }
}

async function openRun(id: string) {
  runDetail.value = await apiClient.get<EvalRun>(`/evals/runs/${id}`)
}

onMounted(async () => {
  providerStore.loadProviders()
  await loadDatasets()
  await loadRuns()
  if (combos.value.length === 0) addCombo()
})
onUnmounted(() => {
  if (pollTimer) clearInterval(pollTimer)
})
</script>

<template>
  <div class="evals-view">
    <header class="evals-header">
      <router-link to="/chat" class="evals-back">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="15 18 9 12 15 6" /></svg>
        {{ t('insights.back') }}
      </router-link>
      <h2 class="evals-title">{{ t('evals.title') }}</h2>
      <p class="evals-sub">{{ t('evals.subtitle') }}</p>
    </header>

    <!-- 数据集 -->
    <section class="card eval-section">
      <div class="section-head">
        <h3 class="section-title">{{ t('evals.datasets') }}</h3>
        <button class="btn-ghost btn-sm" @click="creatingDataset = !creatingDataset">
          {{ creatingDataset ? t('providers.cancel') : t('evals.newDataset') }}
        </button>
      </div>
      <div v-if="creatingDataset" class="ds-form">
        <input v-model="dsName" class="form-input" :placeholder="t('evals.datasetName')" />
        <input v-model="dsDesc" class="form-input" :placeholder="t('evals.datasetDesc')" />
        <textarea v-model="dsCasesJson" class="form-input ds-cases" rows="8" spellcheck="false"></textarea>
        <p v-if="dsError" class="evals-error">{{ dsError }}</p>
        <button class="btn-primary btn-sm" @click="createDataset">{{ t('evals.saveDataset') }}</button>
      </div>
      <ul v-if="datasets.length" class="ds-list">
        <li
          v-for="d in datasets"
          :key="d.id"
          :class="['ds-item', { active: selectedDatasetId === d.id }]"
        >
          <div class="ds-main" @click="selectedDatasetId = d.id">
            <div class="ds-info">
              <span class="ds-name">{{ d.name }}</span>
              <span class="ds-count">{{ t('evals.caseCount').replace('{0}', String(d.case_count)) }}</span>
            </div>
          </div>
          <div class="ds-actions">
            <button class="btn-ghost btn-xs" @click.stop="toggleDatasetDetail(d.id)">
              {{ expandedDatasetId === d.id ? t('evals.hideCases') : t('evals.viewCases') }}
            </button>
            <button class="btn-ghost btn-xs" @click.stop="deleteDataset(d.id)">{{ t('providers.delete') }}</button>
          </div>
          <!-- 数据集内容：用例明细 -->
          <div v-if="expandedDatasetId === d.id" class="ds-detail" @click.stop>
            <p v-if="detailLoading === d.id" class="empty-hint">{{ t('insights.loading') }}</p>
            <template v-else-if="datasetDetails[d.id]">
              <div
                v-for="c in datasetDetails[d.id].cases"
                :key="c.id"
                class="ds-case"
              >
                <div class="ds-case-head">
                  <code class="ds-case-id">{{ c.id }}</code>
                  <span v-for="tag in c.tags || []" :key="tag" class="ds-case-tag">{{ tag }}</span>
                </div>
                <p class="ds-case-prompt">{{ c.prompt }}</p>
                <p class="ds-case-expect">
                  <template v-if="c.expect_keywords?.length">{{ t('evals.expectKeywords') }}: {{ c.expect_keywords.join(' / ') }}</template>
                  <template v-if="c.expect_tools?.length"> · {{ t('evals.expectTools') }}: {{ c.expect_tools.join(', ') }}</template>
                  <template v-if="c.forbid_tools?.length"> · {{ t('evals.forbidTools') }}: {{ c.forbid_tools.join(', ') }}</template>
                </p>
              </div>
            </template>
          </div>
        </li>
      </ul>
      <p v-else class="empty-hint">{{ t('evals.noDatasets') }}</p>
    </section>

    <!-- 对比矩阵 -->
    <section class="card eval-section">
      <div class="section-head">
        <h3 class="section-title">{{ t('evals.matrix') }}</h3>
        <button class="btn-ghost btn-sm" @click="addCombo">{{ t('evals.addCombo') }}</button>
      </div>
      <p v-if="!selectedDataset" class="empty-hint">{{ t('evals.pickDatasetFirst') }}</p>
      <div v-for="(c, i) in combos" :key="i" class="combo-row">
        <select v-model="c.provider_id" class="form-input combo-provider" @change="onComboProviderChange(c)">
          <option v-for="p in usableProviders" :key="p.id" :value="p.id">{{ p.name }}</option>
        </select>
        <select v-model="c.model" class="form-input combo-model">
          <option v-for="o in modelOptions(c.provider_id)" :key="o.id" :value="o.id">{{ o.label }}</option>
        </select>
        <input v-model="c.system_prompt" class="form-input combo-sp" :placeholder="t('evals.systemPromptPh')" />
        <button class="btn-ghost btn-xs" :disabled="combos.length <= 1" @click="removeCombo(i)">✕</button>
      </div>
      <p v-if="runError" class="evals-error">{{ runError }}</p>
      <button
        class="btn-primary btn-sm run-btn"
        :disabled="!selectedDataset || combos.length === 0"
        @click="startRun"
      >
        {{ t('evals.startRun') }}
      </button>
    </section>

    <!-- 运行历史 -->
    <section class="card eval-section">
      <div class="section-head">
        <h3 class="section-title">{{ t('evals.runs') }}</h3>
      </div>
      <ul v-if="runs.length" class="runs-list">
        <li v-for="r in runs" :key="r.id" class="run-item" @click="openRun(r.id)">
          <span class="run-time">{{ r.created_at.replace('T', ' ').slice(0, 16) }}</span>
          <span :class="['run-status', 'st-' + r.status]">
            {{ r.status === 'running' ? t('evals.statusRunning') : r.status === 'done' ? t('evals.statusDone') : t('evals.statusError') }}
          </span>
          <span v-for="(s, i) in r.summary" :key="i" class="run-chip">
            {{ s.model || s.provider_id }} · {{ s.pass_rate != null ? Math.round((s.pass_rate || 0) * 100) + '%' : '…' }}
          </span>
        </li>
      </ul>
      <p v-else class="empty-hint">{{ t('evals.noRuns') }}</p>
    </section>

    <!-- 运行详情 -->
    <section v-if="runDetail" class="card eval-section">
      <div class="section-head">
        <h3 class="section-title">{{ t('evals.runDetail') }}</h3>
        <button class="btn-ghost btn-xs" @click="runDetail = null">✕</button>
      </div>
      <div v-for="(res, i) in runDetail.results" :key="i" class="result-card">
        <div class="result-head">
          <strong>{{ res.model || res.provider_id }}</strong>
          <span v-if="res.status === 'done'" class="result-metrics">
            {{ t('evals.passRate') }} <strong>{{ Math.round((res.pass_rate || 0) * 100) }}%</strong>
            （{{ res.passed }}/{{ res.total }}）· {{ t('evals.avgLatency') }} {{ res.avg_latency_ms }}ms
          </span>
          <span v-else-if="res.status === 'running'" class="run-status st-running">{{ t('evals.statusRunning') }}</span>
          <span v-else class="run-status st-error">{{ res.error }}</span>
        </div>
        <div v-if="res.cases" class="case-grid">
          <div
            v-for="c in res.cases"
            :key="c.id"
            :class="['case-cell', c.passed ? 'case-pass' : 'case-fail']"
            :title="c.error || ''"
          >
            {{ c.passed ? '✓' : '✗' }} {{ c.id }}
          </div>
        </div>
      </div>
    </section>
  </div>
</template>

<style scoped>
.evals-view {
  min-height: 100vh;
  background: var(--bg-main);
  padding: var(--space-lg);
  max-width: 1080px;
  margin: 0 auto;
}
.evals-header {
  display: flex;
  align-items: baseline;
  gap: var(--space-md);
  margin-bottom: var(--space-lg);
}
.evals-back {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: var(--font-size-sm);
  color: var(--color-text-secondary);
  text-decoration: none;
}
.evals-back:hover { color: var(--color-primary); }
.evals-title { margin: 0; font-size: var(--font-size-lg); font-weight: 700; color: var(--color-text); }
.evals-sub { margin: 0; font-size: var(--font-size-xs); color: var(--color-text-tertiary); }
.eval-section { padding: var(--space-md) var(--space-lg); margin-bottom: var(--space-lg); }
.section-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: var(--space-sm); }
.section-title { margin: 0; font-size: var(--font-size-md); font-weight: 700; color: var(--color-text); }
.ds-form { display: flex; flex-direction: column; gap: var(--space-sm); margin-bottom: var(--space-md); }
.ds-cases { font-family: var(--font-mono, monospace); font-size: var(--font-size-xs); }
.ds-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 6px; }
.ds-item {
  display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap;
  padding: 8px 12px; border: 1px solid var(--border-color); border-radius: var(--radius-md);
  transition: var(--transition-base);
}
.ds-item:hover { border-color: var(--color-primary); }
.ds-item.active { border-color: var(--color-primary); background: var(--color-primary-light); }
.ds-main { cursor: pointer; flex: 1; min-width: 0; }
.ds-info { display: flex; gap: var(--space-sm); align-items: baseline; }
.ds-name { font-weight: 600; color: var(--color-text); }
.ds-count { font-size: var(--font-size-xs); color: var(--color-text-secondary); }
.ds-actions { display: flex; gap: 6px; }
/* 数据集用例明细 */
.ds-detail { flex-basis: 100%; margin-top: 8px; display: flex; flex-direction: column; gap: 8px; }
.ds-case { padding: 8px 10px; border: 1px solid var(--border-light); border-radius: var(--radius-sm); background: var(--bg-surface); }
.ds-case-head { display: flex; gap: 6px; align-items: center; margin-bottom: 4px; }
.ds-case-id { font-family: var(--font-mono, monospace); font-size: var(--font-size-xs); color: var(--color-primary); background: var(--bg-tag); padding: 1px 6px; border-radius: var(--radius-sm); }
.ds-case-tag { font-size: 11px; color: var(--color-text-tertiary); background: var(--bg-tag); padding: 1px 6px; border-radius: var(--radius-sm); }
.ds-case-prompt { margin: 0 0 4px; font-size: var(--font-size-sm); color: var(--color-text); white-space: pre-wrap; word-break: break-word; }
.ds-case-expect { margin: 0; font-size: 11px; color: var(--color-text-tertiary); }
.combo-row { display: flex; gap: var(--space-sm); align-items: center; margin-bottom: var(--space-sm); }
.combo-provider { width: 180px; flex-shrink: 0; }
.combo-model { width: 220px; flex-shrink: 0; }
.combo-sp { flex: 1; }
.run-btn { margin-top: var(--space-sm); }
.runs-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 6px; }
.run-item {
  display: flex; align-items: center; gap: var(--space-sm); flex-wrap: wrap;
  padding: 8px 12px; border: 1px solid var(--border-color); border-radius: var(--radius-md);
  cursor: pointer; transition: var(--transition-base); font-size: var(--font-size-sm);
}
.run-item:hover { border-color: var(--color-primary); }
.run-time { color: var(--color-text-tertiary); font-size: var(--font-size-xs); font-variant-numeric: tabular-nums; }
.run-status { font-weight: 600; font-size: var(--font-size-xs); padding: 2px 8px; border-radius: var(--radius-full); }
.st-running { color: var(--color-warning); background: var(--color-warning-light); }
.st-done { color: var(--color-success, #16a34a); background: var(--color-success-light); }
.st-error { color: var(--color-danger); background: var(--color-danger-light); }
.run-chip { font-size: var(--font-size-xs); color: var(--color-text-secondary); background: var(--bg-tag); padding: 2px 8px; border-radius: var(--radius-sm); }
.result-card { margin-bottom: var(--space-md); }
.result-head { display: flex; align-items: baseline; gap: var(--space-md); margin-bottom: var(--space-xs); flex-wrap: wrap; }
.result-metrics { font-size: var(--font-size-sm); color: var(--color-text-secondary); }
.case-grid { display: flex; flex-wrap: wrap; gap: 6px; }
.case-cell { font-size: var(--font-size-xs); padding: 3px 10px; border-radius: var(--radius-sm); font-family: var(--font-mono, monospace); }
.case-pass { color: var(--color-success, #16a34a); background: var(--color-success-light); }
.case-fail { color: var(--color-danger); background: var(--color-danger-light); }
.evals-error { color: var(--color-danger); font-size: var(--font-size-sm); margin: var(--space-xs) 0; }
.empty-hint { color: var(--color-text-tertiary); font-size: var(--font-size-sm); padding: var(--space-sm) 0; }
</style>
