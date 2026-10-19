<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { apiClient } from '../api/client'
import { currentOrigin } from '../api/runtime'
import { getToken } from '../api/token'
import { useLanguage } from '../composables/useLanguage'

const { t } = useLanguage()
const router = useRouter()

type Session = { id: string; label: string; status: string; counters?: any; error?: any; updated_at?: string }
type StepView = { sid: string; index: number; action: string; label: string; hit_index: number; hit_kind: string; status: string; code?: string; message?: string }
type RunState = { run_id: string; status: string; total_steps: number; steps: StepView[]; error_code?: string; error_message?: string; stats?: any }

const available = ref(false)
const activeSession = ref<string | null>(null)
const recState = ref('')
const sessions = ref<Session[]>([])
const workflows = ref<{ id: string; name: string }[]>([])
const selected = ref<string | null>(null)
const compileOut = ref<any>(null)
const dryOut = ref<any>(null)
const recording = ref(false)
const busy = ref(false)
// 录制交互状态：行内标签 + 实时计数 + 保存中
const recLabel = ref('')
const recEvents = ref(0)
const recStartedAt = ref('')
const stopping = ref(false)
const nowTick = ref(Date.now())
// 后端仍在运行的 run（可能来自其他页面/刷新前）：必须有可见的中止入口，
// 否则会被"已有回放进行中"一直挡住（真机坐实）。
const runningRuns = ref<string[]>([])
const lastRec = ref<{ sid: string; events: number } | null>(null)
const error = ref('')
const notice = ref('')
let statusTimer: ReturnType<typeof setInterval> | null = null

// 运行状态 + SSE
const run = ref<RunState | null>(null)
const es = ref<EventSource | null>(null)
const streamFinal = ref(false)
const runPanel = ref<HTMLElement | null>(null)

/** 顶部常驻状态条的数据（不依赖滚动位置）。 */
// 五步引导：准备 → 录制 → 解析 → 发布 → 回放
const currentStep = computed(() => {
  if (recording.value) return 2
  if (selected.value) {
    if (compileOut.value && compileOut.value.workflow_id) return 5
    if (compileOut.value) return 4
    return 3
  }
  return 1
})

const guideSteps = computed(() => [
  { n: 1, title: t.value('repeat.guidePrepare'), desc: t.value('repeat.guidePrepareDesc') },
  { n: 2, title: t.value('repeat.guideRecord'), desc: t.value('repeat.guideRecordDesc') },
  { n: 3, title: t.value('repeat.guideCompile'), desc: t.value('repeat.guideCompileDesc') },
  { n: 4, title: t.value('repeat.guidePublish'), desc: t.value('repeat.guidePublishDesc') },
  { n: 5, title: t.value('repeat.guideReplay'), desc: t.value('repeat.guideReplayDesc') },
])

async function parseLastRec() {
  if (!lastRec.value) return
  selected.value = lastRec.value.sid
  await doCompile(false)
}

// 当前页面未跟踪、但后端仍在跑的 run（刷新页面/从别处启动的常见情形）
const activeRuns = computed(() =>
  (runningRuns.value || []).filter((id) => id !== run.value?.run_id))

async function abortActiveRun() {
  const id = activeRuns.value[0]
  if (!id) return
  error.value = ''
  try {
    await apiClient.post(`/desktop-repeat/runs/${id}/abort`, { reason: 'user' })
    notice.value = t.value('repeat.abortedOne')
    await refreshStatus()
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  }
}

const recStateText = computed(() => {
  const key = {
    arming: 'repeat.stateArming', countdown: 'repeat.stateCountdown',
    recording: 'repeat.stateRecording', paused: 'repeat.statePaused',
    finalizing: 'repeat.stateFinalizing',
  }[recState.value] || 'repeat.stateRecording'
  return t.value(key)
})

const recDuration = computed(() => {
  if (!recStartedAt.value) return ''
  const t0 = Date.parse(recStartedAt.value)
  if (!t0) return ''
  const sec = Math.max(0, Math.floor((nowTick.value - t0) / 1000))
  const m = Math.floor(sec / 60)
  return m > 0 ? `${m}:${String(sec % 60).padStart(2, '0')}` : `${sec}s`
})

const runBar = computed(() => {
  const r = run.value
  if (!r) return { done: 0, total: 0, cur: '' }
  // 降级完成（degraded）与跳过（skipped）同样是"已处置"步骤；只数 'done'
  // 会让进度条在降级场景虚低（体检 P2-6）。后端 stats() 同口径（done+degraded）。
  const done = r.steps.filter((x) => ['done', 'degraded', 'skipped'].includes(x.status)).length
  const cur = r.steps.find((x) => x.status === 'running' || x.status === 'suspended')
  return { done, total: r.total_steps || r.steps.length, cur: cur?.label || '' }
})

const runBadgeClass = computed(() => {
  const st = run.value?.status || ''
  if (st === 'succeeded') return 'is-ok'
  if (st === 'failed' || st === 'aborted' || st === 'interrupted') return 'is-bad'
  if (st === 'suspended') return 'is-warn'
  return 'is-run'
})

const barTitle = computed(() => {
  const st = run.value?.status || ''
  const key = {
    running: 'repeat.barRunning', precheck: 'repeat.barRunning',
    suspended: 'repeat.barSuspended', succeeded: 'repeat.barDone',
    failed: 'repeat.barFailed', aborted: 'repeat.barAborted',
    interrupted: 'repeat.barInterrupted',
  }[st] || 'repeat.barRunning'
  return t.value(key)
})

function scrollToRun() {
  runPanel.value?.scrollIntoView({ behavior: 'smooth', block: 'center' })
}

