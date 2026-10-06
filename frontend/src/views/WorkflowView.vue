<script setup lang="ts">
/* eslint-disable @typescript-eslint/no-explicit-any */
// Dify 风格工作流编辑器：节点 data 按类型异构且运行时可变，动态图结构暂用 any 承载。
// TODO(1.1): 引入节点数据判别联合类型，移除本文件的 no-explicit-any 豁免。
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { apiClient } from '../api/client'
import { useProviderStore } from '../stores/providers'
import { useLanguage } from '../composables/useLanguage'
import VarRefField from '../components/VarRefField.vue'

const { t } = useLanguage()
const providerStore = useProviderStore()
const route = useRoute()
const router = useRouter()

// ── 节点定义（Dify 风格：输入输出端口 + 默认配置） ──────────
// 默认 data 对齐 Dify 节点能力文档 https://docs.dify.ai/zh/cloud/use-dify/nodes/*
const NODE_DEFS: Record<string, any> = {
  start: {
    type: 'start', icon: '▶', color: '#22c55e', inputs: [], outputs: ['out'],
    make: () => ({ variables: [{ name: 'input', label: '输入', type: 'string', required: false, default: '' }] }),
  },
  end: {
    type: 'end', icon: '⏹', color: '#ef4444', inputs: ['in'], outputs: [],
    make: () => ({ outputs: [{ name: 'result', type: 'string', value: '' }] }),
  },
  llm: {
    type: 'llm', icon: '🧠', color: '#8b5cf6', inputs: ['in'], outputs: ['out'],
    make: () => ({
      provider_id: 'deepseek', model: 'deepseek-flash', system_prompt: '', user_prompt: '',
      // Dify LLM 参数：温度 / 核采样 / 惩罚项 / 最大令牌（空 = 用模型默认）
      temperature: '', top_p: '', frequency_penalty: '', presence_penalty: '', max_tokens: '',
      reasoning_format: 'separated', // separated=推理分离输出 reasoning 变量；merged=并入正文
      max_retries: 0, retry_interval: 1,
    }),
  },
  code: {
    type: 'code', icon: 'ε', color: '#0ea5e9', inputs: ['in'], outputs: ['out'],
    make: () => ({
      code: '# 可用变量：topic = start.topic\noutput = {"result": "处理：" + str(topic)}',
      timeout: 15, max_retries: 0, retry_interval: 1,
    }),
  },
  condition: {
    type: 'condition', icon: '⑂', color: '#f59e0b', inputs: ['in'], outputs: ['true', 'false'],
    // Dify If-ELSE：多条件 + AND/OR 组合（旧图单条件结构加载时自动迁移）
    make: () => ({ logic: 'and', conditions: [{ left: '', op: '==', right: '' }] }),
  },
  http: {
    type: 'http', icon: '🌐', color: '#14b8a6', inputs: ['in'], outputs: ['out'],
    // Dify HTTP 请求：查询参数 / 认证 / 请求体类型 / 超时 / SSL / 重试
    make: () => ({
      method: 'GET', url: '',
      headers: [{ key: '', value: '' }],
      params: [{ key: '', value: '' }],
      auth: { type: 'no-auth', config: { kind: 'bearer', token: '', username: '', password: '', header_name: '', header_value: '' } },
      body_type: 'raw', body: '', form: [{ key: '', value: '' }],
      timeout: 15, ssl_verify: true, max_retries: 0, retry_interval: 1,
    }),
  },
  template: {
    type: 'template', icon: '❝', color: '#ec4899', inputs: ['in'], outputs: ['out'],
    make: () => ({ template: '' }),
  },
  assign: {
    type: 'assign', icon: '🏷', color: '#a855f7', inputs: ['in'], outputs: ['out'],
    // Dify 变量赋值器：操作模式 set/clear/append/extend/加减乘除
    make: () => ({ variables: [{ name: '', value: '', op: 'set' }] }),
  },
}

const PALETTE_GROUPS: { key: string; types: string[] }[] = [
  { key: 'basic', types: ['start', 'end'] },
  { key: 'ai', types: ['llm'] },
  { key: 'process', types: ['code', 'template'] },
  { key: 'logic', types: ['condition'] },
  { key: 'integration', types: ['http'] },
  { key: 'variable', types: ['assign'] },
]

// Dify If-ELSE 运算符全集（https://docs.dify.ai/zh/cloud/use-dify/nodes/ifelse）
const OPS = [
  '==', '!=', '>', '>=', '<', '<=',
  'contains', 'not_contains', 'starts_with', 'ends_with',
  'is_empty', 'is_not_empty',
]

// Start 输入类型（对齐 Dify 用户输入字段类型；引擎按此转换运行值）
const START_TYPES = ['string', 'paragraph', 'number', 'integer', 'boolean']
// End 输出变量类型（对齐 Dify 输出节点支持的类型）
const END_TYPES = ['string', 'number', 'integer', 'boolean', 'object', 'array']
// 变量赋值操作（对齐 Dify 变量赋值器）
const ASSIGN_OPS = ['set', 'clear', 'append', 'extend', 'add', 'sub', 'mul', 'div']
// HTTP 请求体类型与认证（对齐 Dify HTTP 请求节点）
const BODY_TYPES = ['raw', 'json', 'form']
const AUTH_TYPES = ['no-auth', 'api-key']
const AUTH_KINDS = ['bearer', 'basic', 'custom']

const NODE_W = 210
const HEADER_H = 44

// ── 画布状态 ────────────────────────────────────────────
const graph = reactive<{ nodes: any[]; edges: any[] }>({
  nodes: [{ id: 'start', type: 'start', position: { x: 80, y: 140 }, data: { variables: [{ name: 'input', type: 'string', default: '' }] } }],
  edges: [],
})
const viewport = reactive({ x: 40, y: 40, zoom: 1 })
const viewportRef = ref<HTMLElement | null>(null)
const selectedId = ref<string | null>(null)
const connecting = ref<{ source: string; handle: string; x: number; y: number } | null>(null)
const hoverPort = reactive<{ node: string; handle: string }>({ node: '', handle: '' })

const selected = computed(() => graph.nodes.find((n) => n.id === selectedId.value) || null)

// ── 运行 / 保存状态 ─────────────────────────────────────
const workflowId = ref<string | null>(null)
const workflowName = ref('未命名工作流')
const saving = ref(false)
const running = ref(false)
const runOpen = ref(false)
const openOpen = ref(false)
const loadError = ref('')
const savedAt = ref('')
const runResults = reactive<Record<string, any>>({})
const endOutputs = ref<Record<string, any>>({})
const runStatus = ref('idle') // idle | running | success | error
const workflowList = ref<any[]>([])

const hasResults = computed(() => Object.keys(runResults).length > 0)
const startVars = computed(() => {
  const s = graph.nodes.find((n) => n.type === 'start')
  return (s?.data?.variables || []).map((v: any) => ({
    name: v.name,
    label: v.label || v.name,
    type: v.type || 'string',
    required: !!v.required,
    default: v.default,
  }))
})
const runInputs = reactive<Record<string, any>>({})
const runInputError = ref('')

