<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { apiClient } from '../api/client'
import { currentOrigin } from '../api/runtime'
import { getToken } from '../api/token'
import { useLanguage } from '../composables/useLanguage'

const { t } = useLanguage()

interface StageStatusItem {
  stage: string
  label: string
  target_engine: string
  available: boolean
  reason: string
  will_engine: string
}
interface StageResult {
  stage: string
  status: string
  engine: string
  target_engine: string
  summary: string
  duration_ms: number
  error?: string
  facts?: Record<string, unknown>
}
interface RebuildInfo {
  ok: boolean
  run_id: string
  exe_path: string
  exe_name: string
  size: number
  sha256: string
  kind: string
  title: string
  log?: string
  error: string
}
interface RunRecord {
  run_id: string
  subject_name: string
  subject_sha256: string
  created_at: string
  status: string
  stages: Record<string, StageResult>
  evidence_bundle_id: string
  evidence_dir: string
  evidence_report: { ok?: boolean; items_checked?: number; problems?: string[] }
  run_dir: string
  duration_ms?: number
  rebuild?: RebuildInfo
}

type PhaseStatus = 'pending' | 'running' | 'done' | 'skipped' | 'failed'
interface LivePhase {
  stage: string
  label: string
  index: number
  total: number
  status: PhaseStatus
  done: number
  totalSteps: number
  percent: number
  message: string
  summary: string
}
interface ProgressEventData {
  type: string
  stage?: string
  label?: string
  index?: number
  stage_total?: number
  status?: string
  done?: number
  total?: number
  percent?: number
  message?: string
  summary?: string
  record?: RunRecord
  error?: string
}

const stages = ref<StageStatusItem[]>([])
const runs = ref<Record<string, unknown>[]>([])
const detail = ref<RunRecord | null>(null)

// 界面上隐藏的专用阶段（Java class 反编译、功能等价 exe 重建）；仅展示通用阶段信息。
const HIDDEN_STAGES = new Set(['java_decompile', 'rebuild_exe'])

// 工具链状态表只展示通用阶段（剔除隐藏项）。
const visibleStages = computed(() => stages.value.filter((s) => !HIDDEN_STAGES.has(s.stage)))

// 详情页阶段表（后端回传为对象）剔除隐藏项后转成可迭代条目。
const detailStageEntries = computed<[string, StageResult][]>(() =>
  detail.value
    ? (Object.entries(detail.value.stages) as [string, StageResult][]).filter(
        ([k]) => !HIDDEN_STAGES.has(k),
      )
    : [],
)
const notes = ref('')
const loading = ref(false)
const error = ref('')

// ── 文件选择上传 ──────────────────────────────────────────────────────
const selectedFile = ref<File | null>(null)
const fileInput = ref<HTMLInputElement | null>(null)
const uploadResult = ref<{ classification: Record<string, unknown>; plan: Record<string, unknown> } | null>(null)
const expectedGoal = computed(() => {
  const plan = uploadResult.value?.plan
  if (!plan) return ''
  return String(plan.goal || '')
})

// ── 实时进度状态 ──────────────────────────────────────────────────────
const liveRunId = ref('')
const streamStatus = ref<'idle' | 'connecting' | 'running' | 'done' | 'error'>('idle')
const streamError = ref('')
const phases = ref<LivePhase[]>([])
const streamFinalized = ref(false)
const eventSource = ref<EventSource | null>(null)

const overallDone = computed(
  () => phases.value.filter((p) => p.status === 'done' || p.status === 'skipped' || p.status === 'failed').length,
)
const overallTotal = computed(() => phases.value.length)
const overallPercent = computed(() =>
  overallTotal.value ? Math.round((overallDone.value / overallTotal.value) * 100) : 0,
)
const currentPhase = computed(() => phases.value.find((p) => p.status === 'running'))

async function loadStatus() {
  try {
    const res = await apiClient.get<{ stages: StageStatusItem[] }>('/reverse-engineering/status')
    stages.value = res.stages || []
  } catch (e) {
    error.value = String(e)
  }
}

async function loadRuns() {
  try {
    runs.value = await apiClient.get<Record<string, unknown>[]>('/reverse-engineering/runs')
  } catch (e) {
    error.value = String(e)
  }
}