async function refreshStatus() {
  try {
    const wasRecording = recording.value
    const s = await apiClient.get<any>('/desktop-repeat/status')
    available.value = !!s.available
    activeSession.value = s.active_session || null
    recState.value = s.recording_state || ''
    recEvents.value = Math.max(0, Number(s.recording_events || 0))
    if (s.recording_started_at) recStartedAt.value = s.recording_started_at
    nowTick.value = Date.now()
    recording.value = !!s.active_session
    runningRuns.value = Array.isArray(s.running_runs) ? s.running_runs : []
    // 热键/覆盖层自行结束时服务会立即收口：这里同步刷新列表与计数
    if (wasRecording && !recording.value) {
      notice.value = t.value('repeat.recEnded')
      await fetchSessions()
    }
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  }
}
async function fetchSessions() {
  try {
    const r = await apiClient.get<any>('/desktop-repeat/sessions?limit=50')
    sessions.value = r.items || []
  } catch {
    /* 未激活时静默 */
  }
}
async function fetchWorkflows() {
  try {
    workflows.value = (await apiClient.get<any[]>('/workflows')).map((w) => ({ id: w.id, name: w.name }))
  } catch {
    workflows.value = []
  }
}

async function startRec() {
  busy.value = true
  error.value = ''
  try {
    // 行内标签（不再用 window.prompt 阻塞对话框）；留空自动生成
    const stamp = new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
    const label = recLabel.value.trim() || `${t.value('repeat.defaultLabel')} ${stamp}`
    const doc = await apiClient.post<any>('/desktop-repeat/sessions', { label })
    activeSession.value = doc.session_id
    recording.value = true
    recEvents.value = 0
    recStartedAt.value = doc.created_at || new Date().toISOString()
    recLabel.value = ''
    notice.value = t.value('repeat.recStarted')
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    busy.value = false
  }
}
async function stopRec(discard = false) {
  if (stopping.value) return
  if (!activeSession.value && !recording.value) {
    error.value = t.value('repeat.noActive')
    return
  }
  stopping.value = true
  busy.value = true
  error.value = ''
  try {
    const doc = await apiClient.post<any>(`/desktop-repeat/sessions/${activeSession.value || ''}/stop`, { discard })
    recording.value = false
    recState.value = ''
    activeSession.value = null
    if (!discard) selected.value = doc.session_id
    await fetchSessions()
    const n = Number(doc?.counters?.events || 0)
    if (!discard && doc.status === 'ready') {
      lastRec.value = { sid: doc.session_id || activeSession.value || '', events: n }
    }
    if (!discard && doc.status === 'ready' && n < 5) {
      // 空录制是最常见的踩坑：给出明确原因与下一步，而不是"已保存"
      notice.value = t.value('repeat.tooFewEvents').replace('{n}', String(n))
    } else {
      notice.value = doc.status === 'ready'
        ? t.value('repeat.stopReady')
        : doc.status === 'discarded' ? t.value('repeat.discarded') : t.value('repeat.stopFailed')
    }
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    busy.value = false
    stopping.value = false
  }
}
async function pause() {
  try {
    await apiClient.post('/desktop-repeat/sessions/pause', {})
    notice.value = t.value('repeat.paused')
    await refreshStatus()
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  }
}
async function resume() {
  try {
    await apiClient.post('/desktop-repeat/sessions/resume', {})
    notice.value = t.value('repeat.resumed')
    await refreshStatus()
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  }
}

async function openSession(sid: string) {
  selected.value = sid
  compileOut.value = null
  try {
    await apiClient.get<any>(`/desktop-repeat/sessions/${sid}`)
  } catch {
    /* ignore */
  }
}
async function doCompile(save = false) {
  if (!selected.value) return
  busy.value = true
  error.value = ''
  try {
    const r = await apiClient.post<any>(`/desktop-repeat/sessions/${selected.value}/compile`,
      { save, name: sessions.value.find((s) => s.id === selected.value)?.label || '' })
    compileOut.value = r
    notice.value = t.value('repeat.compiled')
    if (save && r.workflow_id) {
      await fetchWorkflows()
      router.push(`/workflows/${r.workflow_id}`)
    }
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    busy.value = false
  }
}
async function removeSession(sid: string) {
  if (!window.confirm(t.value('repeat.confirmDelete'))) return
  try {
    await apiClient.delete(`/desktop-repeat/sessions/${sid}`)
    if (selected.value === sid) {
      selected.value = null
      compileOut.value = null
    }
    await fetchSessions()
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  }
}

async function dryRun(wfid: string) {
  busy.value = true
  error.value = ''
  try {
    const r = await apiClient.post<any>('/desktop-repeat/dry-run', { workflow_id: wfid })
    // 干跑报告独立保存：不能覆盖解析结果（旧实现把 compileOut 换成 {dry}，步骤列表消失）
    dryOut.value = r
    notice.value = r.ok ? t.value('repeat.dryOk') : t.value('repeat.dryMiss')
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    busy.value = false
  }
}
async function exportSkill(wfid: string) {
  try {
    const r = await apiClient.post<any>(`/desktop-repeat/workflows/${wfid}/skill`, {})
    notice.value = t.value('repeat.skillExported') + ': ' + (r.skill_dir || '')
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  }
}
function editWorkflow(wfid: string) {
  router.push(`/workflows/${wfid}`)
}

// 参数录入：支持 "name=值" 键值对，或按缺参顺序只填值。
// 旧实现只认 name=值，用户按提示顺序填值会得到空参数、二次 rejected（真机复现）。
function parseParamInput(raw: string, defs: { name: string }[]): Record<string, string> {
  const out: Record<string, string> = {}
  const text = (raw || '').trim()
  if (!text) return out
  if (text.includes('=')) {
    text.split(/[;\n]/).forEach((seg) => {
      seg.split(',').forEach((kv) => {
        const i = kv.indexOf('=')
        if (i > 0) out[kv.slice(0, i).trim()] = kv.slice(i + 1).trim()
      })
    })
    return out
  }
  const parts = text.split(/[;\n,]/).map((s) => s.trim())
  defs.forEach((d, i) => {
    if (parts[i] !== undefined && parts[i] !== '') out[d.name] = parts[i]
  })
  return out
}

