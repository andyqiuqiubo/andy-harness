<script setup lang="ts">
/**
 * 知识库管理页 —— 知识库 / 文档 / 分段 / 检索测试 / 嵌入模型。
 *
 * 对接后端（src/backend/harness/api/rest/knowledge_base.py）：
 * - /api/kb*（管理面）；/api/retrieval（检索）；/api/skills/knowledge_base_search（技能模式）。
 * 设计基调：紧凑、克制——无装饰图标，信息密度优先（与项目既有视图一致）。
 */
import { computed, onMounted, ref } from 'vue'
import { apiClient } from '../api/client'
import { useLanguage } from '../composables/useLanguage'

const { t } = useLanguage()

// ── 类型 ────────────────────────────────────────────────
interface KnowledgeBase {
  id: string
  name: string
  description: string
  embedding_model_id: string | null
  index_type: string
  default_retrieval_mode: string
  need_reindex: number
  doc_count?: number
  segment_count?: number
  vector_count?: number
}

interface KBDocument {
  id: string
  kb_id: string
  name: string
  file_type: string
  storage_stage: string
  status: string
  segment_count: number
  char_count: number
  asset_count: number
  segment_strategy: string
  error: string
  created_at: string
}

interface Segment {
  id: string
  doc_id: string
  kb_id: string
  content: string
  position: number
  parent_id: string | null
  metadata: Record<string, unknown>
  has_embedding: boolean
}

interface SearchSegment {
  segment_id: string | null
  content: string
  score: number
  position: number
  source: { doc_id?: string; doc_name?: string; kb_id?: string; parent_id?: string | null }
  asset_refs: string[]
  truncated: boolean
}

interface QueryAnalysis {
  language: string
  cleaned: string
  entities: string[]
  subqueries: string[]
}

interface SearchResult {
  segments: SearchSegment[]
  total: number
  rerank_applied: boolean
  mode: string
  warn: string
  reason: string
  degraded: boolean
  note: string
  analysis?: QueryAnalysis | null
  skipped?: boolean
}

interface EmbeddingModel {
  id: string
  name: string
  provider: string
  model_name: string
  endpoint: string
  dimensions: number
  batch_size: number
  normalize: number | boolean
  rerank_enabled: number | boolean
  probe_ok: number
  has_api_key: boolean
}

// ── 列表态 ──────────────────────────────────────────────
const loading = ref(false)
const loadError = ref('')
const kbs = ref<KnowledgeBase[]>([])
const flash = ref('')
let flashTimer: ReturnType<typeof setTimeout> | null = null

function setFlash(msg: string) {
  flash.value = msg
  if (flashTimer) clearTimeout(flashTimer)
  flashTimer = setTimeout(() => (flash.value = ''), 2600)
}

function errText(e: unknown): string {
  return e instanceof Error ? e.message : String(e)
}

async function loadKbs() {
  loading.value = true
  loadError.value = ''
  try {
    const res = await apiClient.get<{ knowledge_bases: KnowledgeBase[] }>('/kb')
    kbs.value = res.knowledge_bases || []
  } catch (e) {
    loadError.value = errText(e)
  } finally {
    loading.value = false
  }
}

// ── 新建知识库 ──────────────────────────────────────────
const showCreate = ref(false)
const createForm = ref({ name: '', description: '', default_retrieval_mode: 'keyword' })
const creating = ref(false)
const createError = ref('')

function openCreate() {
  createForm.value = { name: '', description: '', default_retrieval_mode: 'keyword' }
  createError.value = ''
  showCreate.value = true
}

async function submitCreate() {
  if (!createForm.value.name.trim()) {
    createError.value = t.value('kb.needName')
    return
  }
  creating.value = true
  createError.value = ''
  try {
    const kb = await apiClient.post<KnowledgeBase>('/kb', {
      name: createForm.value.name.trim(),
      description: createForm.value.description.trim(),
      default_retrieval_mode: createForm.value.default_retrieval_mode,
    })
    showCreate.value = false
    await loadKbs()
    await openKb(kb)
  } catch (e) {
    createError.value = errText(e)
  } finally {
    creating.value = false
  }
}

// ── 详情态 ──────────────────────────────────────────────
const selected = ref<KnowledgeBase | null>(null)
const documents = ref<KBDocument[]>([])
const docsLoading = ref(false)
const docsError = ref('')

async function openKb(kb: KnowledgeBase) {
  selected.value = kb
  searchResult.value = null
  searchError.value = ''
  segsDoc.value = null
  showModels.value = false
  modelMsg.value = ''
  modelErr.value = ''
  await Promise.all([loadDocuments(), loadModels()])
}

function backToList() {
  selected.value = null
}

async function refreshKb() {
  // 同步统计（文档/分段/向量）与 need_reindex 状态
  const res = await apiClient.get<{ knowledge_bases: KnowledgeBase[] }>('/kb')
  kbs.value = res.knowledge_bases || []
  const fresh = kbs.value.find((k) => k.id === selected.value?.id)
  if (fresh) selected.value = fresh
}