function closeStream() {
  if (eventSource.value) {
    eventSource.value.close()
    eventSource.value = null
  }
}

/** 阶段结束状态归一：ok/substituted → 已完成；skipped/failed 各自独立。 */
function doneStatus(s: string | undefined): PhaseStatus {
  if (s === 'skipped') return 'skipped'
  if (s === 'failed') return 'failed'
  return 'done'
}

function upsertPhase(data: ProgressEventData) {
  const stage = data.stage || ''
  if (HIDDEN_STAGES.has(stage)) return // 隐藏的专用阶段不进入实时列表
  let p = phases.value.find((x) => x.stage === stage)
  if (!p) {
    p = {
      stage,
      label: data.label || stage,
      index: data.index ?? phases.value.length,
      total: data.stage_total ?? phases.value.length,
      status: 'running',
      done: 0,
      totalSteps: 0,
      percent: 0,
      message: '',
      summary: '',
    }
    phases.value.push(p)
  }
  p.status = 'running'
  if (data.label) p.label = data.label
  if (typeof data.index === 'number') p.index = data.index
  if (typeof data.stage_total === 'number') p.total = data.stage_total
}

function updatePhaseProgress(data: ProgressEventData) {
  const stage = data.stage || ''
  if (HIDDEN_STAGES.has(stage)) return
  const p = phases.value.find((x) => x.stage === stage)
  if (!p) return
  p.status = 'running'
  if (typeof data.done === 'number') p.done = data.done
  if (typeof data.total === 'number') p.totalSteps = data.total
  if (typeof data.percent === 'number') p.percent = data.percent
  if (typeof data.message === 'string') p.message = data.message
}

function setPhaseDone(data: ProgressEventData) {
  const stage = data.stage || ''
  if (HIDDEN_STAGES.has(stage)) return
  const p = phases.value.find((x) => x.stage === stage)
  if (!p) return
  p.status = doneStatus(data.status)
  if (typeof data.summary === 'string') p.summary = data.summary
  p.percent = 100
  p.done = p.totalSteps
}

function handleProgressEvent(data: ProgressEventData) {
  clearConnectWatch() // 收到任何事件即认为流已建立，撤销超时看门狗
  switch (data.type) {
    case 'phase_start':
      upsertPhase(data)
      streamStatus.value = 'running'
      break
    case 'phase_progress':
      updatePhaseProgress(data)
      streamStatus.value = 'running'
      break
    case 'phase_done':
      setPhaseDone(data)
      streamStatus.value = 'running'
      break
    case 'run_done':
      streamFinalized.value = true
      closeStream()
      streamStatus.value = 'done'
      if (data.record) detail.value = data.record
      void loadRuns()
      break
    case 'run_error':
      streamFinalized.value = true
      closeStream()
      streamStatus.value = 'error'
      streamError.value = String(data.error || 'unknown')
      break
    default:
      break
  }
}

function openProgressStream(runId: string) {
  closeStream()
  const origin = currentOrigin()
  const token = getToken()
  const qs = token ? `?token=${encodeURIComponent(token)}` : ''
  const es = new EventSource(`${origin}/api/reverse-engineering/runs/${runId}/progress${qs}`)
  eventSource.value = es
  es.onmessage = (ev: MessageEvent) => {
    let data: ProgressEventData
    try {
      data = JSON.parse(ev.data) as ProgressEventData
    } catch {
      return
    }
    handleProgressEvent(data)
  }
  es.onerror = () => {
    // 正常结束（run_done/run_error）已在 handleProgressEvent 中主动 close，不会走到这里；
    // 仅当连接异常中断时兜底标记错误。
    if (streamFinalized.value || streamStatus.value === 'done' || streamStatus.value === 'error') return
    streamStatus.value = 'error'
    streamError.value = 'progress stream disconnected'
    closeStream()
  }
}

/** 重置阶段列表：来自工具链状态，保证未收到事件前也能看到待执行阶段与固定顺序。 */
function resetPhases() {
  phases.value = visibleStages.value.map((s, i) => ({
    stage: s.stage,
    label: s.label,
    index: i,
    total: stages.value.length,
    status: 'pending',
    done: 0,
    totalSteps: 0,
    percent: 0,
    message: '',
    summary: '',
  }))
}