// 运行：先取参数定义（若有）弹表单；缺参时后端返回 rejected 清单
async function runWorkflow(wfid: string) {
  closeStream()
  error.value = ''
  notice.value = ''
  try {
    const res = await apiClient.post<any>('/desktop-repeat/runs', { workflow_id: wfid, params: {} })
    if (res.rejected) {
      const defs: { name: string; required?: boolean; default?: string; description?: string }[] =
        res.params || []
      if (!defs.length) {
        error.value = res.error?.message || t.value('repeat.stopFailed')
        return
      }
      const names = defs.map((d) => d.name + (d.required ? '*' : '')).join(', ')
      const hint = t.value('repeat.paramHint').replace('{names}', names)
      const vals = window.prompt(hint, defs.map((d) => `${d.name}=`).join(', '))
      if (vals === null) return
      const params = parseParamInput(vals, defs)
      if (!Object.keys(params).length) {
        error.value = hint
        return
      }
      const again = await apiClient.post<any>('/desktop-repeat/runs', { workflow_id: wfid, params })
      if (!again.rejected) openStream(again.run_id)
      else error.value = again.error?.message || 'param rejected'
      return
    }
    openStream(res.run_id)
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  }
}
function openStream(runId: string) {
  const origin = currentOrigin()
  const token = getToken()
  const qs = token ? `?token=${encodeURIComponent(token)}` : ''
  const s = new EventSource(`${origin}/api/desktop-repeat/runs/${runId}/events${qs}`)
  es.value = s
  run.value = { run_id: runId, status: 'running', total_steps: 0, steps: [] }
  streamFinal.value = false
  // 运行区在页面中列底部：启动后自动滚过去，避免"点了回放页面毫无变化"
  nextTick(() => setTimeout(scrollToRun, 200))
  s.onmessage = (ev: MessageEvent) => {
    let d: any
    try { d = JSON.parse(ev.data) } catch { return }
    onEvent(d)
  }
  s.onerror = () => {
    if (!streamFinal.value) closeStream()
  }
}
function onEvent(d: any) {
  if (!run.value) return
  switch (d.type) {
    case 'run_started':
      run.value.total_steps = d.total_steps
      break
    case 'step_started': {
      // 重试同一步骤时不新增行，原地重置（否则时间线出现重复 sid 行）
      const ex = run.value.steps.find((x) => x.sid === d.sid)
      if (ex) {
        ex.index = d.index
        ex.action = d.action
        ex.label = d.label
        ex.status = 'running'
        ex.hit_index = -1
        ex.hit_kind = ''
        ex.code = ''
        ex.message = ''
      } else {
        run.value.steps.push({ sid: d.sid, index: d.index, action: d.action, label: d.label,
          hit_index: -1, hit_kind: '', status: 'running' })
      }
      break
    }
    case 'step_done': {
      const st = run.value.steps.find((x) => x.sid === d.sid)
      if (st) { st.status = d.status; st.hit_index = d.hit_index; st.hit_kind = d.kind }
      break
    }
    case 'step_failed': {
      const st = run.value.steps.find((x) => x.sid === d.sid)
      if (st) { st.status = 'failed'; st.code = d.code; st.message = d.message }
      break
    }
    case 'run_suspended': {
      run.value.status = 'suspended'
      notice.value = t.value('repeat.suspended')
      break
    }
    case 'notice':
      if (d.title) notice.value = d.title + (d.body ? ' · ' + d.body : '')
      break
    case 'run_done':
      run.value.status = 'succeeded'
      notice.value = t.value('repeat.runDone')  // 覆盖"等待人工"等中间提示
      run.value.stats = d.record?.stats
      streamFinal.value = true
      closeStream()
      break
    case 'run_error':
      run.value.status = 'failed'
      error.value = (d.code ? d.code + ' ' : '') + (d.message || 'run error')
      streamFinal.value = true
      closeStream()
      break
    default:
      break
  }
}
async function resumeRun(decision: string) {
  if (!run.value) return
  try {
    await apiClient.post(`/desktop-repeat/runs/${run.value.run_id}/resume`, { decision })
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  }
}
async function abortRun() {
  if (!run.value) return
  try {
    await apiClient.post(`/desktop-repeat/runs/${run.value.run_id}/abort`, {})
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  }
}
function closeStream() {
  if (es.value) {
    try { es.value.close() } catch { /* ignore */ }
    es.value = null
  }
}

const stepClass = (s: StepView) => ({
  'rp-step': true,
  'rp-done': s.status === 'done' || s.status === 'degraded',
  'rp-run': s.status === 'running',
  'rp-fail': s.status === 'failed',
  'rp-skip': s.status === 'skipped',
})

onMounted(async () => {
  await refreshStatus()
  await fetchSessions()
  await fetchWorkflows()
  // 录制状态轮询：热键/覆盖层结束录制时后端会立即收口，页面需同步（否则一直显示录制中）
  statusTimer = setInterval(refreshStatus, 3000)
})
// 滚动条自动隐藏：仅在滚动交互时显示，静止 1.2s 后淡出
function onScrollCapture(ev: Event) {
  const el = ev.target as HTMLElement | null
  if (!el || !el.classList || !el.classList.contains('rp-scroll')) return
  el.classList.add('is-scrolling')
  const w = el as HTMLElement & { _sbTimer?: number }
  if (w._sbTimer) window.clearTimeout(w._sbTimer)
  w._sbTimer = window.setTimeout(() => el.classList.remove('is-scrolling'), 1200)
}

onMounted(() => { document.addEventListener('scroll', onScrollCapture, true) })
onUnmounted(() => {
  document.removeEventListener('scroll', onScrollCapture, true)
  if (statusTimer) {
    clearInterval(statusTimer)
    statusTimer = null
  }
  closeStream()
})
</script>