async function deleteKb(kb: KnowledgeBase) {
  if (!window.confirm(t.value('kb.confirmDeleteKb').replace('{0}', kb.name))) return
  try {
    await apiClient.delete(`/kb/${kb.id}`)
    if (selected.value?.id === kb.id) selected.value = null
    await loadKbs()
    setFlash(t.value('kb.saved'))
  } catch (e) {
    setFlash(errText(e))
  }
}

async function updateKbMode(mode: string) {
  if (!selected.value) return
  try {
    const kb = await apiClient.put<KnowledgeBase>(`/kb/${selected.value.id}`, {
      default_retrieval_mode: mode,
    })
    selected.value = { ...selected.value, ...kb }
    setFlash(t.value('kb.saved'))
  } catch (e) {
    setFlash(errText(e))
  }
}

// ── 文档 ────────────────────────────────────────────────
const uploadStrategy = ref('auto')
const uploading = ref(false)
const docsMsg = ref('')
const fileInput = ref<HTMLInputElement | null>(null)

async function loadDocuments() {
  if (!selected.value) return
  docsLoading.value = true
  docsError.value = ''
  try {
    const res = await apiClient.get<{ documents: KBDocument[] }>(`/kb/${selected.value.id}/documents`)
    documents.value = res.documents || []
  } catch (e) {
    docsError.value = errText(e)
  } finally {
    docsLoading.value = false
  }
}

function pickFiles() {
  fileInput.value?.click()
}

async function onFilesPicked(ev: Event) {
  const input = ev.target as HTMLInputElement
  const files = Array.from(input.files || [])
  input.value = ''
  if (!files.length || !selected.value) return
  uploading.value = true
  docsMsg.value = ''
  try {
    const form = new FormData()
    for (const f of files) form.append('files', f, f.name)
    form.append('strategy', uploadStrategy.value)
    const res = await apiClient.uploadForm<{ documents: KBDocument[] }>(
      `/kb/${selected.value.id}/documents`,
      form,
    )
    docsMsg.value = t.value('kb.uploadDone').replace('{0}', String(res.documents.length))
    const failed = res.documents.filter((d) => d.status === 'failed')
    if (failed.length) docsMsg.value += ' ' + failed.map((d) => `${d.name}: ${d.error}`).join('；')
    await Promise.all([loadDocuments(), refreshKb()])
  } catch (e) {
    setFlash(t.value('kb.uploadFailed').replace('{0}', errText(e)))
  } finally {
    uploading.value = false
  }
}

async function deleteDoc(doc: KBDocument) {
  if (!selected.value) return
  if (!window.confirm(t.value('kb.confirmDeleteDoc').replace('{0}', doc.name))) return
  try {
    await apiClient.delete(`/kb/${selected.value.id}/documents/${doc.id}`)
    await Promise.all([loadDocuments(), refreshKb()])
  } catch (e) {
    setFlash(errText(e))
  }
}

async function reindexDoc(doc: KBDocument) {
  if (!selected.value) return
  try {
    await apiClient.post(`/kb/${selected.value.id}/documents/${doc.id}/reindex`)
    await Promise.all([loadDocuments(), refreshKb()])
    setFlash(t.value('kb.saved'))
  } catch (e) {
    setFlash(errText(e))
  }
}

// ── 分段预览 ────────────────────────────────────────────
const segsDoc = ref<KBDocument | null>(null)
const segsLoading = ref(false)
const segs = ref<Segment[]>([])

async function openSegments(doc: KBDocument) {
  if (!selected.value) return
  segsDoc.value = doc
  segsLoading.value = true
  segs.value = []
  try {
    const res = await apiClient.get<{ segments: Segment[] }>(
      `/kb/${selected.value.id}/segments?doc_id=${encodeURIComponent(doc.id)}`,
    )
    segs.value = res.segments || []
  } catch (e) {
    setFlash(errText(e))
    segsDoc.value = null
  } finally {
    segsLoading.value = false
  }
}

function segRoleTag(meta: Record<string, unknown>): string {
  const role = String(meta.role || '')
  if (role === 'parent') return t.value('kb.segParent')
  if (role === 'child') return t.value('kb.segChild')
  if (role === 'qa') return t.value('kb.segQa')
  if (role === 'asset') return t.value('kb.segAsset')
  return ''
}

// ── 检索测试 ────────────────────────────────────────────
const searchForm = ref({
  query: '',
  retrieval_mode: '',
  top_k: 6,
  score_threshold: 0,
  rerank: false,
  skill: false,
})
const searching = ref(false)
const searchResult = ref<SearchResult | null>(null)
const searchError = ref('')

async function doSearch() {
  if (!selected.value || !searchForm.value.query.trim()) return
  searching.value = true
  searchError.value = ''
  searchResult.value = null
  const body: Record<string, unknown> = {
    query: searchForm.value.query.trim(),
    knowledge_base_ids: [selected.value.id],
    top_k: searchForm.value.top_k,
    rerank: searchForm.value.rerank || null,
  }
  if (searchForm.value.retrieval_mode) body.retrieval_mode = searchForm.value.retrieval_mode
  if (searchForm.value.score_threshold > 0) body.score_threshold = searchForm.value.score_threshold
  try {
    const url = searchForm.value.skill ? '/skills/knowledge_base_search' : '/retrieval'
    searchResult.value = await apiClient.post<SearchResult>(url, body)
  } catch (e) {
    searchError.value = errText(e)
  } finally {
    searching.value = false
  }
}