/** 连接看门狗：附加到死 run（如后端重启前的遗留任务）时 SSE 既不来事件也不报错，
 *  界面会永远停在"建立中"并把按钮锁死。超时未收到任何事件 → 标记错误并解锁。 */
const connectWatch = ref<number | null>(null)

function startConnectWatch() {
  clearConnectWatch()
  connectWatch.value = window.setTimeout(() => {
    if (streamStatus.value === 'connecting') {
      streamStatus.value = 'error'
      streamError.value = t.value('reveng.connectTimeout')
      closeStream()
    }
  }, 15000)
}

function clearConnectWatch() {
  if (connectWatch.value !== null) {
    window.clearTimeout(connectWatch.value)
    connectWatch.value = null
  }
}

/** 附加到某个 run 的实时进度流（运行中的历史任务也可直接进入）。 */
function attachRun(runId: string) {
  detail.value = null
  streamFinalized.value = false
  streamError.value = ''
  resetPhases()
  liveRunId.value = runId
  streamStatus.value = 'connecting'
  openProgressStream(runId)
  startConnectWatch()
}

function triggerFileInput() {
  fileInput.value?.click()
}

function onFileChange(ev: Event) {
  const target = ev.target as HTMLInputElement
  const file = target.files?.[0] || null
  selectedFile.value = file
  uploadResult.value = null
  if (target) target.value = ''
}

async function uploadAndStart() {
  if (!selectedFile.value) return
  error.value = ''
  streamFinalized.value = false
  streamError.value = ''
  resetPhases()
  liveRunId.value = ''
  streamStatus.value = 'connecting'
  try {
    const form = new FormData()
    form.append('file', selectedFile.value)
    form.append('notes', notes.value)
    const res = await apiClient.uploadForm<{
      run_id: string
      status: string
      classification: Record<string, unknown>
      plan: Record<string, unknown>
    }>('/reverse-engineering/runs/upload', form)
    uploadResult.value = { classification: res.classification, plan: res.plan }
    attachRun(res.run_id)
  } catch (e) {
    error.value = String(e)
    streamStatus.value = 'error'
  }
}

async function openRun(runId: string) {
  loading.value = true
  try {
    detail.value = await apiClient.get<RunRecord>(`/reverse-engineering/runs/${runId}`)
  } catch (e) {
    error.value = String(e)
  } finally {
    loading.value = false
  }
}

// ── 功能等价重建（产出新 exe）──────────────────────────────────────────
const rebuilding = ref(false)
const rebuildLocal = ref<RebuildInfo | null>(null)
const rebuildError = ref('')

const rebuildInfo = computed<RebuildInfo | null>(() => rebuildLocal.value ?? detail.value?.rebuild ?? null)

/** 样本是否为可执行文件（决定是否展示重建入口；判定规则与后端一致为"是可执行容器"）。 */
const isExecutableSubject = computed(() =>
  /\.(exe|dll|sys|scr|ocx|cpl|com|so|dylib|elf)$/i.test(String(detail.value?.subject_name ?? '')),
)

async function doRebuild() {
  if (!detail.value || rebuilding.value) return
  rebuilding.value = true
  rebuildError.value = ''
  try {
    const res = await apiClient.post<RebuildInfo>(`/reverse-engineering/runs/${detail.value.run_id}/rebuild`)
    rebuildLocal.value = res
    if (!res.ok) rebuildError.value = res.error || t.value('reveng.rebuildFailed')
    else await openRun(detail.value.run_id) // 刷新详情（后端已把结果写回 run.json）
  } catch (e) {
    rebuildError.value = String(e)
  } finally {
    rebuilding.value = false
  }
}

function phaseBadge(s: PhaseStatus): string {
  // script 中须经 .value 调用（模板会自动解包 ref，script 不会）
  return t.value(`reveng.phase${s.charAt(0).toUpperCase()}${s.slice(1)}`)
}

/** 运行历史状态 → 徽章类名。 */
function statusBadge(status: string): string {
  switch (status) {
    case 'ok':
      return 'st-ok'
    case 'running':
      return 'st-wait'
    case 'failed':
      return 'st-fail'
    default:
      return 'st-empty'
  }
}