<template>
  <div class="rp-page">
    <div class="rp-shell rp-scroll">
      <!-- 顶部：标题 + 主操作 -->
      <header class="rp-topbar">
        <div class="rp-topbar-main">
          <router-link to="/chat" class="rp-back">← {{ t('insights.back') }}</router-link>
          <h1 class="rp-title">{{ t('repeat.title') }}</h1>
          <p class="rp-sub">{{ t('repeat.subtitle') }}</p>
        </div>
        <div class="rp-topbar-actions">
          <span v-if="!available" class="rp-badge is-warn">{{ t('repeat.unavailable') }}</span>
          <template v-else-if="!recording">
            <input v-model="recLabel" class="rp-input" :disabled="busy"
                   :placeholder="t('repeat.labelPlaceholder')" @keyup.enter="startRec" />
            <button class="rp-btn is-primary" :disabled="busy" @click="startRec">
              {{ t('repeat.start') }}
            </button>
          </template>
        </div>
      </header>

      <!-- 五步引导 -->
      <ol class="rp-stepper rp-scroll">
        <li v-for="g in guideSteps" :key="g.n"
            :class="{ 'is-active': currentStep === g.n, 'is-done': currentStep > g.n }">
          <span class="rp-stepper-num">{{ currentStep > g.n ? '✓' : g.n }}</span>
          <span class="rp-stepper-text">
            <span class="rp-stepper-title">{{ g.title }}</span>
            <span v-if="currentStep === g.n" class="rp-stepper-desc">{{ g.desc }}</span>
          </span>
        </li>
      </ol>

      <!-- 录制状态条 -->
      <div v-if="recording" class="rp-banner is-recording">
        <span class="rp-banner-dot"></span>
        <strong class="rp-banner-title">{{ recStateText }}</strong>
        <span class="rp-banner-meta">
          {{ recEvents }} 事件<template v-if="recDuration"> · {{ recDuration }}</template>
        </span>
        <span class="rp-banner-msg">{{ t('repeat.recHotkeys') }}</span>
        <span class="rp-banner-ops">
          <button v-if="recState !== 'paused'" class="rp-btn is-ghost" :disabled="busy" @click="pause">{{ t('repeat.pause') }}</button>
          <button v-else class="rp-btn is-ghost" :disabled="busy" @click="resume">{{ t('repeat.resume') }}</button>
          <button class="rp-btn is-primary" :disabled="busy || stopping" @click="stopRec(false)">
            {{ stopping ? t('repeat.saving') : t('repeat.stop') }}
          </button>
          <button class="rp-btn is-danger" :disabled="busy || stopping" @click="stopRec(true)">{{ t('repeat.discard') }}</button>
        </span>
      </div>

      <!-- 回放状态条 -->
      <div v-if="run" class="rp-banner" :class="'is-' + run.status">
        <span class="rp-banner-dot"></span>
        <strong class="rp-banner-title">{{ barTitle }}</strong>
        <span v-if="runBar.total" class="rp-banner-meta">
          {{ runBar.done }}/{{ runBar.total }}<template v-if="runBar.cur"> · {{ runBar.cur }}</template>
        </span>
        <span v-if="notice" class="rp-banner-msg">{{ notice }}</span>
        <span class="rp-banner-ops">
          <template v-if="run.status === 'suspended'">
            <button class="rp-btn is-ghost" @click="resumeRun('retry')">{{ t('repeat.continue') }}</button>
            <button class="rp-btn is-danger" @click="resumeRun('abort')">{{ t('repeat.abort') }}</button>
          </template>
          <button v-else-if="run.status === 'running'" class="rp-btn is-danger" @click="abortRun">{{ t('repeat.abort') }}</button>
          <button class="rp-btn is-ghost" @click="scrollToRun">{{ t('repeat.viewRun') }}</button>
        </span>
      </div>

      <!-- 后端仍在运行、但本页未跟踪的回放：必须给中止入口，
           否则每次点回放都被"已有回放进行中"拒绝且无处取消（真机坐实）。 -->
      <div v-if="activeRuns.length" class="rp-banner is-running">
        <span class="rp-banner-dot"></span>
        <strong class="rp-banner-title">{{ t('repeat.runBusyTitle') }}</strong>
        <span class="rp-banner-meta">{{ activeRuns.length }}</span>
        <span class="rp-banner-msg">{{ activeRuns[0] }}</span>
        <span class="rp-banner-ops">
          <button class="rp-btn is-danger" @click="abortActiveRun">{{ t('repeat.abort') }}</button>
        </span>
      </div>

      <!-- 提示 -->
      <p v-if="notice && !run" class="rp-toast">{{ notice }}</p>
      <p v-if="error" class="rp-toast is-error">{{ error }}</p>

      <!-- 录制前准备清单 -->
      <section v-if="!recording && !selected && !run" class="rp-card rp-callout">
        <h2 class="rp-callout-title">{{ t('repeat.prepareTitle') }}</h2>
        <ol class="rp-callout-list">
          <li>{{ t('repeat.prepare1') }}</li>
          <li>{{ t('repeat.prepare2') }}</li>
          <li>{{ t('repeat.prepare3') }}</li>
        </ol>
        <p class="rp-callout-note">{{ t('repeat.prepareNote') }}</p>
      </section>

      <!-- 录制完成 -->
      <section v-if="lastRec && !recording" class="rp-card rp-next" :class="{ 'is-warn': lastRec.events < 5 }">
        <div class="rp-next-main">
          <strong class="rp-next-title">{{ t('repeat.recDone') }}</strong>
          <span class="rp-muted">{{ lastRec.events }} {{ t('repeat.eventsUnit') }}</span>
        </div>
        <div class="rp-next-ops">
          <template v-if="lastRec.events >= 5">
            <button class="rp-btn is-primary" :disabled="busy" @click="parseLastRec">{{ t('repeat.parseNow') }}</button>
          </template>
          <template v-else>
            <span class="rp-next-warn">{{ t('repeat.tooFewShort') }}</span>
            <button class="rp-btn is-primary" :disabled="busy" @click="startRec">{{ t('repeat.start') }}</button>
          </template>
        </div>
      </section>

      <div class="rp-layout">
        <aside class="rp-sidebar">
          <section class="rp-card">
            <header class="rp-card-head">
              <h2 class="rp-card-title">{{ t('repeat.sessions') }}</h2>
              <span class="rp-count">{{ sessions.length }}</span>
            </header>
            <ul class="rp-list rp-scroll">
              <li v-for="s in sessions" :key="s.id" class="rp-item"
                  :class="{ 'is-selected': selected === s.id }" @click="openSession(s.id)">
                <span class="rp-dot" :class="'is-' + s.status"></span>
                <span class="rp-item-main">{{ s.label || s.id }}</span>
                <span class="rp-item-meta">{{ s.counters?.events || 0 }}</span>
                <button class="rp-icon-btn" :title="t('repeat.delete')" @click.stop="removeSession(s.id)">×</button>
              </li>
              <li v-if="!sessions.length" class="rp-empty">{{ t('repeat.noSessions') }}</li>
            </ul>
          </section>

          <section class="rp-card">
            <header class="rp-card-head">
              <h2 class="rp-card-title">{{ t('repeat.flows') }}</h2>
              <span class="rp-count">{{ workflows.length }}</span>
            </header>
            <ul class="rp-list rp-scroll">
              <li v-for="w in workflows" :key="w.id" class="rp-flow">
                <span class="rp-item-main">{{ w.name }}</span>
                <div class="rp-flow-ops">
                  <button class="rp-btn is-ghost is-xs" @click="dryRun(w.id)">{{ t('repeat.dry') }}</button>
                  <button class="rp-btn is-primary is-xs" @click="runWorkflow(w.id)">{{ t('repeat.run') }}</button>
                  <button class="rp-btn is-ghost is-xs" @click="editWorkflow(w.id)">{{ t('repeat.edit') }}</button>
                  <button class="rp-btn is-ghost is-xs" @click="exportSkill(w.id)">{{ t('repeat.skill') }}</button>
                </div>
              </li>
              <li v-if="!workflows.length" class="rp-empty">{{ t('repeat.noFlows') }}</li>
            </ul>
          </section>
        </aside>

        <main class="rp-content">
          <!-- 解析结果 -->
          <section v-if="selected" class="rp-card">
            <header class="rp-card-head">
              <h2 class="rp-card-title">{{ t('repeat.compile') }}</h2>
              <div class="rp-card-ops">
                <button class="rp-btn is-ghost" :disabled="busy" @click="doCompile(false)">{{ t('repeat.compileBtn') }}</button>
                <button class="rp-btn is-primary" :disabled="busy" @click="doCompile(true)">{{ t('repeat.publish') }}</button>
              </div>
            </header>
            <p v-if="compileOut && compileOut.stats" class="rp-stats">
              {{ compileOut.stats.steps }} steps · {{ compileOut.stats.needs_review }} review ·
              {{ compileOut.stats.loop_candidates }} loop? · {{ compileOut.stats.param_candidates }} param? ·
              {{ compileOut.stats.duration_ms }}ms
            </p>
            <ul v-if="compileOut && compileOut.steps" class="rp-rows rp-scroll">
              <li v-for="s in compileOut.steps" :key="s.sid" class="rp-row"
                  :class="{ 'is-warn': (s.meta?.confidence ?? 1) < 0.5 || s.meta?.needs_review }">
                <span class="rp-row-id">{{ s.sid }}</span>
                <span class="rp-row-action">{{ s.action }}</span>
                <span class="rp-row-label">{{ s.label }}</span>
                <span class="rp-row-meta">{{ Math.round((s.meta?.confidence ?? 0) * 100) }}%</span>
              </li>
            </ul>
            <ul v-if="compileOut && compileOut.warnings && compileOut.warnings.length" class="rp-warns">
              <li v-for="(wn, i) in compileOut.warnings" :key="i" class="rp-warn">
                <span class="rp-warn-sev">{{ wn.severity }}</span>
                <span v-if="wn.sid" class="rp-warn-sid">{{ wn.sid }}</span>
                <span>{{ wn.message }}</span>
              </li>
            </ul>
            <p v-else-if="!compileOut" class="rp-empty">{{ t('repeat.compileHint') }}</p>

            <div v-if="dryOut" class="rp-subsection">
              <header class="rp-subhead">
                <h3 class="rp-subtitle">{{ t('repeat.dry') }}</h3>
                <span class="rp-muted">
                  {{ dryOut.summary?.hit ?? 0 }}/{{ dryOut.summary?.total ?? 0 }} 命中 ·
                  {{ dryOut.duration_ms ?? 0 }}ms
                </span>
              </header>
              <p v-if="dryOut.error" class="rp-toast is-error">{{ dryOut.error.code }} {{ dryOut.error.message }}</p>
              <ul class="rp-rows rp-scroll">
                <li v-for="s in dryOut.steps || []" :key="s.sid" class="rp-row"
                    :class="s.hit_index >= 0 ? 'is-ok' : 'is-bad'">
                  <span class="rp-row-id">{{ s.index + 1 }}</span>
                  <span class="rp-row-action">{{ s.action }}</span>
                  <span class="rp-row-label">{{ s.label }}</span>
                  <span class="rp-row-meta">{{ s.verdict }}</span>
                  <span v-if="s.message" class="rp-row-err">{{ s.message }}</span>
                </li>
              </ul>
            </div>
          </section>

          <section v-else class="rp-card rp-empty-state">
            <p class="rp-empty-title">{{ t('repeat.selectHint') }}</p>
          </section>

          <!-- 运行时间线 -->
          <section v-if="run" ref="runPanel" class="rp-card">
            <header class="rp-card-head">
              <h2 class="rp-card-title">{{ t('repeat.runTitle') }}</h2>
              <span class="rp-badge" :class="runBadgeClass">{{ barTitle }}</span>
              <div class="rp-card-ops">
                <template v-if="run.status === 'suspended'">
                  <button class="rp-btn is-ghost" @click="resumeRun('retry')">{{ t('repeat.continue') }}</button>
                  <button class="rp-btn is-danger" @click="resumeRun('abort')">{{ t('repeat.abort') }}</button>
                </template>
                <button v-if="run.status === 'running'" class="rp-btn is-danger" @click="abortRun">{{ t('repeat.abort') }}</button>
              </div>
            </header>
            <ul class="rp-rows rp-scroll">
              <li v-for="s in run.steps" :key="s.sid" class="rp-row" :class="stepClass(s)">
                <span class="rp-row-id">{{ s.index + 1 }}</span>
                <span class="rp-row-action">{{ s.action }}</span>
                <span class="rp-row-label">{{ s.label }}</span>
                <span v-if="s.hit_index >= 0" class="rp-row-meta">#{{ s.hit_index }} {{ s.hit_kind }}</span>
                <span v-if="s.code" class="rp-row-err">{{ s.code }}</span>
              </li>
            </ul>
          </section>

          <section class="rp-card rp-help">
            <h2 class="rp-card-title">{{ t('repeat.help') }}</h2>
            <ol class="rp-help-list">
              <li>{{ t('repeat.help1') }}</li>
              <li>{{ t('repeat.help2') }}</li>
              <li>{{ t('repeat.help3') }}</li>
              <li>{{ t('repeat.help4') }}</li>
            </ol>
            <p class="rp-muted rp-help-note">{{ t('repeat.helpNote') }}</p>
          </section>
        </main>
      </div>
    </div>
  </div>