function scoreText(v: number): string {
  return v.toFixed(3)
}

// ── 嵌入模型 ────────────────────────────────────────────
const models = ref<EmbeddingModel[]>([])
const showModels = ref(false)
const modelBindId = ref('')
const modelForm = ref({
  name: '',
  model_name: '',
  endpoint: '',
  api_key: '',
  dimensions: 0,
})
const savingModel = ref(false)
const probing = ref(false)
const modelMsg = ref('')
const modelErr = ref('')

async function loadModels() {
  try {
    const res = await apiClient.get<{ models: EmbeddingModel[] }>('/kb/embedding-models')
    models.value = res.models || []
  } catch {
    models.value = []
  }
  modelBindId.value = selected.value?.embedding_model_id || ''
}

const boundModelName = computed(() => {
  const id = selected.value?.embedding_model_id
  if (!id) return ''
  return models.value.find((m) => m.id === id)?.name || id.slice(0, 8)
})

async function bindModel(unbind = false) {
  if (!selected.value) return
  try {
    const kb = await apiClient.put<KnowledgeBase>(`/kb/${selected.value.id}/embedding-model`, {
      embedding_model_id: unbind ? null : modelBindId.value || null,
    })
    selected.value = { ...selected.value, ...kb }
    setFlash(t.value('kb.saved'))
  } catch (e) {
    setFlash(errText(e))
  }
}

function buildModelPayload() {
  return {
    name: modelForm.value.name.trim(),
    provider: 'openai',
    model_name: modelForm.value.model_name.trim(),
    endpoint: modelForm.value.endpoint.trim(),
    api_key: modelForm.value.api_key,
    dimensions: modelForm.value.dimensions || 0,
    normalize: true,
    batch_size: 32,
  }
}

async function probeModel() {
  probing.value = true
  modelMsg.value = ''
  modelErr.value = ''
  try {
    const res = await apiClient.post<{ ok: boolean; dimensions: number }>(
      '/kb/embedding-models/probe',
      buildModelPayload(),
    )
    modelMsg.value = t.value('kb.modelProbeOk').replace('{0}', String(res.dimensions))
  } catch (e) {
    modelErr.value = errText(e)
  } finally {
    probing.value = false
  }
}

async function saveModel() {
  savingModel.value = true
  modelMsg.value = ''
  modelErr.value = ''
  try {
    const m = await apiClient.post<EmbeddingModel>('/kb/embedding-models', buildModelPayload())
    modelMsg.value = t.value('kb.modelSaved')
    modelForm.value = { name: '', model_name: '', endpoint: '', api_key: '', dimensions: 0 }
    await loadModels()
    // 新建后默认选中，方便一键绑定
    modelBindId.value = m.id
  } catch (e) {
    modelErr.value = errText(e)
  } finally {
    savingModel.value = false
  }
}

async function deleteModel(m: EmbeddingModel) {
  if (!window.confirm(t.value('kb.confirmDeleteModel').replace('{0}', m.name))) return
  try {
    await apiClient.delete(`/kb/embedding-models/${m.id}`)
    await Promise.all([loadModels(), refreshKb()])
    setFlash(t.value('kb.saved'))
  } catch (e) {
    setFlash(errText(e))
  }
}

async function reindexKb() {
  if (!selected.value) return
  const n = selected.value.segment_count || 0
  if (!window.confirm(t.value('kb.confirmReindexKb').replace('{0}', String(n)))) return
  try {
    const res = await apiClient.post<{ embedded: number; note: string }>(`/kb/${selected.value.id}/reindex`)
    setFlash(t.value('kb.reindexKbOk').replace('{0}', String(res.embedded)))
    await refreshKb()
  } catch (e) {
    setFlash(errText(e))
  }
}

// ── 展示辅助 ────────────────────────────────────────────
const STRATEGIES = ['auto', 'S1', 'S2', 'S3', 'S4']

function strategyLabel(s: string): string {
  if (s === 'auto') return t.value('kb.strategyAuto')
  const map: Record<string, string> = {
    S1: t.value('kb.stS1'),
    S2: t.value('kb.stS2'),
    S3: t.value('kb.stS3'),
    S4: t.value('kb.stS4'),
  }
  return map[s] || s
}

function statusClass(status: string): string {
  if (status === 'ready') return 'st-ok'
  if (status === 'failed') return 'st-fail'
  if (status === 'empty') return 'st-empty'
  return 'st-wait'
}

function statusLabel(status: string): string {
  const map: Record<string, string> = {
    ready: t.value('kb.statusReady'),
    processing: t.value('kb.statusProcessing'),
    pending: t.value('kb.statusPending'),
    empty: t.value('kb.statusEmpty'),
    failed: t.value('kb.statusFailed'),
  }
  return map[status] || status
}