/** 运行历史状态 → 徽章标签。 */
function statusLabel(status: string): string {
  switch (status) {
    case 'ok':
      return t.value('reveng.available')
    case 'running':
      return t.value('reveng.phaseRunning')
    case 'failed':
      return t.value('reveng.phaseFailed')
    default:
      return status
  }
}

/** 阶段结果状态 → 徽章类名。 */
function stageBadge(status: string): string {
  switch (status) {
    case 'ok':
      return 'st-ok'
    case 'failed':
      return 'st-fail'
    default:
      return 'st-wait'
  }
}

const degraded = computed(() =>
  stages.value.filter((s) => !s.available && s.stage !== 'evidence_v2' && !HIDDEN_STAGES.has(s.stage)),
)

onMounted(async () => {
  await loadStatus()
  await loadRuns()
  // 页面打开时若有正在运行的任务（从其它入口发起、或刷新页面后回来），
  // 自动附加其实时进度流——无需重新发起也能看到当前进度。
  const running = runs.value.find((r) => String(r.status) === 'running')
  if (running) attachRun(String(running.run_id))
})
onUnmounted(() => {
  clearConnectWatch()
  closeStream()
})
</script>

<template>
  <div class="rev-view">
    <header class="rev-header">
      <router-link to="/chat" class="rev-back">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="15 18 9 12 15 6" /></svg>
        {{ t('reveng.back') }}
      </router-link>
      <div class="rev-branding">
        <h2 class="rev-title">{{ t('reveng.title') }}</h2>
        <p class="rev-subtitle">{{ t('reveng.subtitle') }}</p>
      </div>
    </header>

    <p v-if="error" class="rev-error">{{ error }}</p>

    <!-- 工具链状态 -->
    <section class="rev-card">
      <div class="rev-card-head">
        <h3 class="rev-card-title">{{ t('reveng.toolchain') }}</h3>
      </div>
      <table class="rev-table">
        <thead>
          <tr>
            <th>{{ t('reveng.stage') }}</th>
            <th>{{ t('reveng.targetEngine') }}</th>
            <th>{{ t('reveng.state') }}</th>
            <th>{{ t('reveng.effective') }}</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="s in visibleStages" :key="s.stage">
            <td>{{ s.label }}</td>
            <td>{{ s.target_engine }}</td>
            <td>
              <span :class="['badge', s.available ? 'st-ok' : 'st-fail']">
                {{ s.available ? t('reveng.available') : t('reveng.degraded') }}
              </span>
            </td>
            <td>{{ s.will_engine }}<span v-if="!s.available" class="rev-why"> — {{ s.reason }}</span></td>
          </tr>
        </tbody>
      </table>
      <p v-if="degraded.length" class="rev-note">
        {{ t('reveng.degradedHint') }}：{{ degraded.map((d) => d.label).join('、') }}
      </p>
    </section>

    <!-- 新建运行 -->
    <section class="rev-card">
      <div class="rev-card-head">
        <h3 class="rev-card-title">{{ t('reveng.newRun') }}</h3>
      </div>
      <div class="rev-form rev-file-form">
        <input
          ref="fileInput"
          type="file"
          class="rev-file-input"
          @change="onFileChange"
        />
        <button
          type="button"
          class="btn-ghost btn-sm"
          @click="triggerFileInput"
        >
          {{ t('reveng.selectFile') }}
        </button>
        <input v-model="notes" class="input" :placeholder="t('reveng.notesPh')" />
        <button
          type="button"
          class="btn-primary btn-sm"
          :disabled="(streamStatus === 'connecting' || streamStatus === 'running') || !selectedFile"
          @click="uploadAndStart"
        >
          {{ streamStatus === 'running' || streamStatus === 'connecting' ? t('reveng.running') : t('reveng.start') }}
        </button>
      </div>
      <p class="rev-file-name">
        {{ selectedFile ? selectedFile.name : t('reveng.noFile') }}
      </p>
      <div v-if="uploadResult" class="rev-recognition">
        <p class="rev-note">
          {{ t('reveng.recognizedAs') }}：
          <strong>{{ uploadResult.classification.kind }}</strong>
          <span v-if="uploadResult.classification.confidence">
            （{{ Number(uploadResult.classification.confidence) * 100 }}%）
          </span>
        </p>
        <p v-if="expectedGoal" class="rev-note">{{ t('reveng.expectedOutput') }}：{{ expectedGoal }}</p>
      </div>
      <p v-else class="rev-note">{{ t('reveng.fileHint') }}</p>
    </section>

    <!-- 实时进展反馈面板 -->
    <section v-if="liveRunId" class="rev-card rev-live">
      <div class="rev-card-head rev-live-head">
        <span class="rev-live-title">{{ t('reveng.runningTitle') }}</span>
        <span class="rev-live-runid">{{ liveRunId }}</span>
      </div>

      <div class="rev-overall">
        <div class="rev-bar"><div class="rev-bar-fill" :style="{ width: overallPercent + '%' }"></div></div>
        <span class="rev-overall-text">
          {{ t('reveng.overall') }}：{{ overallDone }}/{{ overallTotal }}（{{ overallPercent }}%）
        </span>
      </div>
      <p v-if="currentPhase" class="rev-current">
        {{ t('reveng.currentStage') }}：{{ currentPhase.label }}
        <span v-if="currentPhase.message">（{{ currentPhase.message }}）</span>
      </p>

      <p v-if="streamStatus === 'connecting'" class="rev-note">{{ t('reveng.streamConnecting') }}</p>

      <ul class="rev-phases">
        <li
          v-for="p in phases"
          :key="p.stage"
          :class="['rev-phase', p.status, p.status === 'running' ? 'is-active' : '']"
        >
          <div class="rev-phase-row">
            <span class="rev-phase-index">{{ p.index + 1 }}.</span>
            <span class="rev-phase-label">{{ p.label }}</span>
            <span class="rev-phase-badge">{{ phaseBadge(p.status) }}</span>
          </div>
          <div v-if="p.status === 'running' && p.totalSteps" class="rev-phase-prog">
            <div class="rev-bar sm"><div class="rev-bar-fill" :style="{ width: p.percent + '%' }"></div></div>
            <span class="rev-phase-step">
              {{ p.done }}/{{ p.totalSteps }}（{{ p.percent }}%）{{ p.message ? ' · ' + p.message : '' }}
            </span>
          </div>
          <div
            v-if="p.status === 'done' || p.status === 'skipped' || p.status === 'failed'"
            class="rev-phase-sum"
          >
            {{ p.summary }}
          </div>
        </li>
      </ul>

      <p v-if="streamStatus === 'error'" class="rev-error">{{ t('reveng.streamError') }}：{{ streamError }}</p>
      <p v-if="streamStatus === 'done'" class="rev-note">{{ t('reveng.streamDone') }}</p>

      <div v-if="streamStatus === 'done'" class="rev-live-actions">
        <button class="btn-ghost btn-sm" @click="triggerFileInput">{{ t('reveng.startAgain') }}</button>
        <a v-if="detail" class="link-btn" href="#rev-detail">{{ t('reveng.viewDetail') }}</a>
      </div>
    </section>

    <!-- 运行历史 -->
    <section class="rev-card">
      <div class="rev-card-head">
        <h3 class="rev-card-title">{{ t('reveng.history') }}</h3>
      </div>
      <div class="rev-history-scroll">
        <table v-if="runs.length" class="rev-table">
          <thead>
            <tr>
              <th>{{ t('reveng.runId') }}</th>
              <th>{{ t('reveng.subject') }}</th>
              <th>{{ t('reveng.time') }}</th>
              <th>{{ t('reveng.state') }}</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="r in runs" :key="String(r.run_id)">
              <td class="rev-path">{{ r.run_id }}</td>
              <td class="rev-path">{{ r.subject_name }}</td>
              <td>{{ r.created_at }}</td>
              <td>
                <span :class="['badge', statusBadge(String(r.status))]">{{ statusLabel(String(r.status)) }}</span>
              </td>
              <td class="rev-actions">
                <button
                  v-if="r.status === 'running'"
                  class="btn-sm btn-ghost"
                  @click="attachRun(String(r.run_id))"
                >
                  {{ t('reveng.live') }}
                </button>
                <button class="btn-sm btn-ghost link-btn" @click="openRun(String(r.run_id))">{{ t('reveng.view') }}</button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <p v-if="!runs.length" class="rev-empty">{{ t('reveng.empty') }}</p>
    </section>

    <!-- 运行详情 -->
    <section v-if="detail" id="rev-detail" class="rev-card">
      <div class="rev-card-head">
        <h3 class="rev-card-title">{{ t('reveng.detail') }} — {{ detail.run_id }}</h3>
        <div class="rev-actions">
          <button v-if="isExecutableSubject" class="btn-sm btn-ghost" :disabled="rebuilding" @click="doRebuild">
            {{ rebuilding ? t('reveng.rebuilding') : t('reveng.rebuild') }}
          </button>
          <button class="btn-sm btn-ghost link-btn" @click="detail = null">{{ t('reveng.back') }}</button>
        </div>
      </div>
      <div class="rev-meta">
        <p><strong>{{ t('reveng.runId') }}:</strong> <span class="rev-path">{{ detail.run_id }}</span></p>
        <p><strong>{{ t('reveng.subject') }}:</strong> <span class="rev-path">{{ detail.subject_name }}</span></p>
        <p><strong>{{ t('reveng.time') }}:</strong> {{ detail.created_at }}</p>
        <p><strong>{{ t('reveng.duration') }}:</strong> {{ detail.duration_ms }} ms</p>
      </div>
      <table class="rev-table">
        <thead>
          <tr>
            <th>{{ t('reveng.stage') }}</th>
            <th>{{ t('reveng.state') }}</th>
            <th>{{ t('reveng.effective') }}</th>
            <th>{{ t('reveng.duration') }}</th>
            <th>{{ t('reveng.summary') }}</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="([key, st]) in detailStageEntries" :key="String(key)">
            <td>{{ st.stage }}</td>
            <td><span :class="['badge', stageBadge(st.status)]">{{ st.status }}</span></td>
            <td>{{ st.engine }}</td>
            <td>{{ st.duration_ms }} ms</td>
            <td>{{ st.summary }}</td>
          </tr>
        </tbody>
      </table>
      <p v-if="detail.evidence_bundle_id" class="rev-note">
        Evidence v2：{{ detail.evidence_bundle_id }} ·
        {{ t('reveng.items') }} {{ detail.evidence_report?.items_checked ?? 0 }} ·
        {{ detail.evidence_report?.ok ? t('reveng.verified') : t('reveng.verifyFailed') }}
      </p>

      <!-- 功能等价重建：对 exe 逆向的最终产物（新 exe） -->
      <div v-if="isExecutableSubject || rebuildInfo" class="rev-rebuild">
        <div class="rev-rebuild-head">
          <span class="rev-rebuild-title">{{ t('reveng.rebuildTitle') }}</span>
          <button class="btn-xs btn-ghost" :disabled="rebuilding" @click="doRebuild">
            {{ rebuilding ? t('reveng.rebuilding') : t('reveng.rebuild') }}
          </button>
        </div>
        <p v-if="!rebuildInfo" class="rev-note">{{ t('reveng.rebuildHint') }}</p>
        <template v-else-if="rebuildInfo.ok">
          <p class="rev-rebuild-ok">
            {{ t('reveng.rebuildDone') }}：{{ rebuildInfo.exe_name }}
            （{{ (rebuildInfo.size || 0).toLocaleString() }} B<template v-if="rebuildInfo.kind"> · {{ rebuildInfo.kind }}</template>）
          </p>
          <p class="rev-note rev-path">{{ rebuildInfo.exe_path }}</p>
          <p class="rev-note">sha256 {{ rebuildInfo.sha256.slice(0, 24) }}…</p>
        </template>
        <p v-else class="rev-error">{{ t('reveng.rebuildFailed') }}：{{ rebuildInfo.error }}</p>
        <p v-if="rebuildError" class="rev-error">{{ rebuildError }}</p>
      </div>

      <p v-if="detail.run_dir" class="rev-note">{{ detail.run_dir }}</p>
    </section>
    <p v-if="loading" class="rev-note">{{ t('reveng.loading') }}</p>
  </div>