</template>
<style scoped>
/* 全部使用全局设计令牌（--color-* / --bg-* / --radius-* / --shadow-* / --space-*），
   跟随 matrix/ocean/sunset/dark 主题，与其它页面保持一致的观感。 */

.rp-page {
  /* 固定整体高度：页面本身不滚动，由 .rp-shell 统一承载纵向滚动，
     避免"无限下滑"；超长内容一律走内部滚动容器。 */
  height: 100vh;
  height: 100dvh;
  overflow: hidden;
  padding: var(--space-lg);
  background: var(--bg-main);
  color: var(--color-text);
  font-family: var(--font-sans);
}
.rp-shell {
  max-width: 1320px; margin: 0 auto; height: 100%;
  display: flex; flex-direction: column; gap: var(--space-md);
  overflow-y: auto; padding-right: 4px;
  overscroll-behavior: contain;
}

/* ── 顶部 ───────────────────────────────────────────── */
.rp-topbar { display: flex; align-items: flex-end; justify-content: space-between; gap: var(--space-md); flex-wrap: wrap; }
.rp-topbar-main { min-width: 0; }
.rp-back { display: inline-block; font-size: var(--font-size-xs); color: var(--color-text-secondary); text-decoration: none; transition: color var(--transition-fast); }
.rp-back:hover { color: var(--color-primary); }
.rp-title { margin: 6px 0 2px; font-size: var(--font-size-2xl); font-weight: 700; letter-spacing: -0.02em; }
.rp-sub { margin: 0; font-size: var(--font-size-xs); color: var(--color-text-secondary); }
.rp-topbar-actions { display: flex; align-items: center; gap: var(--space-sm); flex-wrap: wrap; }