// ── 旧图数据迁移：补齐 Dify 对齐后新增的字段（打开/初始化时执行一次） ──
function normalizeNodeData(node: any) {
  const d = node?.data || {}
  switch (node?.type) {
    case 'condition': {
      if (!Array.isArray(d.conditions) || !d.conditions.length) {
        d.conditions = [{ left: d.left || '', op: d.op || '==', right: d.right || '' }]
      }
      d.conditions.forEach((c: any) => {
        if (c.op === 'empty') c.op = 'is_empty'
        else if (c.op === 'not_empty') c.op = 'is_not_empty'
      })
      d.logic = d.logic === 'or' ? 'or' : 'and'
      break
    }
    case 'http': {
      if (!Array.isArray(d.params)) d.params = [{ key: '', value: '' }]
      if (!Array.isArray(d.form)) d.form = [{ key: '', value: '' }]
      if (!d.auth) d.auth = {}
      if (!d.auth.type) d.auth.type = 'no-auth'
      if (!d.auth.config) d.auth.config = { kind: 'bearer', token: '', username: '', password: '', header_name: '', header_value: '' }
      if (!d.body_type) d.body_type = 'raw'
      if (d.timeout == null || d.timeout === '') d.timeout = 15
      if (d.ssl_verify == null) d.ssl_verify = true
      if (d.max_retries == null || d.max_retries === '') d.max_retries = 0
      if (d.retry_interval == null || d.retry_interval === '') d.retry_interval = 1
      break
    }
    case 'llm': {
      for (const k of ['temperature', 'top_p', 'frequency_penalty', 'presence_penalty', 'max_tokens']) {
        if (d[k] == null) d[k] = ''
      }
      if (!d.reasoning_format) d.reasoning_format = 'separated'
      if (d.max_retries == null || d.max_retries === '') d.max_retries = 0
      if (d.retry_interval == null || d.retry_interval === '') d.retry_interval = 1
      break
    }
    case 'code': {
      if (d.timeout == null || d.timeout === '') d.timeout = 15
      if (d.max_retries == null || d.max_retries === '') d.max_retries = 0
      if (d.retry_interval == null || d.retry_interval === '') d.retry_interval = 1
      break
    }
    case 'assign': {
      ;(d.variables || []).forEach((v: any) => { if (!v.op) v.op = 'set' })
      break
    }
    case 'start': {
      ;(d.variables || []).forEach((v: any) => {
        if (!v.type) v.type = 'string'
        if (v.label == null) v.label = ''
        if (v.required == null) v.required = false
      })
      break
    }
    case 'end': {
      ;(d.outputs || []).forEach((o: any) => { if (!o.type) o.type = 'string' })
      break
    }
  }
}
function normalizeGraph() {
  graph.nodes.forEach(normalizeNodeData)
}

// ── 坐标换算 ────────────────────────────────────────────
function screenToWorld(clientX: number, clientY: number) {
  const rect = viewportRef.value?.getBoundingClientRect()
  const left = rect?.left ?? 0
  const top = rect?.top ?? 0
  return {
    x: (clientX - left - viewport.x) / viewport.zoom,
    y: (clientY - top - viewport.y) / viewport.zoom,
  }
}

function getNode(id: string) {
  return graph.nodes.find((n) => n.id === id)
}

function portPos(node: any, kind: 'in' | 'out', handle: string) {
  const def = NODE_DEFS[node.type]
  if (kind === 'in') {
    return { x: node.position.x, y: node.position.y + HEADER_H / 2 }
  }
  const idx = Math.max(0, def.outputs.indexOf(handle))
  return { x: node.position.x + NODE_W, y: node.position.y + HEADER_H / 2 + idx * 28 }
}

function portLocalStyle(kind: 'in' | 'out', idx = 0) {
  if (kind === 'in') return { left: '-7px', top: `${HEADER_H / 2 - 7}px` }
  return { right: '-7px', top: `${HEADER_H / 2 - 7 + idx * 28}px` }
}

function nodeStyle(node: any) {
  return {
    left: `${node.position.x}px`,
    top: `${node.position.y}px`,
    width: `${NODE_W}px`,
  }
}
const worldStyle = computed(() => ({
  transform: `translate(${viewport.x}px, ${viewport.y}px) scale(${viewport.zoom})`,
  transformOrigin: '0 0',
}))
const edgeSvgStyle = { position: 'absolute', left: '0', top: '0', overflow: 'visible', width: '1px', height: '1px', pointerEvents: 'none' } as any

function edgePath(edge: any) {
  const s = getNode(edge.source)
  const tg = getNode(edge.target)
  if (!s || !tg) return ''
  const sp = portPos(s, 'out', edge.sourceHandle || 'out')
  const tp = portPos(tg, 'in', 'in')
  const dx = Math.max(40, Math.abs(tp.x - sp.x) * 0.5)
  return `M ${sp.x} ${sp.y} C ${sp.x + dx} ${sp.y}, ${tp.x - dx} ${tp.y}, ${tp.x} ${tp.y}`
}
function tempEdgePath() {
  if (!connecting.value) return ''
  const s = getNode(connecting.value.source)
  if (!s) return ''
  const sp = portPos(s, 'out', connecting.value.handle)
  const tp = { x: connecting.value.x, y: connecting.value.y }
  const dx = Math.max(40, Math.abs(tp.x - sp.x) * 0.5)
  return `M ${sp.x} ${sp.y} C ${sp.x + dx} ${sp.y}, ${tp.x - dx} ${tp.y}, ${tp.x} ${tp.y}`
}

// ── 交互：平移 / 拖拽节点 / 连线 ─────────────────────────
const drag: any = { mode: null }
function addWindowListeners() {
  window.addEventListener('pointermove', onWindowMove)
  window.addEventListener('pointerup', onWindowUp)
}
function removeWindowListeners() {
  window.removeEventListener('pointermove', onWindowMove)
  window.removeEventListener('pointerup', onWindowUp)
}
function onWindowMove(e: PointerEvent) {
  if (drag.mode === 'pan') {
    viewport.x = drag.vx + (e.clientX - drag.sx)
    viewport.y = drag.vy + (e.clientY - drag.sy)
  } else if (drag.mode === 'node') {
    const w = screenToWorld(e.clientX, e.clientY)
    drag.node.position.x = Math.round(w.x - drag.offx)
    drag.node.position.y = Math.round(w.y - drag.offy)
  } else if (drag.mode === 'connect') {
    const w = screenToWorld(e.clientX, e.clientY)
    connecting.value!.x = w.x
    connecting.value!.y = w.y
  }
}
function onWindowUp() {
  if (drag.mode === 'connect') {
    if (hoverPort.node && hoverPort.node !== connecting.value!.source) {
      createEdge(connecting.value!.source, hoverPort.node, connecting.value!.handle, hoverPort.handle)
    }
    connecting.value = null
    hoverPort.node = ''
    hoverPort.handle = ''
  }
  removeWindowListeners()
  drag.mode = null
}

function onCanvasPointerDown(e: PointerEvent) {
  const el = e.target as HTMLElement
  if (el.classList.contains('wf-canvas') || el.classList.contains('wf-world') || el.classList.contains('wf-edges')) {
    selectedId.value = null
    drag.mode = 'pan'
    drag.sx = e.clientX
    drag.sy = e.clientY
    drag.vx = viewport.x
    drag.vy = viewport.y
    addWindowListeners()
  }
}
function onNodePointerDown(node: any, e: PointerEvent) {
  e.stopPropagation()
  selectedId.value = node.id
  const w = screenToWorld(e.clientX, e.clientY)
  drag.mode = 'node'
  drag.node = node
  drag.offx = w.x - node.position.x
  drag.offy = w.y - node.position.y
  addWindowListeners()
}
function onPortDown(nodeId: string, handle: string, e: PointerEvent) {
  e.stopPropagation()
  const node = getNode(nodeId)
  if (!node) return
  const p = portPos(node, 'out', handle)
  connecting.value = { source: nodeId, handle, x: p.x, y: p.y }
  drag.mode = 'connect'
  drag.source = nodeId
  addWindowListeners()
}
function onPortEnter(node: string, handle: string) {
  hoverPort.node = node
  hoverPort.handle = handle
}
function onPortLeave() {
  hoverPort.node = ''
  hoverPort.handle = ''
}

function createEdge(source: string, target: string, sourceHandle: string, targetHandle: string) {
  if (source === target) return
  const dup = graph.edges.find(
    (e) => e.source === source && e.target === target && e.sourceHandle === sourceHandle,
  )
  if (dup) return
  graph.edges.push({
    id: 'e_' + Math.random().toString(36).slice(2, 9),
    source,
    target,
    sourceHandle,
    targetHandle,
  })
}