</template>

<style scoped>
/* 页面根容器 —— 与 KnowledgeBaseView 等页面对齐的居中卡片布局 */
.rev-view {
  max-width: 1080px;
  margin: 0 auto;
  padding: var(--space-md);
  font-family: var(--font-sans);
  color: var(--color-text);
}

/* 页面头部 */
.rev-header {
  display: flex;
  align-items: baseline;
  gap: var(--space-md);
  margin-bottom: var(--space-lg);
  flex-wrap: wrap;
}
.rev-back {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: var(--font-size-sm);
  color: var(--color-text-secondary);
  text-decoration: none;
}
.rev-back:hover { color: var(--color-primary); }
.rev-branding {
  display: flex;
  flex-direction: column;
  gap: var(--space-xs);
}
.rev-title {
  font-size: var(--font-size-lg);
  font-weight: 700;
  margin: 0;
  color: var(--color-text);
}
.rev-subtitle {
  font-size: var(--font-size-xs);
  color: var(--color-text-secondary);
  margin: 0;
}

/* 错误提示 */
.rev-error {
  color: var(--color-error);
  background: var(--color-error-bg);
  border: 1px solid var(--border-error);
  border-radius: var(--radius-sm);
  padding: var(--space-xs) var(--space-sm);
  font-size: var(--font-size-xs);
  margin-bottom: var(--space-sm);
}