/* ── 输入 / 按钮 ────────────────────────────────────── */
.rp-input {
  width: 200px; padding: 9px 12px; font-family: inherit; font-size: var(--font-size-sm);
  color: var(--color-text); background: var(--bg-input); border: 1px solid var(--border-color);
  border-radius: var(--radius-md); transition: border-color var(--transition-fast), box-shadow var(--transition-fast);
}
.rp-input::placeholder { color: var(--color-text-tertiary); }
.rp-input:focus { outline: none; border-color: var(--color-primary); box-shadow: 0 0 0 3px var(--color-primary-light); }

.rp-btn {
  display: inline-flex; align-items: center; justify-content: center; gap: 6px;
  padding: 9px 14px; font-family: inherit; font-size: var(--font-size-sm); font-weight: 500;
  border: 1px solid transparent; border-radius: var(--radius-md); cursor: pointer;
  transition: background var(--transition-fast), color var(--transition-fast),
              border-color var(--transition-fast), transform var(--transition-fast);
}
.rp-btn:active:not(:disabled) { transform: translateY(1px); }
.rp-btn:disabled { opacity: .45; cursor: not-allowed; }
.rp-btn.is-primary { background: var(--color-primary); color: var(--color-text-inverse); }
.rp-btn.is-primary:hover:not(:disabled) { background: var(--color-primary-hover); box-shadow: var(--shadow-primary); }
.rp-btn.is-ghost { background: transparent; color: var(--color-text-secondary); border-color: var(--border-color); }
.rp-btn.is-ghost:hover:not(:disabled) { background: var(--bg-hover); color: var(--color-text); border-color: var(--border-strong); }
.rp-btn.is-danger { background: var(--color-danger-light); color: var(--color-danger); }
.rp-btn.is-danger:hover:not(:disabled) { background: var(--color-danger); color: #fff; }
.rp-btn.is-xs { padding: 4px 9px; font-size: var(--font-size-xs); border-radius: var(--radius-sm); }

.rp-badge {
  display: inline-flex; align-items: center; padding: 3px 8px; font-size: var(--font-size-xs);
  font-weight: 500; border-radius: var(--radius-full); background: var(--bg-tag); color: var(--color-primary);
}
.rp-badge.is-ok { background: var(--color-success-light); color: var(--color-success); }
.rp-badge.is-warn { background: var(--color-warning-light); color: var(--color-warning); }
.rp-badge.is-bad { background: var(--color-danger-light); color: var(--color-danger); }
.rp-badge.is-run { background: var(--color-primary-light); color: var(--color-primary); }

/* ── 五步引导 ───────────────────────────────────────── */
.rp-stepper { display: flex; gap: var(--space-sm); margin: 0; padding: 0; list-style: none; overflow-x: auto; }
.rp-stepper li {
  display: flex; align-items: center; gap: 10px; flex: 1 1 0; min-width: 148px;
  padding: 10px 12px; background: var(--bg-surface); border: 1px solid var(--border-color);
  border-radius: var(--radius-lg);
  transition: border-color var(--transition-base), background var(--transition-base);
}
.rp-stepper li.is-active { border-color: var(--color-primary); background: var(--color-primary-light); }
.rp-stepper-num {
  display: inline-flex; align-items: center; justify-content: center; flex: none;
  width: 22px; height: 22px; border-radius: var(--radius-full); font-size: var(--font-size-xs);
  background: var(--bg-input); color: var(--color-text-tertiary);
}
.rp-stepper li.is-active .rp-stepper-num { background: var(--color-primary); color: var(--color-text-inverse); }
.rp-stepper li.is-done .rp-stepper-num { background: var(--color-success-light); color: var(--color-success); }
.rp-stepper-text { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
.rp-stepper-title { font-size: var(--font-size-sm); font-weight: 600; color: var(--color-text); }
.rp-stepper li.is-active .rp-stepper-title { color: var(--color-primary); }
.rp-stepper-desc { font-size: var(--font-size-xs); color: var(--color-text-secondary);
  line-height: 1.35; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }

/* ── 状态条 ─────────────────────────────────────────── */
.rp-banner {
  position: sticky; top: 0; z-index: 20; display: flex; align-items: center; gap: var(--space-sm);
  padding: 10px 14px; font-size: var(--font-size-sm); background: var(--bg-surface);
  border: 1px solid var(--border-color); border-radius: var(--radius-lg); box-shadow: var(--shadow-sm);
}
.rp-banner-dot { width: 8px; height: 8px; flex: none; border-radius: var(--radius-full); background: var(--color-primary); }
.rp-banner-title { flex: none; font-weight: 600; }
.rp-banner-meta { flex: none; color: var(--color-text-secondary); font-variant-numeric: tabular-nums; }
.rp-banner-msg { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--color-text-secondary); }
.rp-banner-ops { margin-left: auto; display: flex; flex: none; gap: var(--space-sm); }
.rp-banner.is-running { border-color: var(--color-primary); background: var(--color-primary-light); }
.rp-banner.is-running .rp-banner-dot { background: var(--color-primary); animation: rp-pulse 1.4s infinite; }
.rp-banner.is-recording { border-color: var(--color-warning); background: var(--color-warning-light); }
.rp-banner.is-recording .rp-banner-dot { background: var(--color-warning); animation: rp-pulse 1.4s infinite; }
.rp-banner.is-suspended { border-color: var(--color-warning); background: var(--color-warning-light); }
.rp-banner.is-suspended .rp-banner-dot { background: var(--color-warning); }
.rp-banner.is-succeeded { border-color: var(--color-success); background: var(--color-success-light); }
.rp-banner.is-succeeded .rp-banner-dot { background: var(--color-success); }
.rp-banner.is-failed, .rp-banner.is-aborted, .rp-banner.is-interrupted { border-color: var(--color-danger); background: var(--color-danger-light); }
.rp-banner.is-failed .rp-banner-dot,
.rp-banner.is-aborted .rp-banner-dot,
.rp-banner.is-interrupted .rp-banner-dot { background: var(--color-danger); }
@keyframes rp-pulse { 0%, 100% { opacity: 1; } 50% { opacity: .25; } }

/* ── 提示 ───────────────────────────────────────────── */
.rp-toast { margin: 0; padding: 9px 12px; font-size: var(--font-size-sm); border-radius: var(--radius-md);
  background: var(--bg-success); color: var(--color-success); }
.rp-toast.is-error { background: var(--bg-error); color: var(--color-danger); }

/* ── 布局与卡片 ─────────────────────────────────────── */
.rp-layout { display: grid; grid-template-columns: 320px minmax(0, 1fr); gap: var(--space-md); align-items: start; }
.rp-sidebar, .rp-content { display: flex; flex-direction: column; gap: var(--space-md); min-width: 0; }
.rp-sidebar { position: sticky; top: 0; align-self: start; max-height: 100%; }
.rp-sidebar .rp-card { min-height: 0; }
.rp-card {
  padding: var(--space-md); background: var(--bg-surface); border: 1px solid var(--border-color);
  border-radius: var(--radius-lg); box-shadow: var(--shadow-sm);
}
.rp-card-head { display: flex; align-items: center; gap: var(--space-sm); margin-bottom: var(--space-sm); }
.rp-card-title { margin: 0; font-size: var(--font-size-md); font-weight: 600; color: var(--color-text); }
.rp-card-ops { margin-left: auto; display: flex; gap: var(--space-sm); }
.rp-count { margin-left: auto; font-size: var(--font-size-xs); color: var(--color-text-tertiary); }
.rp-muted { font-size: var(--font-size-xs); color: var(--color-text-tertiary); }
.rp-stats { margin: 0 0 var(--space-sm); font-size: var(--font-size-xs); color: var(--color-text-secondary); }

/* ── 列表 ───────────────────────────────────────────── */
.rp-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column;
  gap: 2px; max-height: 32vh; overflow-y: auto; }