function addNode(type: string, pos?: { x: number; y: number }) {
  const rect = viewportRef.value?.getBoundingClientRect()
  let p = pos
  if (!p) {
    const cx = (rect?.left ?? 0) + (rect?.width ?? 800) / 2
    const cy = (rect?.top ?? 0) + (rect?.height ?? 600) / 2
    p = screenToWorld(cx, cy)
  }
  const id = type + '_' + Math.random().toString(36).slice(2, 8)
  graph.nodes.push({ id, type, position: { x: Math.round(p.x), y: Math.round(p.y) }, data: NODE_DEFS[type].make() })
  selectedId.value = id
}
function onPaletteDragStart(e: DragEvent, type: string) {
  e.dataTransfer?.setData('text/node-type', type)
}
function onDrop(e: DragEvent) {
  const type = e.dataTransfer?.getData('text/node-type')
  if (!type || !NODE_DEFS[type]) return
  const w = screenToWorld(e.clientX, e.clientY)
  addNode(type, { x: w.x - NODE_W / 2, y: w.y - HEADER_H / 2 })
}
function deleteNode(node: any) {
  if (!window.confirm(t.value('wfEditor.confirmDeleteNode'))) return
  graph.nodes = graph.nodes.filter((n) => n.id !== node.id)
  graph.edges = graph.edges.filter((e) => e.source !== node.id && e.target !== node.id)
  if (selectedId.value === node.id) selectedId.value = null
}

// ── 缩放 ────────────────────────────────────────────────
function onWheel(e: WheelEvent) {
  const rect = viewportRef.value?.getBoundingClientRect()
  const mx = (e.clientX - (rect?.left ?? 0))
  const my = (e.clientY - (rect?.top ?? 0))
  const wx = (mx - viewport.x) / viewport.zoom
  const wy = (my - viewport.y) / viewport.zoom
  const factor = e.deltaY < 0 ? 1.1 : 0.9
  const nz = Math.min(2, Math.max(0.3, viewport.zoom * factor))
  viewport.x = mx - wx * nz
  viewport.y = my - wy * nz
  viewport.zoom = nz
}
function zoomBy(factor: number) {
  const rect = viewportRef.value?.getBoundingClientRect()
  const cx = (rect?.width ?? 800) / 2
  const cy = (rect?.height ?? 600) / 2
  const wx = (cx - viewport.x) / viewport.zoom
  const wy = (cy - viewport.y) / viewport.zoom
  const nz = Math.min(2, Math.max(0.3, viewport.zoom * factor))
  viewport.x = cx - wx * nz
  viewport.y = cy - wy * nz
  viewport.zoom = nz
}
function resetView() {
  viewport.x = 40
  viewport.y = 40
  viewport.zoom = 1
}

// ── 标题 / 状态辅助 ─────────────────────────────────────
function nodeTitle(node: any) {
  switch (node.type) {
    case 'start': return '开始'
    case 'end': return '结束'
    case 'llm': return node.data.model || '大模型'
    case 'code': return '代码'
    case 'condition': return node.data.left ? `${node.data.left} ${node.data.op} ${node.data.right}` : '条件分支'
    case 'http': return `${node.data.method} ${node.data.url || 'HTTP'}`
    case 'template': return '模板'
    case 'assign': return '变量赋值'
    default: return NODE_DEFS[node.type]?.type || node.type
  }
}
function nodeTitleById(id: string) {
  const n = getNode(id)
  return n ? nodeTitle(n) : id
}
function nodeStatus(node: any) {
  return runResults[node.id]?.status || ''
}
function isEdgeHighlighted(edge: any) {
  return selectedId.value === edge.source || selectedId.value === edge.target
}
function stringify(obj: any) {
  if (obj === undefined || obj === null) return ''
  try {
    return JSON.stringify(obj, null, 2)
  } catch {
    return String(obj)
  }
}

// ── 保存 / 打开 / 删除 / 新建 ───────────────────────────
async function ensureSaved(): Promise<string | null> {
  if (workflowId.value) return workflowId.value
  return saveWorkflow(true)
}
async function saveWorkflow(silent = false): Promise<string | null> {
  if (!workflowName.value.trim()) {
    loadError.value = '工作流名称不能为空'
    return null
  }
  saving.value = true
  try {
    const body = { name: workflowName.value.trim(), description: '', graph: { nodes: graph.nodes, edges: graph.edges } }
    let res: any
    if (workflowId.value) {
      res = await apiClient.put(`/workflows/${workflowId.value}`, body)
    } else {
      res = await apiClient.post('/workflows', body)
      workflowId.value = res.id
      // 同步 URL，使刷新/分享保留同一工作流
      router.replace(`/workflows/${res.id}`)
    }
    if (!silent) savedAt.value = new Date().toLocaleTimeString()
    loadError.value = ''
    return workflowId.value
  } catch (e) {
    loadError.value = e instanceof Error ? e.message : String(e)
    return null
  } finally {
    saving.value = false
  }
}
function onNew() {
  if (graph.nodes.length && !window.confirm(t.value('wfEditor.confirmClear'))) return
  workflowId.value = null
  workflowName.value = '未命名工作流'
  router.replace('/workflows/new')
  graph.nodes = [{ id: 'start', type: 'start', position: { x: 80, y: 140 }, data: { variables: [{ name: 'input', label: '输入', type: 'string', required: false, default: '' }] } }]
  graph.edges = []
  selectedId.value = null
  Object.keys(runResults).forEach((k) => delete runResults[k])
  endOutputs.value = {}
  runStatus.value = 'idle'
  savedAt.value = ''
}
function onClear() {
  if (!window.confirm(t.value('wfEditor.confirmClear'))) return
  graph.nodes = []
  graph.edges = []
  selectedId.value = null
}
async function onOpen() {
  try {
    workflowList.value = await apiClient.get('/workflows')
    openOpen.value = true
  } catch (e) {
    loadError.value = e instanceof Error ? e.message : String(e)
  }
}
async function loadWorkflow(id: string) {
  try {
    const wf = (await apiClient.get(`/workflows/${id}`)) as any
    graph.nodes = wf.graph.nodes || []
    graph.edges = wf.graph.edges || []
    normalizeGraph() // 旧图补齐 Dify 对齐新增字段
    workflowId.value = wf.id
    workflowName.value = wf.name
    selectedId.value = null
    openOpen.value = false
    router.replace(`/workflows/${id}`)
    Object.keys(runResults).forEach((k) => delete runResults[k])
    endOutputs.value = {}
    runStatus.value = 'idle'
  } catch (e) {
    loadError.value = e instanceof Error ? e.message : String(e)
  }
}
async function onDelete() {
  if (!workflowId.value) return
  if (!window.confirm(t.value('wfEditor.confirmDelete'))) return
  try {
    await apiClient.delete(`/workflows/${workflowId.value}`)
    router.push('/workflows')
  } catch (e) {
    loadError.value = e instanceof Error ? e.message : String(e)
  }
}

// ── 运行 ────────────────────────────────────────────────
function onRun() {
  const hasStart = graph.nodes.some((n) => n.type === 'start')
  if (!hasStart) {
    loadError.value = '工作流至少需要一个「开始」节点'
    return
  }
  startVars.value.forEach((v: any) => {
    if (!(v.name in runInputs)) {
      runInputs[v.name] = v.type === 'boolean'
        ? ['1', 'true', 'yes', 'on'].includes(String(v.default ?? '').toLowerCase())
        : (v.default || '')
    }
  })
  runInputError.value = ''
  runOpen.value = true
}
async function confirmRun() {
  // 必填校验（对齐 Dify Start 用户输入的 required）
  const missing = startVars.value.filter((v: any) => v.required && !String(runInputs[v.name] ?? '').trim())
  if (missing.length) {
    runInputError.value = t.value('wfEditor.requiredMissing').replace('{0}', missing.map((v: any) => v.label).join('、'))
    return
  }
  runInputError.value = ''
  runOpen.value = false
  const id = await ensureSaved()
  if (!id) return
  running.value = true
  runStatus.value = 'running'
  Object.keys(runResults).forEach((k) => delete runResults[k])
  endOutputs.value = {}
  try {
    const res: any = await apiClient.post(`/workflows/${id}/run`, { inputs: { ...runInputs } })
    if (res.engine_result) {
      const r = res.engine_result.results || {}
      Object.keys(r).forEach((k) => (runResults[k] = r[k]))
      endOutputs.value = res.engine_result.end_outputs || {}
      runStatus.value = res.status || 'success'
    } else {
      runStatus.value = 'error'
      loadError.value = res.error || '运行失败'
    }
  } catch (e) {
    runStatus.value = 'error'
    loadError.value = e instanceof Error ? e.message : String(e)
  } finally {
    running.value = false
  }
}