/* 卡片（与 .kb-card 对齐） */
.rev-card {
  background: var(--bg-surface);
  border: 1px solid var(--border-color);
  border-radius: var(--radius-md);
  padding: var(--space-lg);
  margin-bottom: var(--space-md);
  box-shadow: var(--shadow-sm);
}
.rev-card-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: var(--space-md);
}
.rev-card-title {
  font-size: var(--font-size-sm);
  font-weight: 700;
  margin: 0;
  color: var(--color-text);
}

/* 表格 */
.rev-table {
  width: 100%;
  border-collapse: collapse;
  font-size: var(--font-size-xs);
}
.rev-table th,
.rev-table td {
  padding: var(--space-xs) var(--space-sm);
  border-bottom: 1px solid var(--border-light);
  text-align: left;
  color: var(--color-text);
}
.rev-table th {
  background: var(--bg-surface);
  font-weight: 700;
  color: var(--color-text-secondary);
}
.rev-table tr:hover {
  background: var(--bg-hover);
}
.rev-path {
  font-family: var(--font-mono);
  font-size: var(--font-size-xs);
  word-break: break-all;
  color: var(--color-text-secondary);
}

/* 运行历史：固定高度 + 纵向滚动，超出时区域内滚动查看 */
.rev-history-scroll {
  max-height: 260px;
  overflow-y: auto;
  border: 1px solid var(--border-color);
  border-radius: var(--radius-sm);
}
.rev-history-scroll .rev-table th {
  position: sticky;
  top: 0;
  background: var(--bg-surface);
  z-index: 1;
}
.rev-table th:first-child,
.rev-table td:first-child {
  text-align: left;
}
.rev-table th:last-child,
.rev-table td:last-child {
  text-align: left;
}