.rp-item {
  display: flex; align-items: center; gap: var(--space-sm); padding: 9px 10px;
  border-radius: var(--radius-md); cursor: pointer; transition: background var(--transition-fast);
}
.rp-item:hover { background: var(--bg-hover); }
.rp-item.is-selected { background: var(--bg-active); box-shadow: inset 2px 0 0 var(--color-primary); }
.rp-dot { width: 8px; height: 8px; flex: none; border-radius: var(--radius-full); background: var(--color-text-tertiary); }
.rp-dot.is-ready { background: var(--color-success); }
.rp-dot.is-draft { background: var(--color-warning); }
.rp-dot.is-failed { background: var(--color-danger); }
.rp-dot.is-discarded { background: var(--border-strong); }
.rp-item-main { flex: 1; min-width: 0; font-size: var(--font-size-sm); color: var(--color-text);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.rp-item-meta { flex: none; font-size: var(--font-size-xs); color: var(--color-text-tertiary); font-variant-numeric: tabular-nums; }
.rp-icon-btn {
  width: 20px; height: 20px; flex: none; border: 0; border-radius: var(--radius-sm);
  background: transparent; color: var(--color-text-tertiary); font-size: 14px; line-height: 1; cursor: pointer;
  transition: background var(--transition-fast), color var(--transition-fast);
}
.rp-icon-btn:hover { background: var(--color-danger-light); color: var(--color-danger); }
.rp-flow { display: flex; flex-direction: column; gap: 6px; padding: 10px; border-radius: var(--radius-md); }
.rp-flow:hover { background: var(--bg-hover); }
.rp-flow-ops { display: grid; grid-template-columns: 1fr 1fr; gap: 6px; }
.rp-flow-ops .rp-btn { width: 100%; }

/* ── 步骤行 ─────────────────────────────────────────── */
.rp-rows { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 4px;
  max-height: min(460px, 46vh); overflow-y: auto; }
.rp-row {
  display: flex; align-items: center; gap: var(--space-sm); padding: 7px 10px;
  font-size: var(--font-size-sm); background: var(--bg-main); border: 1px solid transparent;
  border-left: 2px solid var(--border-strong); border-radius: var(--radius-sm);
}
.rp-row-id { width: 30px; flex: none; font-size: var(--font-size-xs); color: var(--color-text-tertiary); font-variant-numeric: tabular-nums; }
.rp-row-action { min-width: 78px; flex: none; font-weight: 600; color: var(--color-text); }
.rp-row-label { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--color-text-secondary); }
.rp-row-meta { flex: none; font-size: var(--font-size-xs); color: var(--color-text-tertiary); font-variant-numeric: tabular-nums; }
.rp-row-err { flex: none; font-size: var(--font-size-xs); color: var(--color-danger); }
.rp-row.is-warn { border-left-color: var(--color-warning); background: var(--color-warning-light); }
.rp-row.is-ok { border-left-color: var(--color-success); }
.rp-row.is-bad { border-left-color: var(--color-danger); background: var(--color-danger-light); }
.rp-row.rp-run { border-left-color: var(--color-primary); background: var(--color-primary-light); }
.rp-row.rp-fail { border-left-color: var(--color-danger); background: var(--color-danger-light); }
.rp-row.rp-skip { opacity: .5; }