function modeLabel(mode: string): string {
  const map: Record<string, string> = {
    keyword: t.value('kb.modeKeyword'),
    semantic: t.value('kb.modeSemantic'),
    hybrid: t.value('kb.modeHybrid'),
  }
  return map[mode] || mode
}

function truncate(text: string, n: number): string {
  return text.length > n ? text.slice(0, n) + '…' : text
}

onMounted(loadKbs)
</script>

<template>
  <div class="kb-view">
    <header class="kb-header">
      <router-link to="/chat" class="kb-back">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="15 18 9 12 15 6" /></svg>
        {{ t('insights.back') }}
      </router-link>
      <h2 class="kb-title">{{ t('kb.title') }}</h2>
      <span v-if="flash" class="kb-flash">{{ flash }}</span>
      <button v-if="!selected" class="btn-primary" @click="openCreate">{{ t('kb.new') }}</button>
    </header>

    <!-- ═══ 列表态 ═══ -->
    <template v-if="!selected">
      <div v-if="loading" class="kb-hint">{{ t('kb.loading') }}</div>
      <div v-else-if="loadError" class="kb-error">{{ loadError }}</div>
      <div v-else-if="!kbs.length" class="kb-hint kb-empty">{{ t('kb.empty') }}</div>
      <div v-else class="kb-grid">
        <div v-for="kb in kbs" :key="kb.id" class="kb-card">
          <div class="kb-card-head">
            <span class="kb-card-name">{{ kb.name }}</span>
            <span v-if="kb.need_reindex" class="badge badge-warn">{{ t('kb.needReindex') }}</span>
            <span class="badge">{{ modeLabel(kb.default_retrieval_mode) }}</span>
          </div>
          <p v-if="kb.description" class="kb-card-desc">{{ truncate(kb.description, 80) }}</p>
          <div class="kb-card-stats">
            <span>{{ t('kb.docs') }} {{ kb.doc_count ?? 0 }}</span>
            <span>{{ t('kb.segs') }} {{ kb.segment_count ?? 0 }}</span>
            <span>{{ t('kb.vectors') }} {{ kb.vector_count ?? 0 }}</span>
          </div>
          <div class="kb-card-actions">
            <button class="btn-sm" @click="openKb(kb)">{{ t('kb.open') }}</button>
            <button class="btn-sm btn-danger" @click="deleteKb(kb)">{{ t('kb.deleteKb') }}</button>
          </div>
        </div>
      </div>
    </template>

    <!-- ═══ 详情态 ═══ -->
    <template v-else>
      <div class="kb-detail-head">
        <button class="btn-sm" @click="backToList">{{ t('kb.backToList') }}</button>
        <span class="kb-detail-name">{{ selected.name }}</span>
        <select
          class="select"
          :value="selected.default_retrieval_mode"
          @change="updateKbMode(($event.target as HTMLSelectElement).value)"
        >
          <option value="keyword">{{ t('kb.modeKeyword') }}</option>
          <option value="semantic">{{ t('kb.modeSemantic') }}</option>
          <option value="hybrid">{{ t('kb.modeHybrid') }}</option>
        </select>
        <span v-if="selected.need_reindex" class="badge badge-warn">{{ t('kb.needReindex') }}</span>
        <button v-if="selected.need_reindex" class="btn-sm" @click="reindexKb">{{ t('kb.reindexKb') }}</button>
        <button class="btn-sm btn-danger" @click="deleteKb(selected)">{{ t('kb.deleteKb') }}</button>
      </div>

      <!-- 文档管理 -->
      <section class="kb-section">
        <div class="kb-section-head">
          <h3 class="kb-section-title">{{ t('kb.docs') }}</h3>
          <span class="kb-hint-inline">{{ t('kb.uploadHint') }}</span>
          <select v-model="uploadStrategy" class="select select-sm">
            <option v-for="s in STRATEGIES" :key="s" :value="s">{{ strategyLabel(s) }}</option>
          </select>
          <button class="btn-primary btn-sm" :disabled="uploading" @click="pickFiles">
            {{ uploading ? t('kb.uploading') : t('kb.upload') }}
          </button>
          <input ref="fileInput" type="file" multiple class="kb-file-input" @change="onFilesPicked" />
        </div>
        <p v-if="docsMsg" class="kb-note">{{ docsMsg }}</p>
        <div v-if="docsLoading" class="kb-hint">{{ t('kb.loading') }}</div>
        <div v-else-if="docsError" class="kb-error">{{ docsError }}</div>
        <p v-else-if="!documents.length" class="kb-hint">{{ t('kb.noDocs') }}</p>
        <table v-else class="kb-table">
          <thead>
            <tr>
              <th>{{ t('kb.docName') }}</th>
              <th>{{ t('kb.docStatus') }}</th>
              <th class="num">{{ t('kb.docSegCount') }}</th>
              <th class="num">{{ t('kb.docChars') }}</th>
              <th class="num">{{ t('kb.docAssets') }}</th>
              <th>{{ t('kb.docStrategy') }}</th>
              <th>{{ t('kb.docActions') }}</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="doc in documents" :key="doc.id">
              <td class="td-name" :title="doc.error || doc.name">{{ doc.name }}</td>
              <td><span class="badge" :class="statusClass(doc.status)">{{ statusLabel(doc.status) }}</span></td>
              <td class="num">{{ doc.segment_count }}</td>
              <td class="num">{{ doc.char_count }}</td>
              <td class="num">{{ doc.asset_count }}</td>
              <td>{{ strategyLabel(doc.segment_strategy) }}</td>
              <td class="td-actions">
                <button class="link-btn" @click="openSegments(doc)">{{ t('kb.viewSegments') }}</button>
                <button class="link-btn" @click="reindexDoc(doc)">{{ t('kb.reindex') }}</button>
                <button class="link-btn link-danger" @click="deleteDoc(doc)">{{ t('kb.deleteDoc') }}</button>
              </td>
            </tr>
          </tbody>
        </table>
      </section>

      <!-- 检索测试 -->
      <section class="kb-section">
        <div class="kb-section-head">
          <h3 class="kb-section-title">{{ t('kb.search') }}</h3>
          <label class="kb-check">
            <input v-model="searchForm.skill" type="checkbox" />
            {{ t('kb.searchSkill') }}
          </label>
        </div>
        <div class="kb-search-bar">
          <input
            v-model="searchForm.query"
            class="input kb-search-input"
            :placeholder="t('kb.searchPh')"
            @keydown.enter="doSearch"
          />
          <select v-model="searchForm.retrieval_mode" class="select select-sm">
            <option value="">{{ modeLabel(selected.default_retrieval_mode) }}（{{ t('kb.mode') }}）</option>
            <option value="keyword">{{ t('kb.modeKeyword') }}</option>
            <option value="semantic">{{ t('kb.modeSemantic') }}</option>
            <option value="hybrid">{{ t('kb.modeHybrid') }}</option>
          </select>
          <label class="kb-label">{{ t('kb.searchTopK') }}
            <input v-model.number="searchForm.top_k" type="number" min="1" max="50" class="input input-xs" />
          </label>
          <label class="kb-label">{{ t('kb.searchThreshold') }}
            <input v-model.number="searchForm.score_threshold" type="number" min="0" max="1" step="0.05" class="input input-xs" />
          </label>
          <label class="kb-check">
            <input v-model="searchForm.rerank" type="checkbox" />
            {{ t('kb.searchRerank') }}
          </label>
          <button class="btn-primary btn-sm" :disabled="searching || !searchForm.query.trim()" @click="doSearch">
            {{ searching ? t('kb.searching') : t('kb.searchBtn') }}
          </button>
        </div>

        <div v-if="searchError" class="kb-error">{{ searchError }}</div>
        <template v-else-if="searchResult">
          <div class="kb-search-meta">
            <span>{{ searchResult.total }} / {{ modeLabel(searchResult.mode) }}</span>
            <span v-if="searchResult.rerank_applied" class="badge">{{ t('kb.searchRerankApplied') }}</span>
            <span v-if="searchResult.reason === 'below_threshold'" class="kb-hint-inline">{{ t('kb.searchBelow') }}</span>
          </div>
          <p v-if="searchResult.degraded" class="kb-note">{{ t('kb.searchDegraded') }}</p>
          <div v-if="searchResult.analysis" class="kb-analysis">
            <span>{{ t('kb.searchAnalysis') }}: {{ searchResult.analysis.language }}</span>
            <span v-if="searchResult.analysis.entities.length">
              {{ searchResult.analysis.entities.join(' / ') }}
            </span>
            <span v-if="searchResult.analysis.subqueries.length > 1">
              {{ t('kb.searchSubq') }}: {{ searchResult.analysis.subqueries.join(' | ') }}
            </span>
          </div>
          <p v-if="!searchResult.segments.length" class="kb-hint">{{ t('kb.searchNoHit') }}</p>
          <div v-else class="kb-results">
            <div v-for="(seg, i) in searchResult.segments" :key="seg.segment_id || i" class="kb-result">
              <div class="kb-result-head">
                <span class="kb-result-rank">#{{ i + 1 }}</span>
                <span class="kb-result-score">{{ t('kb.searchScore') }} {{ scoreText(seg.score) }}</span>
                <span class="kb-result-src">
                  {{ seg.source.doc_name || '?' }}<template v-if="seg.position"> · {{ t('kb.searchPos').replace('{0}', String(seg.position)) }}</template>
                </span>
                <span v-if="seg.truncated" class="badge badge-warn">{{ t('kb.segTruncated') }}</span>
                <span v-if="seg.asset_refs.length" class="badge">{{ t('kb.docAssets') }} {{ seg.asset_refs.length }}</span>
              </div>
              <pre class="kb-result-content">{{ truncate(seg.content, 400) }}</pre>
            </div>
          </div>
        </template>
      </section>

      <!-- 嵌入模型 -->
      <section class="kb-section">
        <div class="kb-section-head">
          <h3 class="kb-section-title">{{ t('kb.models') }}</h3>
          <span class="kb-hint-inline">{{ t('kb.modelCurrent') }}: {{ selected.embedding_model_id ? boundModelName : t('kb.modelNone') }}</span>
          <button class="link-btn" @click="showModels = !showModels">
            {{ showModels ? t('kb.cancel') : t('kb.modelManage') }}
          </button>
        </div>
        <div class="kb-bind-row">
          <select v-model="modelBindId" class="select select-sm">
            <option value="">{{ t('kb.modelNone') }}</option>
            <option v-for="m in models" :key="m.id" :value="m.id">{{ m.name }}（{{ m.model_name }}）</option>
          </select>
          <button class="btn-sm" @click="bindModel(false)">{{ t('kb.modelBind') }}</button>
          <button v-if="selected.embedding_model_id" class="btn-sm" @click="bindModel(true)">{{ t('kb.modelUnbind') }}</button>
        </div>

        <template v-if="showModels">
          <div v-if="models.length" class="kb-model-list">
            <div v-for="m in models" :key="m.id" class="kb-model-item">
              <span class="kb-model-name">{{ m.name }}</span>
              <span class="kb-hint-inline">{{ m.model_name }}</span>
              <span class="kb-hint-inline">{{ m.endpoint }}</span>
              <span class="kb-hint-inline">dim {{ m.dimensions }}</span>
              <span v-if="m.probe_ok" class="badge st-ok">probe ok</span>
              <span class="badge">{{ m.has_api_key ? t('kb.modelKeyConfigured') : t('kb.modelKeyMissing') }}</span>
              <button class="link-btn link-danger" @click="deleteModel(m)">{{ t('kb.modelDelete') }}</button>
            </div>
          </div>
          <p v-else class="kb-hint">{{ t('kb.modelEmpty') }}</p>

          <h4 class="kb-subtitle">{{ t('kb.modelAdd') }}</h4>
          <div class="kb-model-form">
            <input v-model="modelForm.name" class="input" :placeholder="t('kb.modelFieldName')" />
            <input v-model="modelForm.model_name" class="input" :placeholder="t('kb.modelFieldModel')" />
            <input v-model="modelForm.endpoint" class="input" :placeholder="t('kb.modelFieldEndpoint')" />
            <input v-model="modelForm.api_key" class="input" type="password" :placeholder="t('kb.modelFieldKey')" />
            <input v-model.number="modelForm.dimensions" type="number" min="0" class="input input-xs" :placeholder="t('kb.modelFieldDims')" />
            <button class="btn-sm" :disabled="probing" @click="probeModel">{{ t('kb.modelProbe') }}</button>
            <button class="btn-primary btn-sm" :disabled="savingModel" @click="saveModel">
              {{ savingModel ? t('kb.modelSaving') : t('kb.modelSave') }}
            </button>
          </div>
          <p v-if="modelMsg" class="kb-note">{{ modelMsg }}</p>
          <p v-if="modelErr" class="kb-error">{{ modelErr }}</p>
        </template>
      </section>
    </template>

    <!-- 新建知识库弹窗 -->
    <div v-if="showCreate" class="kb-overlay" @click.self="showCreate = false">
      <div class="kb-dialog">
        <h3 class="kb-dialog-title">{{ t('kb.createTitle') }}</h3>
        <input v-model="createForm.name" class="input" :placeholder="t('kb.namePh')" @keydown.enter="submitCreate" />
        <input v-model="createForm.description" class="input" :placeholder="t('kb.desc')" />
        <label class="kb-label">{{ t('kb.mode') }}
          <select v-model="createForm.default_retrieval_mode" class="select">
            <option value="keyword">{{ t('kb.modeKeyword') }}</option>
            <option value="semantic">{{ t('kb.modeSemantic') }}</option>
            <option value="hybrid">{{ t('kb.modeHybrid') }}</option>
          </select>
        </label>
        <p v-if="createError" class="kb-error">{{ createError }}</p>
        <div class="kb-dialog-actions">
          <button class="btn-sm" @click="showCreate = false">{{ t('kb.cancel') }}</button>
          <button class="btn-primary btn-sm" :disabled="creating" @click="submitCreate">
            {{ creating ? t('kb.loading') : t('kb.createSubmit') }}
          </button>
        </div>
      </div>
    </div>

    <!-- 分段预览弹窗 -->
    <div v-if="segsDoc" class="kb-overlay" @click.self="segsDoc = null">
      <div class="kb-dialog kb-dialog-wide">
        <h3 class="kb-dialog-title">{{ t('kb.segViewTitle').replace('{0}', segsDoc.name) }}</h3>
        <div v-if="segsLoading" class="kb-hint">{{ t('kb.loading') }}</div>
        <p v-else-if="!segs.length" class="kb-hint">{{ t('kb.segEmpty') }}</p>
        <div v-else class="kb-seg-list">
          <div v-for="seg in segs" :key="seg.id" class="kb-seg-item">
            <div class="kb-seg-meta">
              <span class="kb-seg-pos">#{{ seg.position }}</span>
              <span v-if="segRoleTag(seg.metadata)" class="badge">{{ segRoleTag(seg.metadata) }}</span>
              <span v-if="seg.metadata.headings" class="kb-hint-inline">{{ seg.metadata.headings }}</span>
              <span v-if="seg.metadata.truncated" class="badge badge-warn">{{ t('kb.segTruncated') }}</span>
              <span v-if="seg.has_embedding" class="badge st-ok">{{ t('kb.segVec') }}</span>
            </div>
            <pre class="kb-seg-content">{{ truncate(seg.content, 500) }}</pre>
          </div>
        </div>
        <div class="kb-dialog-actions">
          <button class="btn-sm" @click="segsDoc = null">{{ t('kb.cancel') }}</button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.kb-view {
  min-height: 100vh;
  background: var(--bg-main);
  padding: var(--space-lg);
  max-width: 1080px;
  margin: 0 auto;
  color: var(--color-text);
  font-size: var(--font-size-sm);
}