/* 操作区 */
.rev-actions {
  display: flex;
  gap: var(--space-sm);
  align-items: center;
}

/* 表单 */
.rev-form {
  display: flex;
  gap: var(--space-sm);
  flex-wrap: wrap;
  align-items: center;
}
.rev-file-form {
  flex-wrap: wrap;
  align-items: center;
}
.rev-file-input {
  display: none;
}
.rev-file-name {
  margin-top: var(--space-xs);
  font-size: var(--font-size-xs);
  color: var(--color-text-secondary);
  word-break: break-all;
}
.rev-recognition {
  margin-top: var(--space-sm);
  padding: var(--space-xs) var(--space-sm);
  background: var(--bg-hover);
  border-radius: var(--radius-sm);
}
.rev-note {
  margin-top: var(--space-sm);
  font-size: var(--font-size-xs);
  color: var(--color-text-secondary);
}
.rev-why {
  color: var(--color-text-tertiary);
}
.rev-empty {
  margin: 0;
  padding: var(--space-md);
  font-size: var(--font-size-xs);
  color: var(--color-text-secondary);
  text-align: center;
}
/* 操作区（已使用全局 btn-* 类，此处保留 flex 布局） */
.rev-actions {
  display: flex;
  gap: var(--space-sm);
  align-items: center;
}