/* ── 警告 ───────────────────────────────────────────── */
.rp-warns { list-style: none; margin: var(--space-sm) 0 0; padding: 0; display: flex; flex-direction: column; gap: 4px; }
.rp-warn { display: flex; align-items: baseline; gap: 8px; font-size: var(--font-size-xs); color: var(--color-text-secondary); }
.rp-warn-sev { flex: none; padding: 1px 6px; border-radius: var(--radius-xs); background: var(--bg-tag); color: var(--color-primary); }
.rp-warn-sid { flex: none; color: var(--color-text-tertiary); font-variant-numeric: tabular-nums; }

/* ── 子区块 / 空状态 ────────────────────────────────── */
.rp-subsection { margin-top: var(--space-md); padding-top: var(--space-md); border-top: 1px solid var(--border-color); }
.rp-subhead { display: flex; align-items: baseline; justify-content: space-between; margin-bottom: var(--space-sm); }
.rp-subtitle { margin: 0; font-size: var(--font-size-sm); font-weight: 600; }
.rp-empty { padding: 8px 4px; font-size: var(--font-size-xs); color: var(--color-text-tertiary); }
.rp-empty-state { display: flex; align-items: center; justify-content: center; min-height: 160px; }
.rp-empty-title { margin: 0; font-size: var(--font-size-sm); color: var(--color-text-tertiary); }

/* ── 准备清单 / 下一步 ─────────────────────────────── */
.rp-callout { border-left: 2px solid var(--color-primary); }
.rp-callout-title { margin: 0 0 var(--space-sm); font-size: var(--font-size-sm); font-weight: 600; }
.rp-callout-list { margin: 0; padding-left: 20px; font-size: var(--font-size-sm); line-height: 1.9; color: var(--color-text-secondary); }
.rp-callout-note { margin: var(--space-sm) 0 0; font-size: var(--font-size-xs); color: var(--color-warning); }
.rp-next { display: flex; align-items: center; gap: var(--space-md); border-left: 2px solid var(--color-success); }
.rp-next.is-warn { border-left-color: var(--color-warning); }
.rp-next-main { display: flex; align-items: baseline; gap: var(--space-sm); min-width: 0; }
.rp-next-title { font-size: var(--font-size-sm); }
.rp-next-ops { margin-left: auto; display: flex; align-items: center; gap: var(--space-sm); }
.rp-next-warn { font-size: var(--font-size-xs); color: var(--color-warning); }

/* ── 帮助 ───────────────────────────────────────────── */
.rp-help-list { margin: 0; padding-left: 20px; font-size: var(--font-size-sm); line-height: 1.9; color: var(--color-text-secondary); }
.rp-help-note { margin: var(--space-sm) 0 0; line-height: 1.6; }

/* ── 滚动条：默认隐藏，仅在滚动交互时显示 ───────────────── */
.rp-scroll { scrollbar-width: thin; scrollbar-color: transparent transparent; }
.rp-scroll.is-scrolling { scrollbar-color: var(--border-strong) transparent; }
.rp-scroll::-webkit-scrollbar { width: 8px; height: 8px; }
.rp-scroll::-webkit-scrollbar-track { background: transparent; }
.rp-scroll::-webkit-scrollbar-thumb { background: transparent; border-radius: var(--radius-full); }
.rp-scroll.is-scrolling::-webkit-scrollbar-thumb { background: var(--border-strong); }
.rp-scroll.is-scrolling::-webkit-scrollbar-thumb:hover { background: var(--color-text-tertiary); }

/* ── 响应式 ─────────────────────────────────────────── */
@media (max-width: 1080px) {
  .rp-layout { grid-template-columns: 1fr; }
  .rp-sidebar { order: 2; }
}
@media (max-width: 720px) {
  .rp-page { padding: var(--space-sm); }
  .rp-topbar { align-items: flex-start; }
  .rp-topbar-actions { width: 100%; }
  .rp-topbar-actions .rp-btn { flex: 1; }
  .rp-input { flex: 1; width: auto; min-width: 0; }
  .rp-stepper { flex-wrap: nowrap; }
  .rp-stepper li { min-width: 112px; padding: 9px 10px; gap: 8px; }
  .rp-banner { flex-wrap: wrap; }
  .rp-banner-msg { flex-basis: 100%; order: 5; white-space: normal; }
  .rp-banner-ops { margin-left: 0; }
  .rp-card-head { flex-wrap: wrap; }
  .rp-card-ops { width: 100%; margin-left: 0; }
  .rp-card-ops .rp-btn { flex: 1; }
  .rp-next { flex-direction: column; align-items: stretch; }
  .rp-next-ops { margin-left: 0; justify-content: flex-end; }
  .rp-row-action { min-width: 60px; }
  .rp-sidebar { position: static; max-height: none; }
  .rp-list { max-height: 40vh; }
  .rp-rows { max-height: 52vh; }
}
</style>