onMounted(async () => {
  providerStore.loadProviders()
  normalizeGraph()
  const id = route.params.id
  if (id && id !== 'new') {
    await loadWorkflow(String(id))
  }
})
onUnmounted(() => {
  removeWindowListeners()
})

// ── 变量选择器（已抽离到 components/VarRefField.vue；此处仅提供上游变量数据） ──
function nodeOutputFields(node: any): string[] {
  const d = node?.data || {}
  switch (node?.type) {
    case 'start':
    case 'assign':
      return (d.variables || []).map((v: any) => String(v.name || '').trim()).filter(Boolean)
    case 'end':
      return (d.outputs || []).map((o: any) => String(o.name || '').trim()).filter(Boolean)
    case 'llm': return ['text', 'reasoning']
    case 'condition': return ['branch', 'conditions']
    case 'http': return ['status_code', 'body', 'headers', 'size']
    case 'template': return ['output']
    case 'code': return inferCodeOutputs(d.code || '')
    default: return []
  }
}
function inferCodeOutputs(code: string): string[] {
  const keys = new Set<string>()
  let m: RegExpExecArray | null
  const re1 = /output\[['"]([^'"]+)['"]\]\s*=/g
  while ((m = re1.exec(code))) keys.add(m[1])
  const re2 = /output\.(\w+)\s*=/g
  while ((m = re2.exec(code))) keys.add(m[1])
  const re3 = /output\s*=\s*\{([^}]*)\}/g
  while ((m = re3.exec(code))) {
    const body = m[1]
    const re4 = /(['"]?)(\w+)\1\s*:/g
    let m2: RegExpExecArray | null
    while ((m2 = re4.exec(body))) keys.add(m2[2])
  }
  return keys.size ? [...keys] : ['result']
}
// 字段中文标注（变量选择器里展示，插入仍用原始字段名）
const FIELD_LABELS: Record<string, string> = {
  text: t.value('wfEditor.fText'),
  reasoning: t.value('wfEditor.fReasoning'),
  branch: t.value('wfEditor.fBranch'),
  conditions: t.value('wfEditor.fConditions'),
  status_code: t.value('wfEditor.fStatusCode'),
  body: t.value('wfEditor.fBody'),
  headers: t.value('wfEditor.fHeaders'),
  size: t.value('wfEditor.fSize'),
  output: t.value('wfEditor.fOutput'),
  result: t.value('wfEditor.fResult'),
  error: t.value('wfEditor.fError'),
}

function upstreamVars(nodeId: string) {
  const rev: Record<string, string[]> = {}
  graph.edges.forEach((e: any) => { (rev[e.target] ||= []).push(e.source) })
  const seen = new Set<string>()
  const stack = [...(rev[nodeId] || [])]
  while (stack.length) {
    const n = stack.pop() as string
    if (seen.has(n)) continue
    seen.add(n)
    ;(rev[n] || []).forEach((s) => { if (!seen.has(s)) stack.push(s) })
  }
  const list: { nodeId: string; title: string; fields: { name: string; label: string }[] }[] = []
  seen.forEach((nid) => {
    const node = getNode(nid)
    if (!node) return
    const fields = nodeOutputFields(node).map((f: string) => ({
      name: f,
      label: FIELD_LABELS[f] || f,
    }))
    if (fields.length) list.push({ nodeId: nid, title: nodeTitle(node), fields })
  })
  return list
}
const availableVars = computed(() => (selectedId.value ? upstreamVars(selectedId.value) : []))
</script>

<template>
  <div class="wf-studio">
    <header class="wf-toolbar">
      <router-link to="/chat" class="btn-ghost btn-sm">← {{ t('insights.back') }}</router-link>
      <router-link to="/workflows" class="btn-ghost btn-sm wf-back">← {{ t('workflows.back') }}</router-link>
      <div class="wf-brand">⚡ {{ t('wfEditor.title') }}</div>
      <input v-model="workflowName" class="wf-name-input" :placeholder="t('wfEditor.title')" />
      <div class="wf-tool-actions">
        <button class="btn-ghost btn-sm" @click="onNew">{{ t('wfEditor.new') }}</button>
        <button class="btn-ghost btn-sm" @click="onOpen">{{ t('wfEditor.open') }}</button>
        <button v-if="graph.nodes.length" class="btn-ghost btn-sm" @click="onClear">{{ t('wfEditor.clear') }}</button>
        <button v-if="workflowId" class="btn-ghost btn-sm" @click="onDelete">{{ t('wfEditor.delete') }}</button>
        <button class="btn-primary btn-sm" :disabled="saving" @click="saveWorkflow()">
          {{ saving ? t('wfEditor.saving') : t('wfEditor.save') }}
        </button>
        <button class="btn-primary btn-sm" :disabled="running" @click="onRun">
          {{ running ? t('wfEditor.running') : t('wfEditor.run') }}
        </button>
        <span v-if="savedAt" class="wf-saved">✓ {{ t('wfEditor.saved') }} {{ savedAt }}</span>
      </div>
    </header>

    <div class="wf-body">
      <!-- 控件列表 -->
      <aside class="wf-palette">
        <div class="wf-panel-title">{{ t('wfEditor.palette') }}</div>
        <div v-for="g in PALETTE_GROUPS" :key="g.key" class="wf-pal-group">
          <div class="wf-pal-group-title">{{ t('wfEditor.cats.' + g.key) }}</div>
          <div
            v-for="ty in g.types"
            :key="ty"
            class="wf-pal-item"
            draggable="true"
            @dragstart="onPaletteDragStart($event, ty)"
            @click="addNode(ty)"
          >
            <span class="wf-pal-icon" :style="{ color: NODE_DEFS[ty].color }">{{ NODE_DEFS[ty].icon }}</span>
            <span>{{ t('wfEditor.nodeTypes.' + ty) }}</span>
          </div>
        </div>
        <div class="wf-pal-hint">{{ t('wfEditor.addNodeHint') }}</div>
      </aside>

      <!-- 无限画布 -->
      <main
        ref="viewportRef"
        class="wf-canvas"
        @pointerdown="onCanvasPointerDown"
        @dragover.prevent
        @drop="onDrop"
        @wheel.prevent="onWheel"
      >
        <div class="wf-world" :style="worldStyle">
          <svg :style="edgeSvgStyle">
            <path
              v-for="edge in graph.edges"
              :key="edge.id"
              :d="edgePath(edge)"
              class="wf-edge"
              :class="{ 'wf-edge-active': isEdgeHighlighted(edge) }"
            />
            <path v-if="connecting" :d="tempEdgePath()" class="wf-edge wf-edge-temp" />
          </svg>

          <div
            v-for="node in graph.nodes"
            :key="node.id"
            class="wf-node"
            :class="['wf-node-' + node.type, { 'wf-node-selected': node.id === selectedId, 'wf-node-running': runStatus === 'running' && runResults[node.id] }]"
            :style="nodeStyle(node)"
            @pointerdown="onNodePointerDown(node, $event)"
          >
            <div class="wf-node-head" :style="{ borderColor: NODE_DEFS[node.type]?.color }">
              <span class="wf-node-icon" :style="{ color: NODE_DEFS[node.type]?.color }">{{ NODE_DEFS[node.type]?.icon }}</span>
              <span class="wf-node-title">{{ nodeTitle(node) }}</span>
              <span v-if="runResults[node.id]" class="wf-node-status" :class="'st-' + nodeStatus(node)"></span>
            </div>
            <div class="wf-node-sub">{{ t('wfEditor.nodeTypes.' + node.type) }}</div>

            <div
              v-if="NODE_DEFS[node.type]?.inputs.length"
              class="wf-port wf-port-in"
              :style="portLocalStyle('in')"
              @pointerenter="onPortEnter(node.id, 'in')"
              @pointerleave="onPortLeave"
            ></div>
            <div
              v-for="(h, i) in NODE_DEFS[node.type]?.outputs"
              :key="h"
              class="wf-port wf-port-out"
              :style="portLocalStyle('out', Number(i))"
              @pointerdown.stop="onPortDown(node.id, h, $event)"
              @pointerenter="onPortEnter(node.id, h)"
              @pointerleave="onPortLeave"
            ></div>
            <button class="wf-node-del" title="delete" @pointerdown.stop @click.stop="deleteNode(node)">×</button>
          </div>
        </div>

        <div class="wf-zoom">
          <button @click="zoomBy(1.2)">＋</button>
          <button @click="zoomBy(0.8)">－</button>
          <button @click="resetView">{{ t('wfEditor.fit') }}</button>
        </div>
        <div v-if="loadError" class="wf-error">{{ loadError }}</div>
      </main>

      <!-- 节点配置 -->
      <aside class="wf-config">
        <div class="wf-panel-title">{{ t('wfEditor.config') }}</div>
        <div v-if="selected" class="wf-config-body">
          <div class="wf-cfg-row">
            <label>{{ t('wfEditor.nodeTypes.' + selected.type) }}</label>
            <code class="wf-cfg-id">{{ selected.id }}</code>
          </div>

          <template v-if="selected.type === 'start'">
            <div class="wf-cfg-hint">{{ t('wfEditor.startHint') }}</div>
            <div v-for="(v, i) in selected.data.variables" :key="i" class="wf-cfg-card">
              <div class="wf-cfg-item">
                <input v-model="v.name" :placeholder="t('wfEditor.varName')" class="form-input" />
                <button class="btn-ghost btn-xs" @click="selected.data.variables.splice(i, 1)">×</button>
              </div>
              <div class="wf-cfg-item">
                <input v-model="v.label" :placeholder="t('wfEditor.varLabel')" class="form-input" />
              </div>
              <div class="wf-cfg-item">
                <select v-model="v.type" class="form-input">
                  <option v-for="ty in START_TYPES" :key="ty" :value="ty">{{ t('wfEditor.startTypes.' + ty) }}</option>
                </select>
                <label class="wf-cfg-check">
                  <input v-model="v.required" type="checkbox" />
                  <span>{{ t('wfEditor.required') }}</span>
                </label>
              </div>
              <div v-if="v.type !== 'boolean'" class="wf-cfg-item">
                <input v-model="v.default" :placeholder="t('wfEditor.varDefault')" class="form-input" />
              </div>
            </div>
            <button class="btn-ghost btn-sm" @click="selected.data.variables.push({ name: '', label: '', type: 'string', required: false, default: '' })">{{ t('wfEditor.addVar') }}</button>
          </template>

          <template v-else-if="selected.type === 'end'">
            <div class="wf-cfg-hint">{{ t('wfEditor.endHint') }}</div>
            <div v-for="(o, i) in selected.data.outputs" :key="i" class="wf-cfg-card">
              <div class="wf-cfg-item">
                <input v-model="o.name" :placeholder="t('wfEditor.outName')" class="form-input" />
                <button class="btn-ghost btn-xs" @click="selected.data.outputs.splice(i, 1)">×</button>
              </div>
              <div class="wf-cfg-item">
                <select v-model="o.type" class="form-input">
                  <option v-for="ty in END_TYPES" :key="ty" :value="ty">{{ t('wfEditor.endTypes.' + ty) }}</option>
                </select>
              </div>
              <div class="wf-cfg-item">
                <VarRefField v-model="o.value" :vars="availableVars" :placeholder="t('wfEditor.outValue')" />
              </div>
            </div>
            <button class="btn-ghost btn-sm" @click="selected.data.outputs.push({ name: '', type: 'string', value: '' })">{{ t('wfEditor.addOutput') }}</button>
          </template>

          <template v-else-if="selected.type === 'llm'">
            <div class="wf-cfg-row"><label>{{ t('wfEditor.provider') }}</label>
              <select v-model="selected.data.provider_id" class="form-input">
                <option v-for="p in providerStore.providers" :key="p.id" :value="p.id">{{ p.name }}</option>
              </select>
            </div>
            <div class="wf-cfg-row"><label>{{ t('wfEditor.model') }}</label>
              <input v-model="selected.data.model" class="form-input" />
            </div>
            <div class="wf-cfg-row"><label>{{ t('wfEditor.systemPrompt') }}</label>
              <VarRefField v-model="selected.data.system_prompt" :vars="availableVars" :multiline="true" :rows="3" :placeholder="t('wfEditor.systemPrompt')" />
            </div>
            <div class="wf-cfg-row"><label>{{ t('wfEditor.userPrompt') }}</label>
              <VarRefField v-model="selected.data.user_prompt" :vars="availableVars" :multiline="true" :rows="4" :placeholder="t('wfEditor.userPrompt')" />
            </div>
            <!-- Dify LLM 模型参数（留空 = 使用模型默认值） -->
            <div class="wf-cfg-subtitle">{{ t('wfEditor.modelParams') }}</div>
            <div class="wf-cfg-grid">
              <div class="wf-cfg-row"><label>{{ t('wfEditor.temperature') }}</label>
                <input v-model="selected.data.temperature" type="number" step="0.1" min="0" max="2" placeholder="default" class="form-input" />
              </div>
              <div class="wf-cfg-row"><label>{{ t('wfEditor.topP') }}</label>
                <input v-model="selected.data.top_p" type="number" step="0.05" min="0" max="1" placeholder="default" class="form-input" />
              </div>
              <div class="wf-cfg-row"><label>{{ t('wfEditor.freqPenalty') }}</label>
                <input v-model="selected.data.frequency_penalty" type="number" step="0.1" min="-2" max="2" placeholder="default" class="form-input" />
              </div>
              <div class="wf-cfg-row"><label>{{ t('wfEditor.presPenalty') }}</label>
                <input v-model="selected.data.presence_penalty" type="number" step="0.1" min="-2" max="2" placeholder="default" class="form-input" />
              </div>
              <div class="wf-cfg-row"><label>{{ t('wfEditor.maxTokens') }}</label>
                <input v-model="selected.data.max_tokens" type="number" step="1" min="1" placeholder="default" class="form-input" />
              </div>
              <div class="wf-cfg-row"><label>{{ t('wfEditor.reasoningFormat') }}</label>
                <select v-model="selected.data.reasoning_format" class="form-input">
                  <option value="separated">{{ t('wfEditor.reasoningSeparated') }}</option>
                  <option value="merged">{{ t('wfEditor.reasoningMerged') }}</option>
                </select>
              </div>
            </div>
            <div class="wf-cfg-subtitle">{{ t('wfEditor.errorHandling') }}</div>
            <div class="wf-cfg-grid">
              <div class="wf-cfg-row"><label>{{ t('wfEditor.maxRetries') }}</label>
                <input v-model.number="selected.data.max_retries" type="number" step="1" min="0" class="form-input" />
              </div>
              <div class="wf-cfg-row"><label>{{ t('wfEditor.retryInterval') }}</label>
                <input v-model.number="selected.data.retry_interval" type="number" step="0.5" min="0" class="form-input" />
              </div>
            </div>
          </template>

          <template v-else-if="selected.type === 'code'">
            <div class="wf-cfg-hint">{{ t('wfEditor.codeHint') }}</div>
            <VarRefField v-model="selected.data.code" :vars="availableVars" :multiline="true" :rows="12" :mono="true" :placeholder="t('wfEditor.code')" />
            <div class="wf-cfg-subtitle">{{ t('wfEditor.errorHandling') }}</div>
            <div class="wf-cfg-grid">
              <div class="wf-cfg-row"><label>{{ t('wfEditor.timeout') }}</label>
                <input v-model.number="selected.data.timeout" type="number" step="1" min="1" class="form-input" />
              </div>
              <div class="wf-cfg-row"><label>{{ t('wfEditor.maxRetries') }}</label>
                <input v-model.number="selected.data.max_retries" type="number" step="1" min="0" class="form-input" />
              </div>
            </div>
          </template>

          <template v-else-if="selected.type === 'condition'">
            <div class="wf-cfg-hint">{{ t('wfEditor.conditionHint') }}</div>
            <div class="wf-cfg-row"><label>{{ t('wfEditor.logic') }}</label>
              <select v-model="selected.data.logic" class="form-input">
                <option value="and">{{ t('wfEditor.logicAnd') }}</option>
                <option value="or">{{ t('wfEditor.logicOr') }}</option>
              </select>
            </div>
            <div class="wf-cfg-list">
              <div v-for="(c, i) in selected.data.conditions" :key="i" class="wf-cfg-card">
                <div class="wf-cfg-cond-head">
                  <span>{{ i === 0 ? t('wfEditor.condIf') : t('wfEditor.condAnd') }} #{{ Number(i) + 1 }}</span>
                  <button v-if="selected.data.conditions.length > 1" class="btn-ghost btn-xs" @click="selected.data.conditions.splice(i, 1)">×</button>
                </div>
                <VarRefField v-model="c.left" :vars="availableVars" placeholder="{{ ref }}" />
                <select v-model="c.op" class="form-input">
                  <option v-for="op in OPS" :key="op" :value="op">{{ t('wfEditor.ops.' + op) }}</option>
                </select>
                <VarRefField v-model="c.right" :vars="availableVars" :placeholder="t('wfEditor.right')" />
              </div>
            </div>
            <button class="btn-ghost btn-sm" @click="selected.data.conditions.push({ left: '', op: '==', right: '' })">{{ t('wfEditor.addCondition') }}</button>
            <div v-if="runResults[selected.id]?.branch" class="wf-cfg-hint">{{ t('wfEditor.branch') }}: {{ runResults[selected.id].branch === 'true' ? t('wfEditor.true') : t('wfEditor.false') }}</div>
          </template>

          <template v-else-if="selected.type === 'http'">
            <div class="wf-cfg-row"><label>{{ t('wfEditor.method') }}</label>
              <select v-model="selected.data.method" class="form-input">
                <option v-for="m in ['GET', 'POST', 'PUT', 'DELETE', 'PATCH']" :key="m" :value="m">{{ m }}</option>
              </select>
            </div>
            <div class="wf-cfg-row"><label>{{ t('wfEditor.url') }}</label>
              <VarRefField v-model="selected.data.url" :vars="availableVars" :placeholder="t('wfEditor.url')" />
            </div>
            <!-- 查询参数（Dify HTTP 请求 params） -->
            <div class="wf-cfg-subtitle">{{ t('wfEditor.queryParams') }}</div>
            <div class="wf-cfg-list">
              <div v-for="(p, i) in selected.data.params" :key="i" class="wf-cfg-item">
                <input v-model="p.key" :placeholder="t('wfEditor.headerKey')" class="form-input" />
                <VarRefField v-model="p.value" :vars="availableVars" :placeholder="t('wfEditor.headerVal')" />
                <button class="btn-ghost btn-xs" @click="selected.data.params.splice(i, 1)">×</button>
              </div>
            </div>
            <button class="btn-ghost btn-sm" @click="selected.data.params.push({ key: '', value: '' })">+ {{ t('wfEditor.addParam') }}</button>
            <!-- 身份认证（Dify HTTP 请求 auth：no-auth / api-key bearer|basic|custom） -->
            <div class="wf-cfg-subtitle">{{ t('wfEditor.auth') }}</div>
            <div class="wf-cfg-row">
              <select v-model="selected.data.auth.type" class="form-input">
                <option v-for="a in AUTH_TYPES" :key="a" :value="a">{{ t('wfEditor.authTypes.' + a) }}</option>
              </select>
            </div>
            <template v-if="selected.data.auth.type === 'api-key'">
              <div class="wf-cfg-row">
                <select v-model="selected.data.auth.config.kind" class="form-input">
                  <option v-for="k in AUTH_KINDS" :key="k" :value="k">{{ t('wfEditor.authKinds.' + k) }}</option>
                </select>
              </div>
              <div v-if="selected.data.auth.config.kind === 'bearer'" class="wf-cfg-row">
                <VarRefField v-model="selected.data.auth.config.token" :vars="availableVars" :placeholder="t('wfEditor.authToken')" />
              </div>
              <template v-else-if="selected.data.auth.config.kind === 'basic'">
                <div class="wf-cfg-row">
                  <input v-model="selected.data.auth.config.username" :placeholder="t('wfEditor.authUser')" class="form-input" />
                </div>
                <div class="wf-cfg-row">
                  <input v-model="selected.data.auth.config.password" type="password" :placeholder="t('wfEditor.authPass')" class="form-input" />
                </div>
              </template>
              <template v-else>
                <div class="wf-cfg-row">
                  <input v-model="selected.data.auth.config.header_name" :placeholder="t('wfEditor.authHeaderName')" class="form-input" />
                </div>
                <div class="wf-cfg-row">
                  <VarRefField v-model="selected.data.auth.config.header_value" :vars="availableVars" :placeholder="t('wfEditor.authHeaderValue')" />
                </div>
              </template>
            </template>
            <div class="wf-cfg-row"><label>{{ t('wfEditor.headers') }}</label></div>
            <div v-for="(h, i) in selected.data.headers" :key="i" class="wf-cfg-item">
              <input v-model="h.key" :placeholder="t('wfEditor.headerKey')" class="form-input" />
              <VarRefField v-model="h.value" :vars="availableVars" :placeholder="t('wfEditor.headerVal')" />
              <button class="btn-ghost btn-xs" @click="selected.data.headers.splice(i, 1)">×</button>
            </div>
            <button class="btn-ghost btn-sm" @click="selected.data.headers.push({ key: '', value: '' })">+ Header</button>
            <!-- 请求体（Dify HTTP 请求 body：raw / json / form） -->
            <div class="wf-cfg-subtitle">{{ t('wfEditor.bodyType') }}</div>
            <div class="wf-cfg-row">
              <select v-model="selected.data.body_type" class="form-input">
                <option v-for="b in BODY_TYPES" :key="b" :value="b">{{ t('wfEditor.bodyTypes.' + b) }}</option>
              </select>
            </div>
            <div v-if="selected.data.body_type === 'form'" class="wf-cfg-list">
              <div v-for="(f, i) in selected.data.form" :key="i" class="wf-cfg-item">
                <input v-model="f.key" :placeholder="t('wfEditor.headerKey')" class="form-input" />
                <VarRefField v-model="f.value" :vars="availableVars" :placeholder="t('wfEditor.headerVal')" />
                <button class="btn-ghost btn-xs" @click="selected.data.form.splice(i, 1)">×</button>
              </div>
              <button class="btn-ghost btn-sm" @click="selected.data.form.push({ key: '', value: '' })">+ {{ t('wfEditor.addParam') }}</button>
            </div>
            <div v-else class="wf-cfg-row">
              <VarRefField v-model="selected.data.body" :vars="availableVars" :multiline="true" :rows="3" :placeholder="t('wfEditor.body')" />
            </div>
            <!-- 超时 / SSL / 重试（Dify HTTP 请求高级设置） -->
            <div class="wf-cfg-subtitle">{{ t('wfEditor.advanced') }}</div>
            <div class="wf-cfg-grid">
              <div class="wf-cfg-row"><label>{{ t('wfEditor.timeout') }}</label>
                <input v-model.number="selected.data.timeout" type="number" step="1" min="1" class="form-input" />
              </div>
              <div class="wf-cfg-row"><label>{{ t('wfEditor.maxRetries') }}</label>
                <input v-model.number="selected.data.max_retries" type="number" step="1" min="0" class="form-input" />
              </div>
            </div>
            <label class="wf-cfg-check">
              <input v-model="selected.data.ssl_verify" type="checkbox" />
              <span>{{ t('wfEditor.sslVerify') }}</span>
            </label>
          </template>

          <template v-else-if="selected.type === 'template'">
            <div class="wf-cfg-hint">{{ t('wfEditor.templateHint') }}</div>
            <VarRefField v-model="selected.data.template" :vars="availableVars" :multiline="true" :rows="6" :placeholder="t('wfEditor.template')" />
          </template>

          <template v-else-if="selected.type === 'assign'">
            <div class="wf-cfg-hint">{{ t('wfEditor.assignHint') }}</div>
            <div v-for="(v, i) in selected.data.variables" :key="i" class="wf-cfg-card">
              <div class="wf-cfg-item">
                <input v-model="v.name" :placeholder="t('wfEditor.varName')" class="form-input" />
                <button class="btn-ghost btn-xs" @click="selected.data.variables.splice(i, 1)">×</button>
              </div>
              <div class="wf-cfg-item">
                <select v-model="v.op" class="form-input">
                  <option v-for="op in ASSIGN_OPS" :key="op" :value="op">{{ t('wfEditor.assignOps.' + op) }}</option>
                </select>
              </div>
              <div class="wf-cfg-item">
                <VarRefField v-model="v.value" :vars="availableVars" :placeholder="t('wfEditor.value')" />
              </div>
            </div>
            <button class="btn-ghost btn-sm" @click="selected.data.variables.push({ name: '', value: '', op: 'set' })">{{ t('wfEditor.addVar') }}</button>
          </template>

          <button class="btn-ghost btn-sm wf-del-btn" @click="deleteNode(selected)">{{ t('wfEditor.deleteNode') }}</button>
        </div>
        <div v-else class="wf-no-sel">{{ t('wfEditor.noSelection') }}</div>
      </aside>
    </div>

    <!-- 运行输入弹层 -->
    <div v-if="runOpen" class="wf-overlay" @click.self="runOpen = false">
      <div class="wf-dialog">
        <h3 class="tpl-dialog-title">{{ t('wfEditor.inputs') }}</h3>
        <p class="wf-dialog-hint">{{ t('wfEditor.inputsPh') }}</p>
        <div v-if="startVars.length" class="wf-runs-vars">
          <div v-for="v in startVars" :key="v.name" class="form-row">
            <label class="form-label">
              {{ v.label }}
              <span v-if="v.required" class="wf-req-star">*</span>
              <code class="wf-cfg-id">{{ v.type }}</code>
            </label>
            <input
              v-if="v.type === 'boolean'"
              v-model="runInputs[v.name]"
              type="checkbox"
              class="wf-bool-input"
            />
            <textarea
              v-else-if="v.type === 'paragraph'"
              v-model="runInputs[v.name]"
              class="form-input"
              rows="3"
            ></textarea>
            <input
              v-else
              v-model="runInputs[v.name]"
              :type="v.type === 'number' || v.type === 'integer' ? 'number' : 'text'"
              class="form-input"
            />
          </div>
          <p v-if="runInputError" class="wf-run-error">{{ runInputError }}</p>
        </div>
        <p v-else class="empty-hint">{{ t('wfEditor.startVars') }}</p>
        <div class="tpl-dialog-actions">
          <button class="btn-ghost" @click="runOpen = false">{{ t('providers.cancel') }}</button>
          <button class="btn-primary" :disabled="running" @click="confirmRun">{{ t('wfEditor.run') }}</button>
        </div>
      </div>
    </div>

    <!-- 打开弹层 -->
    <div v-if="openOpen" class="wf-overlay" @click.self="openOpen = false">
      <div class="wf-dialog">
        <h3 class="tpl-dialog-title">{{ t('wfEditor.open') }}</h3>
        <div v-if="workflowList.length" class="wf-list">
          <div v-for="wf in workflowList" :key="wf.id" class="wf-list-item" @click="loadWorkflow(wf.id)">
            <span>{{ wf.name }}</span>
            <span class="wf-list-meta">{{ (wf.graph?.nodes || []).length }} nodes</span>
          </div>
        </div>
        <p v-else class="empty-hint">—</p>
        <div class="tpl-dialog-actions">
          <button class="btn-ghost" @click="openOpen = false">{{ t('providers.cancel') }}</button>
        </div>
      </div>
    </div>

    <!-- 运行结果 -->
    <section v-if="hasResults" class="wf-results">
      <div class="wf-panel-title">{{ t('wfEditor.results') }} · <span :class="'st-' + runStatus">{{ runStatus }}</span></div>
      <div v-if="endOutputs && Object.keys(endOutputs).length" class="wf-end-out">
        <strong>{{ t('wfEditor.endOutputs') }}</strong>
        <pre>{{ stringify(endOutputs) }}</pre>
      </div>
      <div v-for="(r, nid) in runResults" :key="nid" class="wf-res-node">
        <div class="wf-res-head">
          <span class="wf-res-status" :class="'st-' + r.status">{{ r.status }}</span>
          <span class="wf-res-name">{{ nodeTitleById(nid) }}</span>
          <span class="wf-res-ms">{{ r.elapsed_ms }}ms</span>
        </div>
        <div v-if="r.error" class="wf-res-err">{{ r.error }}</div>
        <pre v-else class="wf-res-out">{{ stringify(r.outputs) }}</pre>
      </div>
    </section>
  </div>
</template>

<style scoped>
.wf-studio { display: flex; flex-direction: column; height: 100vh; background: var(--bg-main); color: var(--color-text); }
.wf-toolbar { display: flex; align-items: center; gap: var(--space-md); padding: var(--space-sm) var(--space-lg); border-bottom: 1px solid var(--border-color); background: var(--bg-surface); }
.wf-brand { font-weight: 800; font-size: var(--font-size-md); color: var(--color-primary); white-space: nowrap; }
.wf-name-input { flex: 1; max-width: 320px; background: var(--bg-input); border: 1px solid var(--border-color); color: var(--color-text); border-radius: var(--radius-md); padding: 6px 10px; font-size: var(--font-size-sm); }
.wf-tool-actions { display: flex; align-items: center; gap: 8px; margin-left: auto; flex-wrap: wrap; }
.wf-saved { font-size: var(--font-size-xs); color: var(--color-success, #16a34a); }

.wf-body { flex: 1; display: flex; min-height: 0; }
.wf-palette { width: 200px; border-right: 1px solid var(--border-color); background: var(--bg-surface); padding: var(--space-sm); overflow-y: auto; }
.wf-panel-title { font-size: var(--font-size-sm); font-weight: 700; color: var(--color-text); margin-bottom: var(--space-sm); }
.wf-pal-group { margin-bottom: var(--space-md); }
.wf-pal-group-title { font-size: var(--font-size-xs); color: var(--color-text-tertiary); text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 6px; }
.wf-pal-item { display: flex; align-items: center; gap: 8px; padding: 8px 10px; margin-bottom: 6px; border: 1px solid var(--border-color); border-radius: var(--radius-md); background: var(--bg-form); cursor: grab; font-size: var(--font-size-sm); transition: var(--transition-base); }
.wf-pal-item:hover { border-color: var(--color-primary); transform: translateY(-1px); }
.wf-pal-icon { font-size: 1rem; }
.wf-pal-hint { font-size: var(--font-size-xs); color: var(--color-text-tertiary); margin-top: var(--space-sm); line-height: 1.5; }

.wf-canvas { position: relative; flex: 1; overflow: hidden; background-color: var(--bg-main); background-image: radial-gradient(var(--border-color) 1px, transparent 1px); background-size: 22px 22px; cursor: grab; }
.wf-canvas:active { cursor: grabbing; }
.wf-world { position: absolute; left: 0; top: 0; width: 0; height: 0; }
.wf-edges { position: absolute; left: 0; top: 0; overflow: visible; width: 1px; height: 1px; pointer-events: none; }
.wf-edge { fill: none; stroke: var(--border-color); stroke-width: 2; }
.wf-edge-active { stroke: var(--color-primary); }
.wf-edge-temp { stroke: var(--color-primary); stroke-dasharray: 6 4; }

.wf-node { position: absolute; background: var(--bg-surface); border: 1px solid var(--border-color); border-radius: var(--radius-md); box-shadow: 0 4px 14px rgba(0,0,0,0.18); user-select: none; cursor: grab; }
.wf-node:active { cursor: grabbing; }
.wf-node-selected { border-color: var(--color-primary); box-shadow: 0 0 0 2px var(--color-primary-light); }
.wf-node-head { display: flex; align-items: center; gap: 6px; padding: 8px 10px; border-bottom: 2px solid; border-radius: var(--radius-md) var(--radius-md) 0 0; background: var(--bg-form); }
.wf-node-icon { font-size: 0.95rem; }
.wf-node-title { font-size: var(--font-size-sm); font-weight: 700; color: var(--color-text); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; flex: 1; }
.wf-node-sub { font-size: var(--font-size-xs); color: var(--color-text-tertiary); padding: 4px 10px 8px; }
.wf-node-status { width: 8px; height: 8px; border-radius: 50%; }
.wf-node-del { position: absolute; top: -10px; right: -10px; width: 20px; height: 20px; border-radius: 50%; border: 1px solid var(--border-color); background: var(--bg-surface); color: var(--color-text-secondary); cursor: pointer; font-size: 14px; line-height: 1; display: none; }
.wf-node:hover .wf-node-del { display: block; }
.wf-node-del:hover { color: var(--color-danger); border-color: var(--color-danger); }

.wf-port { position: absolute; width: 14px; height: 14px; border-radius: 50%; background: var(--bg-surface); border: 2px solid var(--color-primary); cursor: crosshair; z-index: 2; }
.wf-port-in { left: -7px; }
.wf-port-out { right: -7px; }
.wf-port:hover { background: var(--color-primary); }

.wf-zoom { position: absolute; right: 14px; bottom: 14px; display: flex; flex-direction: column; gap: 6px; z-index: 5; }
.wf-zoom button { width: 34px; height: 34px; border-radius: var(--radius-md); border: 1px solid var(--border-color); background: var(--bg-surface); color: var(--color-text); cursor: pointer; font-size: 0.9rem; }
.wf-zoom button:hover { border-color: var(--color-primary); color: var(--color-primary); }

.wf-config { width: 320px; border-left: 1px solid var(--border-color); background: var(--bg-surface); padding: var(--space-md); overflow-y: auto; }
.wf-config-body { display: flex; flex-direction: column; gap: var(--space-sm); }
.wf-cfg-row { display: flex; flex-direction: column; gap: 4px; }
.wf-cfg-row > label { font-size: var(--font-size-xs); color: var(--color-text-secondary); }
.wf-cfg-id { font-size: var(--font-size-xs); color: var(--color-text-tertiary); background: var(--bg-input); padding: 2px 6px; border-radius: var(--radius-sm); }
.wf-cfg-list { display: flex; flex-direction: column; gap: 6px; }
.wf-cfg-item { display: flex; gap: 6px; align-items: center; }
.wf-cfg-item .form-input { flex: 1; }
.wf-cfg-item > .var-ref { flex: 1; min-width: 0; }
.wf-cfg-hint { font-size: var(--font-size-xs); color: var(--color-text-tertiary); line-height: 1.5; background: var(--bg-tag); padding: 6px 8px; border-radius: var(--radius-sm); }
.wf-cfg-subtitle { font-size: var(--font-size-xs); font-weight: 700; color: var(--color-text-secondary); margin-top: var(--space-xs); padding-top: 6px; border-top: 1px dashed var(--border-color); letter-spacing: 0.04em; }
.wf-cfg-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 6px; }
.wf-cfg-card { display: flex; flex-direction: column; gap: 6px; padding: 8px; border: 1px solid var(--border-color); border-radius: var(--radius-md); background: var(--bg-form); }
.wf-cfg-cond-head { display: flex; align-items: center; justify-content: space-between; font-size: var(--font-size-xs); font-weight: 600; color: var(--color-text-secondary); }
.wf-cfg-check { display: inline-flex; align-items: center; gap: 4px; font-size: var(--font-size-xs); color: var(--color-text-secondary); white-space: nowrap; cursor: pointer; }
.wf-req-star { color: var(--color-danger); margin-left: 2px; }
.wf-bool-input { width: 16px; height: 16px; }
.wf-run-error { color: var(--color-danger); font-size: var(--font-size-xs); margin: 4px 0 0; }
.wf-code { font-family: var(--font-mono, monospace); font-size: var(--font-size-xs); }
.wf-no-sel { color: var(--color-text-tertiary); font-size: var(--font-size-sm); padding: var(--space-md) 0; }
.wf-del-btn { margin-top: var(--space-sm); align-self: flex-start; color: var(--color-danger); border-color: var(--color-danger); }

.wf-overlay { position: fixed; inset: 0; background: rgba(0,0,0,0.6); display: flex; align-items: center; justify-content: center; z-index: 1000; padding: var(--space-md); }
.wf-dialog { width: min(480px, 100%); background: var(--bg-surface); border: 1px solid var(--border-strong); border-radius: var(--radius-lg); padding: var(--space-lg); box-shadow: var(--shadow-xl); }
.wf-dialog-hint { font-size: var(--font-size-xs); color: var(--color-text-tertiary); margin: 0 0 var(--space-sm); }
.wf-runs-vars { display: flex; flex-direction: column; gap: var(--space-sm); }
.wf-list { display: flex; flex-direction: column; gap: 6px; max-height: 320px; overflow-y: auto; }
.wf-list-item { display: flex; justify-content: space-between; align-items: center; padding: 10px 12px; border: 1px solid var(--border-color); border-radius: var(--radius-md); cursor: pointer; font-size: var(--font-size-sm); }
.wf-list-item:hover { border-color: var(--color-primary); }
.wf-list-meta { font-size: var(--font-size-xs); color: var(--color-text-tertiary); }

.wf-results { position: fixed; right: 336px; bottom: 14px; width: 360px; max-height: 50vh; overflow-y: auto; background: var(--bg-surface); border: 1px solid var(--border-color); border-radius: var(--radius-lg); padding: var(--space-md); box-shadow: var(--shadow-xl); z-index: 6; }
.wf-end-out { margin-bottom: var(--space-sm); }
.wf-end-out pre, .wf-res-out { margin: 0; padding: var(--space-sm); background: var(--bg-input); border: 1px solid var(--border-color); border-radius: var(--radius-md); font-size: var(--font-size-xs); white-space: pre-wrap; word-break: break-word; max-height: 160px; overflow-y: auto; color: var(--color-text); }
.wf-res-node { margin-top: var(--space-sm); }
.wf-res-head { display: flex; align-items: center; gap: 8px; margin-bottom: 4px; }
.wf-res-name { font-size: var(--font-size-xs); font-weight: 600; color: var(--color-text); flex: 1; }
.wf-res-ms { font-size: var(--font-size-xs); color: var(--color-text-tertiary); }
.wf-res-err { color: var(--color-danger); font-size: var(--font-size-xs); }

.wf-error { position: absolute; left: 50%; top: 14px; transform: translateX(-50%); background: var(--color-danger-light); color: var(--color-danger); padding: 6px 12px; border-radius: var(--radius-md); font-size: var(--font-size-xs); z-index: 10; max-width: 80%; }

.st-success, .st-done { background: var(--color-success, #16a34a); }
.st-error { background: var(--color-danger); }
.st-skipped { background: var(--color-text-tertiary); }
.st-running, .st-pending { background: var(--color-warning); }
.wf-res-status, .wf-node-status.st-success, .wf-node-status.st-error, .wf-node-status.st-skipped, .wf-node-status.st-running { display: inline-block; width: 8px; height: 8px; border-radius: 50%; }
.wf-res-status { margin-right: 2px; }
</style>