/* 实时进展面板 */
.rev-live-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
}
.rev-live-runid {
  font-size: var(--font-size-xs);
  color: var(--color-text-tertiary);
  font-family: var(--font-mono);
}
.rev-overall {
  display: flex;
  align-items: center;
  gap: var(--space-sm);
  margin: var(--space-sm) 0 var(--space-md);
}
.rev-bar {
  flex: 1;
  height: var(--space-xs);
  background: var(--bg-input);
  border-radius: var(--radius-sm);
  overflow: hidden;
}
.rev-bar.sm {
  height: 4px;
}
.rev-bar-fill {
  height: 100%;
  background: var(--color-primary);
  border-radius: var(--radius-sm);
  transition: width var(--transition-base) ease;
}
.rev-overall-text {
  font-size: var(--font-size-xs);
  color: var(--color-text-secondary);
  white-space: nowrap;
}
.rev-current {
  margin: 0 0 var(--space-sm);
  font-size: var(--font-size-sm);
  color: var(--color-primary);
  font-weight: 600;
}
.rev-phases {
  list-style: none;
  margin: 0;
  padding: 0;
}
.rev-phase {
  padding: var(--space-xs) var(--space-sm);
  border-radius: var(--radius-sm);
  border-left: 3px solid var(--border-color);
  margin-bottom: var(--space-xs);
}
.rev-phase.is-active {
  border-left-color: var(--color-primary);
  background: var(--bg-hover);
}
.rev-phase.done {
  border-left-color: var(--color-success);
}
.rev-phase.skipped {
  border-left-color: var(--color-warning);
}
.rev-phase.failed {
  border-left-color: var(--color-danger);
}
.rev-phase-row {
  display: flex;
  align-items: center;
  gap: var(--space-xs);
  font-size: var(--font-size-xs);
}
.rev-phase-index {
  color: var(--color-text-tertiary);
}
.rev-phase-label {
  flex: 1;
  text-align: left;
}
.rev-phase-badge {
  font-size: var(--font-size-xs);
  padding: 1px var(--space-xs);
  border-radius: var(--radius-full);
  background: var(--bg-input);
  color: var(--color-text-secondary);
}
.rev-phase.running .rev-phase-badge {
  background: var(--color-primary);
  color: var(--color-text-inverse);
}
.rev-phase.done .rev-phase-badge {
  background: var(--color-success);
  color: var(--color-text-inverse);
}
.rev-phase.skipped .rev-phase-badge {
  background: var(--color-warning);
  color: var(--color-text-inverse);
}
.rev-phase.failed .rev-phase-badge {
  background: var(--color-danger);
  color: #fff;
}
.rev-phase-prog {
  display: flex;
  align-items: center;
  gap: var(--space-sm);
  margin-top: var(--space-xs);
  padding-left: var(--space-md);
}
.rev-phase-step {
  font-size: var(--font-size-xs);
  color: var(--color-text-secondary);
  white-space: nowrap;
}
.rev-phase-sum {
  margin-top: var(--space-xs);
  padding-left: var(--space-md);
  font-size: var(--font-size-xs);
  color: var(--color-text-secondary);
}
.rev-live-actions {
  display: flex;
  gap: var(--space-sm);
  align-items: center;
  margin-top: var(--space-sm);
}

/* 功能等价重建（新 exe 产物） */
.rev-rebuild {
  margin-top: var(--space-md);
  padding-top: var(--space-md);
  border-top: 1px dashed var(--border-color);
}
.rev-rebuild-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: var(--space-xs);
}
.rev-rebuild-title {
  font-size: var(--font-size-xs);
  font-weight: 700;
  color: var(--color-text);
}
.rev-rebuild-ok {
  margin-bottom: var(--space-xs);
  font-size: var(--font-size-xs);
  color: var(--color-success);
}
.rev-path {
  font-family: var(--font-mono);
  font-size: var(--font-size-xs);
  word-break: break-all;
  color: var(--color-text-secondary);
}
</style>