.kb-header {
  display: flex;
  align-items: center;
  gap: var(--space-md);
  margin-bottom: var(--space-md);
}

.kb-back {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: var(--font-size-sm);
  color: var(--color-text-secondary);
  text-decoration: none;
}
.kb-back:hover { color: var(--color-primary); }

.kb-title {
  margin: 0;
  font-size: var(--font-size-lg);
  font-weight: 700;
  flex: 1;
}

.kb-flash {
  font-size: var(--font-size-xs);
  color: var(--color-primary);
}

.kb-hint { color: var(--color-text-tertiary); font-size: var(--font-size-sm); padding: var(--space-sm) 0; }
.kb-empty { padding: var(--space-xl) 0; text-align: center; }
.kb-error { color: var(--color-danger); font-size: var(--font-size-xs); padding: 4px 0; margin: 0; }
.kb-note { color: var(--color-primary); font-size: var(--font-size-xs); margin: 4px 0; }
.kb-hint-inline { color: var(--color-text-tertiary); font-size: var(--font-size-xs); }

/* ── 列表卡片 ── */
.kb-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
  gap: var(--space-sm);
}

.kb-card {
  background: var(--bg-surface);
  border: 1px solid var(--border-color);
  border-radius: var(--radius-md);
  padding: var(--space-md);
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.kb-card-head { display: flex; align-items: center; gap: 6px; }
.kb-card-name { font-weight: 700; font-size: var(--font-size-base); flex: 1; }
.kb-card-desc { margin: 0; color: var(--color-text-secondary); font-size: var(--font-size-xs); }
.kb-card-stats { display: flex; gap: var(--space-md); color: var(--color-text-tertiary); font-size: var(--font-size-xs); }
.kb-card-actions { display: flex; gap: 6px; margin-top: 4px; }

/* ── 详情 ── */
.kb-detail-head {
  display: flex;
  align-items: center;
  gap: var(--space-sm);
  margin-bottom: var(--space-md);
  padding-bottom: var(--space-sm);
  border-bottom: 1px solid var(--border-light);
}
.kb-detail-name { font-weight: 700; font-size: var(--font-size-md); flex: 1; }

.kb-section {
  background: var(--bg-surface);
  border: 1px solid var(--border-color);
  border-radius: var(--radius-md);
  padding: var(--space-md);
  margin-bottom: var(--space-md);
}

.kb-section-head {
  display: flex;
  align-items: center;
  gap: var(--space-sm);
  flex-wrap: wrap;
  margin-bottom: var(--space-sm);
}

.kb-section-title { margin: 0; font-size: var(--font-size-base); font-weight: 700; flex: 0 0 auto; }
.kb-subtitle { margin: var(--space-sm) 0 6px; font-size: var(--font-size-sm); font-weight: 600; }

.kb-file-input { display: none; }

/* ── 表格 ── */
.kb-table { width: 100%; border-collapse: collapse; font-size: var(--font-size-xs); }
.kb-table th {
  text-align: left;
  color: var(--color-text-tertiary);
  font-weight: 600;
  padding: 6px 8px;
  border-bottom: 1px solid var(--border-color);
}
.kb-table td { padding: 6px 8px; border-bottom: 1px solid var(--border-light); }
.kb-table .num { text-align: right; font-variant-numeric: tabular-nums; }
.td-name { max-width: 240px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.td-actions { white-space: nowrap; }

/* ── 检索 ── */
.kb-search-bar { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; margin-bottom: var(--space-sm); }
.kb-search-input { flex: 1; min-width: 220px; }
.kb-search-meta { display: flex; align-items: center; gap: var(--space-sm); font-size: var(--font-size-xs); color: var(--color-text-secondary); margin-bottom: 6px; }
.kb-analysis { display: flex; gap: var(--space-md); font-size: var(--font-size-xs); color: var(--color-text-tertiary); margin-bottom: 6px; flex-wrap: wrap; }

.kb-results { display: flex; flex-direction: column; gap: 6px; }
.kb-result { border: 1px solid var(--border-light); border-radius: var(--radius-sm); padding: 8px 10px; }
.kb-result-head { display: flex; align-items: center; gap: var(--space-sm); font-size: var(--font-size-xs); color: var(--color-text-secondary); flex-wrap: wrap; }
.kb-result-rank { font-weight: 700; color: var(--color-primary); }
.kb-result-score { font-variant-numeric: tabular-nums; }
.kb-result-src { color: var(--color-text-tertiary); }
.kb-result-content {
  margin: 6px 0 0;
  font-family: var(--font-sans);
  font-size: var(--font-size-xs);
  white-space: pre-wrap;
  word-break: break-word;
  color: var(--color-text);
  max-height: 140px;
  overflow: auto;
}

/* ── 模型 ── */
.kb-bind-row { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.kb-model-list { display: flex; flex-direction: column; gap: 4px; margin-top: var(--space-sm); }
.kb-model-item { display: flex; align-items: center; gap: var(--space-sm); font-size: var(--font-size-xs); flex-wrap: wrap; padding: 6px 8px; border: 1px solid var(--border-light); border-radius: var(--radius-sm); }
.kb-model-name { font-weight: 600; }
.kb-model-form { display: flex; gap: 6px; flex-wrap: wrap; align-items: center; }

/* ── 分段列表 ── */
.kb-seg-list { display: flex; flex-direction: column; gap: 6px; max-height: 60vh; overflow: auto; }
.kb-seg-item { border: 1px solid var(--border-light); border-radius: var(--radius-sm); padding: 6px 8px; }
.kb-seg-meta { display: flex; align-items: center; gap: 6px; font-size: var(--font-size-xs); color: var(--color-text-secondary); flex-wrap: wrap; }
.kb-seg-pos { font-weight: 700; color: var(--color-primary); }
.kb-seg-content {
  margin: 4px 0 0;
  font-family: var(--font-sans);
  font-size: var(--font-size-xs);
  white-space: pre-wrap;
  word-break: break-word;
  color: var(--color-text);
  max-height: 110px;
  overflow: auto;
}

/* ── 通用控件 ── */
.input, .select {
  background: var(--bg-input);
  border: 1px solid var(--border-color);
  border-radius: var(--radius-sm);
  color: var(--color-text);
  font-size: var(--font-size-xs);
  padding: 6px 8px;
  outline: none;
}
.input:focus, .select:focus { border-color: var(--color-primary); }
.input-xs { width: 64px; }
.select-sm { padding: 4px 6px; }

.btn-primary {
  background: var(--color-primary);
  border: 1px solid var(--color-primary);
  border-radius: var(--radius-sm);
  color: var(--color-text-inverse, #0a1018);
  font-size: var(--font-size-xs);
  font-weight: 600;
  padding: 6px 12px;
  cursor: pointer;
  transition: var(--transition-fast);
}
.btn-primary:hover { background: var(--color-primary-hover); }
.btn-primary:disabled { opacity: 0.5; cursor: not-allowed; }

.btn-sm {
  background: var(--bg-input);
  border: 1px solid var(--border-color);
  border-radius: var(--radius-sm);
  color: var(--color-text-secondary);
  font-size: var(--font-size-xs);
  padding: 5px 10px;
  cursor: pointer;
  transition: var(--transition-fast);
}
.btn-sm:hover { border-color: var(--color-primary); color: var(--color-primary); }
.btn-sm:disabled { opacity: 0.5; cursor: not-allowed; }
.btn-danger { color: var(--color-danger); border-color: var(--border-error, var(--border-color)); }

.link-btn {
  background: none;
  border: none;
  color: var(--color-primary);
  font-size: var(--font-size-xs);
  cursor: pointer;
  padding: 0 6px 0 0;
}
.link-btn:hover { text-decoration: underline; }
.link-danger { color: var(--color-danger); }

.badge {
  display: inline-block;
  padding: 1px 6px;
  border-radius: var(--radius-full);
  border: 1px solid var(--border-color);
  font-size: 11px;
  color: var(--color-text-secondary);
  line-height: 18px;
}
.badge-warn { color: var(--color-warning); border-color: var(--color-warning); }
.st-ok { color: var(--color-success); border-color: var(--color-success); }
.st-fail { color: var(--color-danger); border-color: var(--color-danger); }
.st-empty { color: var(--color-warning); border-color: var(--color-warning); }
.st-wait { color: var(--color-text-tertiary); }

.kb-check { display: inline-flex; align-items: center; gap: 4px; font-size: var(--font-size-xs); color: var(--color-text-secondary); cursor: pointer; }
.kb-label { display: inline-flex; align-items: center; gap: 4px; font-size: var(--font-size-xs); color: var(--color-text-secondary); }

/* ── 弹窗 ── */
.kb-overlay {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.6);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 1000;
  padding: var(--space-md);
}
.kb-dialog {
  width: min(440px, 100%);
  background: var(--bg-surface);
  border: 1px solid var(--border-strong);
  border-radius: var(--radius-lg);
  padding: var(--space-lg);
  box-shadow: var(--shadow-xl);
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.kb-dialog-wide { width: min(720px, 100%); }
.kb-dialog-title { margin: 0; font-size: var(--font-size-md); font-weight: 700; }
.kb-dialog-actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 4px; }
</style>
