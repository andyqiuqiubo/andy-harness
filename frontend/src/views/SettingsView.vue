<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { useProviderStore } from '../stores/providers'
import { usePluginStore, type PluginInfo, type MarketplacePlugin } from '../stores/plugins'
import { useSettingsStore, THEME_LIST, type Theme } from '../stores/settings'
import { useSkillStore, type SkillInfo } from '../stores/skills'
import { usePermissionStore } from '../stores/permissions'
import { useLanguage } from '../composables/useLanguage'
import { apiClient } from '../api/client'
import { formatLocalTime } from '../utils/datetime'
import type { PermissionAction, PermissionMode } from '../api/types'

type Tab = 'providers' | 'session' | 'plugins' | 'skills' | 'mcp' | 'artifacts' | 'memory' | 'tracing' | 'schedule' | 'general'

const providerStore = useProviderStore()
const pluginStore = usePluginStore()
const settingsStore = useSettingsStore()
const skillStore = useSkillStore()
const permissionStore = usePermissionStore()
const { t } = useLanguage()

const activeTab = ref<Tab>('providers')

// Provider 表单
const showAddProvider = ref(false)
const newProvider = ref({
  name: '',
  base_url: '',
  api_key: '',
  models: '',
})
const testStatus = ref<Record<string, { loading: boolean; result: boolean | null }>>({})

// H10: Provider edit state
const editingProvider = ref<string | null>(null)
const editForm = ref({
  name: '',
  base_url: '',
  models: '',
  api_key: '',
})
const editSaving = ref(false)
const editTestError = ref('')

// S26: Session settings bound to settings store
const sessionSettings = computed({
  get: () => settingsStore.sessionSettings,
  set: (val) => settingsStore.setSessionSettings(val),
})

// S27: Plugin config state
const selectedPluginForConfig = ref<string | null>(null)
const pluginConfigForm = ref<Record<string, unknown>>({})
const configLoading = ref(false)

// 当前选中的插件对象（用于 Modal 渲染 config_schema）
const selectedPlugin = computed(() =>
  selectedPluginForConfig.value
    ? pluginStore.plugins.find((p) => p.id === selectedPluginForConfig.value) ?? null
    : null,
)

// S26: Save session settings
function handleSaveSessionSettings() {
  settingsStore.setSessionSettings({
    model: sessionSettings.value.model,
    temperature: sessionSettings.value.temperature,
    system_prompt: sessionSettings.value.system_prompt,
  })
  alert(t.value('session.saved'))
}

async function handleAddProvider() {
  if (!newProvider.value.name || !newProvider.value.base_url || !newProvider.value.api_key) return
  try {
    await apiClient.post('/providers', {
      name: newProvider.value.name,
      base_url: newProvider.value.base_url,
      api_key: newProvider.value.api_key,
      models: newProvider.value.models ? newProvider.value.models.split(',').map((m) => m.trim()) : undefined,
    })
    await providerStore.loadProviders()
    showAddProvider.value = false
    newProvider.value = { name: '', base_url: '', api_key: '', models: '' }
  } catch (e) {
    alert('添加失败: ' + e)
  }
}

async function handleDeleteProvider(id: string) {
  if (!confirm('确认删除此 Provider?')) return
  try {
    await apiClient.delete(`/providers/${id}`)
    await providerStore.loadProviders()
  } catch (e) {
    alert('删除失败: ' + e)
  }
}

async function handleTestProvider(id: string) {
  testStatus.value[id] = { loading: true, result: null }
  try {
    const res = await apiClient.post<{ connected: boolean; error?: string }>(`/providers/${id}/test`)
    testStatus.value[id] = { loading: false, result: res.connected }
  } catch {
    testStatus.value[id] = { loading: false, result: false }
  }
}

// H10: Start editing a provider
function startEditProvider(id: string) {
  const p = providerStore.providers.find((p) => p.id === id)
  if (!p) return
  editingProvider.value = id
  editTestError.value = ''
  editForm.value = {
    name: p.name,
    base_url: p.base_url,
    models: p.models.join(', '),
    api_key: '',
  }
}

// 启用：保存配置 → 自动测试连接 → 成功则启用并关闭，失败则显示错误
async function handleSaveProvider(id: string) {
  editSaving.value = true
  editTestError.value = ''
  try {
    // 1. 保存配置
    await providerStore.updateProvider(id, {
      name: editForm.value.name,
      base_url: editForm.value.base_url,
      models: editForm.value.models ? editForm.value.models.split(',').map((m) => m.trim()) : [],
      ...(editForm.value.api_key ? { api_key: editForm.value.api_key } : {}),
    })

    // 2. 自动测试连接
    let connected = false
    let testDetail = ''
    try {
      const res = await apiClient.post<{ connected: boolean; error?: string }>(`/providers/${id}/test`)
      connected = res.connected
      if (!connected && res.error) testDetail = res.error
    } catch (e) {
      testDetail = String(e)
    }

    if (!connected) {
      editTestError.value = testDetail || t.value('providers.editTestFail')
      // 测试失败：确保停用状态
      const p = providerStore.providers.find((p) => p.id === id)
      if (p && p.enabled) {
        await providerStore.toggleProviderEnabled(id)
      }
      return
    }

    // 3. 测试成功：启用并关闭弹窗
    const p = providerStore.providers.find((p) => p.id === id)
    if (p && !p.enabled) {
      await providerStore.toggleProviderEnabled(id)
    }
    editingProvider.value = null
  } catch (e) {
    editTestError.value = '保存失败: ' + e
  } finally {
    editSaving.value = false
  }
}

// H10: Toggle provider enabled/disabled
// 启用前必须先通过连接测试；停用直接生效
async function handleToggleProvider(id: string) {
  const p = providerStore.providers.find((p) => p.id === id)
  if (!p) return

  if (!p.enabled) {
    // 启用：先测试连接
    testStatus.value[id] = { loading: true, result: null }
    let connected = false
    let errMsg = ''
    try {
      const res = await apiClient.post<{ connected: boolean; error?: string }>(`/providers/${id}/test`)
      connected = res.connected
      if (!connected && res.error) errMsg = res.error
    } catch (e) {
      errMsg = String(e)
    }
    testStatus.value[id] = { loading: false, result: connected }
    if (!connected) {
      alert((t.value('providers.editTestFail')) + (errMsg ? ': ' + errMsg : ''))
      return
    }
  }

  try {
    await providerStore.toggleProviderEnabled(id)
  } catch (e) {
    alert('操作失败: ' + e)
  }
}

async function handleActivatePlugin(id: string) {
  try {
    await pluginStore.activatePlugin(id)
  } catch (e) {
    alert('激活失败: ' + e)
  }
}

async function handleDeactivatePlugin(id: string) {
  try {
    await pluginStore.deactivatePlugin(id)
  } catch (e) {
    const msg = String(e)
    if (msg.includes('PLUGIN_DEACTIVATE_FORBIDDEN') || msg.includes('核心插件')) {
      alert('核心插件不可停用')
    } else {
      alert('停用失败: ' + e)
    }
  }
}

// S27: Load plugin config schema for rendering
async function handleLoadPluginConfig(plugin: PluginInfo) {
  selectedPluginForConfig.value = plugin.id
  configLoading.value = true
  const res = await pluginStore.fetchPluginConfig(plugin.id)
  if (res?.config) {
    pluginConfigForm.value = { ...res.config }
  } else {
    pluginConfigForm.value = {}
  }
  configLoading.value = false
}

// S27: Cancel — close config modal without saving
function handleCancelPluginConfig() {
  selectedPluginForConfig.value = null
  pluginConfigForm.value = {}
}

// S27: Save plugin config
async function handleSavePluginConfig(id: string) {
  try {
    await pluginStore.savePluginConfig(id, pluginConfigForm.value)
    selectedPluginForConfig.value = null
  } catch (e) {
    alert('保存配置失败: ' + e)
  }
}

// S27: Get config schema properties for rendering
function getConfigSchemaProperties(plugin: PluginInfo): { key: string; type: string; description?: string; default?: unknown }[] {
  const schema = plugin.config_schema as { properties?: Record<string, unknown> } | undefined
  if (!schema?.properties) return []
  return Object.entries(schema.properties).map(([key, val]) => ({
    key,
    type: (val as { type?: string }).type || 'string',
    description: (val as { description?: string }).description,
    default: (val as { default?: unknown }).default,
  }))
}

// 插件市场
const showMarketplace = ref(false)
const marketplaceLoading = ref(false)
const marketplaceInstalling = ref<string | null>(null)
// 说明弹窗：当前查看说明的市场插件
const marketplaceDescribeTarget = ref<MarketplacePlugin | null>(null)

async function handleToggleMarketplace() {
  showMarketplace.value = !showMarketplace.value
  if (showMarketplace.value) {
    marketplaceLoading.value = true
    await pluginStore.fetchMarketplace()
    marketplaceLoading.value = false
  }
}

async function handleMarketplaceInstall(mp: MarketplacePlugin) {
  if (!confirm(`确定从插件市场安装「${mp.name}」吗？`)) return
  marketplaceInstalling.value = mp.plugin_id
  try {
    await pluginStore.installMarketplacePlugin(mp.plugin_id)
  } catch (e) {
    alert('安装失败: ' + e)
  } finally {
    marketplaceInstalling.value = null
  }
}

// 卸载插件
async function handleUninstallPlugin(id: string) {
  if (!confirm(`确定卸载插件 ${id}？此操作将删除插件文件，不可撤销。`)) return
  try {
    await pluginStore.uninstallPlugin(id)
  } catch (e) {
    alert('卸载失败: ' + e)
  }
}

// ── Skills ──────────────────────────────────────────
const skillDetailTarget = ref<SkillInfo | null>(null)
const skillDetail = ref<{ body: string; resources: string[] } | null>(null)
const skillDetailLoading = ref(false)
const skillReloading = ref(false)

async function handleOpenSkillDetail(skill: SkillInfo) {
  skillDetailTarget.value = skill
  skillDetail.value = null
  skillDetailLoading.value = true
  try {
    const res = await skillStore.fetchSkillDetail(skill.name)
    skillDetail.value = res ? { body: res.body, resources: res.resources } : null
  } finally {
    skillDetailLoading.value = false
  }
}

function handleCloseSkillDetail() {
  skillDetailTarget.value = null
  skillDetail.value = null
}

async function handleToggleSkill(skill: SkillInfo, enabled: boolean) {
  try {
    await skillStore.setSkillEnabled(skill.name, enabled)
    if (skillDetailTarget.value?.name === skill.name) {
      skillDetailTarget.value.enabled = enabled
    }
  } catch (e) {
    alert('操作失败: ' + e)
  }
}

// ── 权限策略 ────────────────────────────────────────
async function handleChangePermissionMode(e: Event) {
  const mode = (e.target as HTMLSelectElement).value
  if (!mode) return
  try {
    await permissionStore.setMode(mode as PermissionMode)
  } catch (err) {
    alert('更新失败: ' + err)
  }
}

async function handleChangeToolOverride(toolName: string, e: Event) {
  const value = (e.target as HTMLSelectElement).value
  try {
    await permissionStore.setOverride(toolName, (value || null) as PermissionAction | null)
  } catch (err) {
    alert('更新失败: ' + err)
  }
}

async function handleReloadSkills() {
  skillReloading.value = true
  try {
    const count = await skillStore.reloadSkills()
    alert(`已重新扫描，共发现 ${count} 个 Skill`)
  } catch (e) {
    alert('重新扫描失败: ' + e)
  } finally {
    skillReloading.value = false
  }
}

// ── 标签栏横向滚动（箭头替代原生横向滚动条）────────────
const tabNavRef = ref<HTMLElement | null>(null)
const tabOverflow = ref(false)
const canScrollLeft = ref(false)
const canScrollRight = ref(false)

function updateTabScrollState() {
  const el = tabNavRef.value
  if (!el) return
  tabOverflow.value = el.scrollWidth - el.clientWidth > 1
  canScrollLeft.value = el.scrollLeft > 1
  canScrollRight.value = el.scrollLeft + el.clientWidth < el.scrollWidth - 1
}

function scrollTabs(dir: number) {
  const el = tabNavRef.value
  if (!el) return
  const amount = Math.max(120, Math.round(el.clientWidth * 0.8))
  el.scrollBy({ left: dir * amount, behavior: 'smooth' })
}

onMounted(() => {
  updateTabScrollState()
  window.addEventListener('resize', updateTabScrollState)
  providerStore.loadProviders()
  pluginStore.loadPlugins()
  skillStore.loadSkills()
  permissionStore.loadPermissions()
  loadMcp()
  loadArtifacts()
  loadMemories()
  loadMemorySummaryInfo()
  loadTraces()
  loadSchedules()
  loadScheduleOptions()
})

// ── MCP 客户端 ──────────────────────────────
interface McpServerInfo {
  name: string
  type: string
  command: string
  args: string[]
  url: string
  description: string
  enabled: boolean
  connected: boolean
  tools: string[]
}
const mcpServers = ref<McpServerInfo[]>([])
const mcpLoading = ref(false)
const mcpError = ref('')
const mcpNew = ref({
  type: 'stdio',
  name: '',
  command: '',
  args: '',
  url: '',
  headers: '',
  description: '',
})

async function loadMcp() {
  try {
    const data = await apiClient.get<{ available: boolean; servers: McpServerInfo[] }>('/mcp/servers')
    mcpServers.value = data.available ? (data.servers || []) : []
  } catch (e) {
    mcpServers.value = []
  }
}

async function handleReloadMcp() {
  mcpLoading.value = true
  mcpError.value = ''
  try {
    const data = await apiClient.post<{ connected: string[] }>('/mcp/refresh')
    await loadMcp()
    alert(`${t.value('mcp.refreshDone')} ${data.connected?.join(', ') || '-'}`)
  } catch (e) {
    mcpError.value = String(e)
  } finally {
    mcpLoading.value = false
  }
}

async function handleAddMcp() {
  mcpLoading.value = true
  mcpError.value = ''
  try {
    const body: Record<string, unknown> = {
      name: mcpNew.value.name,
      type: mcpNew.value.type,
    }
    if (mcpNew.value.type === 'stdio') {
      body.command = mcpNew.value.command
      body.args = mcpNew.value.args ? mcpNew.value.args.split(/\s+/).filter(Boolean) : []
    } else {
      body.url = mcpNew.value.url
      body.description = mcpNew.value.description
      body.headers = parseHeaderLines(mcpNew.value.headers)
    }
    const data = await apiClient.post<{ connected: boolean; error?: string }>('/mcp/servers', body)
    if (data.connected) {
      mcpNew.value = { type: mcpNew.value.type, name: '', command: '', args: '', url: '', headers: '', description: '' }
      await loadMcp()
    } else {
      mcpError.value = data.error || t.value('mcp.connectFail')
    }
  } catch (e) {
    mcpError.value = String(e)
  } finally {
    mcpLoading.value = false
  }
}

/** 解析 "Key: Value" 多行文本为 headers 对象。 */
function parseHeaderLines(raw: string): Record<string, string> {
  const out: Record<string, string> = {}
  for (const line of raw.split('\n')) {
    const idx = line.indexOf(':')
    if (idx > 0) {
      const k = line.slice(0, idx).trim()
      const v = line.slice(idx + 1).trim()
      if (k) out[k] = v
    }
  }
  return out
}

async function handleDeleteMcp(name: string) {
  if (!window.confirm(t.value('mcp.confirmRemove') + ` (${name})`)) return
  try {
    await apiClient.delete(`/mcp/servers/${encodeURIComponent(name)}`)
    await loadMcp()
  } catch (e) {
    mcpError.value = String(e)
  }
}

// ── 工件（大工具输出落盘） ────────────────────
interface ArtifactItem {
  id: string
  session_id: string
  tool_name: string
  char_count: number
  size_bytes: number
  summary: string
  created_at: string
}
const artifacts = ref<ArtifactItem[]>([])
const artifactsAvailable = ref(true)
const artifactError = ref('')
const artifactViewId = ref('')
const artifactViewContent = ref('')

async function loadArtifacts() {
  try {
    const data = await apiClient.get<{ available: boolean; artifacts: ArtifactItem[] }>(
      '/artifacts?limit=200',
    )
    artifactsAvailable.value = data.available
    artifacts.value = data.available ? data.artifacts || [] : []
  } catch (e) {
    artifacts.value = []
  }
}

async function handleViewArtifact(id: string) {
  artifactError.value = ''
  if (artifactViewId.value === id) {
    artifactViewId.value = ''
    artifactViewContent.value = ''
    return
  }
  try {
    const data = await apiClient.get<{ content: string }>(`/artifacts/${encodeURIComponent(id)}`)
    artifactViewId.value = id
    artifactViewContent.value = data.content || ''
  } catch (e) {
    artifactError.value = String(e)
  }
}

async function handleDeleteArtifact(id: string) {
  if (!window.confirm(t.value('artifacts.confirmDelete') + ` (${id})`)) return
  try {
    await apiClient.delete(`/artifacts/${encodeURIComponent(id)}`)
    if (artifactViewId.value === id) {
      artifactViewId.value = ''
      artifactViewContent.value = ''
    }
    await loadArtifacts()
  } catch (e) {
    artifactError.value = String(e)
  }
}

function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`
  return `${(n / 1024 / 1024).toFixed(2)} MB`
}

// ── 长期记忆 ────────────────────────────────
interface MemoryItem {
  id: string
  key: string
  value: string
  scope: string
  session_id: string
  tags: string[]
  updated_at: string
}
const memories = ref<MemoryItem[]>([])
const memoryAvailable = ref(true)
const memoryQuery = ref('')
const memoryError = ref('')
const memoryForm = ref({ key: '', value: '', scope: 'global', tags: '' })
const memoryEditingId = ref('')
const memoryEdit = ref({ value: '', tags: '' })

async function loadMemories() {
  memoryError.value = ''
  try {
    const q = memoryQuery.value.trim()
    const path = q ? `/memories?query=${encodeURIComponent(q)}&limit=200` : '/memories?limit=200'
    const data = await apiClient.get<{ available: boolean; memories: MemoryItem[] }>(path)
    memoryAvailable.value = data.available
    memories.value = data.available ? data.memories || [] : []
  } catch (e) {
    memories.value = []
  }
}

function parseTags(raw: string): string[] {
  return raw.split(',').map(s => s.trim()).filter(Boolean)
}

async function handleSaveMemory() {
  memoryError.value = ''
  if (!memoryForm.value.key.trim() || !memoryForm.value.value.trim()) {
    memoryError.value = t.value('memory.required')
    return
  }
  try {
    await apiClient.post('/memories', {
      key: memoryForm.value.key.trim(),
      value: memoryForm.value.value,
      scope: memoryForm.value.scope,
      tags: parseTags(memoryForm.value.tags),
    })
    memoryForm.value = { key: '', value: '', scope: 'global', tags: '' }
    await loadMemories()
  } catch (e) {
    memoryError.value = String(e)
  }
}

function handleStartEditMemory(m: MemoryItem) {
  memoryEditingId.value = m.id
  memoryEdit.value = { value: m.value, tags: (m.tags || []).join(', ') }
}

async function handleSaveEditMemory(id: string) {
  memoryError.value = ''
  try {
    await apiClient.patch(`/memories/${encodeURIComponent(id)}`, {
      value: memoryEdit.value.value,
      tags: parseTags(memoryEdit.value.tags),
    })
    memoryEditingId.value = ''
    await loadMemories()
  } catch (e) {
    memoryError.value = String(e)
  }
}

async function handleDeleteMemory(id: string) {
  if (!window.confirm(t.value('memory.confirmDelete') + ` (${id})`)) return
  try {
    await apiClient.delete(`/memories/${encodeURIComponent(id)}`)
    await loadMemories()
  } catch (e) {
    memoryError.value = String(e)
  }
}

// ── 记忆自动总结 ────────────────────────────
interface MemorySummaryInfo {
  enabled: boolean
  available: boolean
  interval_seconds: number
  max_sessions: number
}
const memorySummaryInfo = ref<MemorySummaryInfo | null>(null)
const memorySummarizing = ref(false)
const memorySummaryMsg = ref('')

async function loadMemorySummaryInfo() {
  try {
    memorySummaryInfo.value = await apiClient.get<MemorySummaryInfo>('/memories/summary-info')
  } catch {
    memorySummaryInfo.value = null
  }
}

async function handleSummarizeNow() {
  memorySummarizing.value = true
  memorySummaryMsg.value = ''
  try {
    const res = await apiClient.post<{
      ok: boolean
      scanned: number
      summarized: number
      saved: number
      skipped_reason: string
    }>('/memories/summarize?max_sessions=5', {})
    if (res.skipped_reason) {
      memorySummaryMsg.value = res.skipped_reason
    } else {
      memorySummaryMsg.value = `${t.value('memory.summaryDone')}：扫描 ${res.scanned} · 总结 ${res.summarized} · 新增记忆 ${res.saved}`
    }
    await loadMemories()
  } catch (e) {
    memorySummaryMsg.value = String(e)
  } finally {
    memorySummarizing.value = false
  }
}

function formatInterval(sec: number): string {
  if (!sec) return '-'
  if (sec % 3600 === 0) return `${sec / 3600} 小时`
  return `${Math.round(sec / 60)} 分钟`
}

// ── 定时任务 ────────────────────────────────
interface ScheduleSpec {
  type: string
  time: string
  weekdays: number[]
  interval_minutes: number
  run_at: string
}
interface ScheduleTask {
  id: string
  name: string
  description: string
  enabled: boolean
  schedule: ScheduleSpec
  schedule_desc: string
  prompt: string
  mcp_servers: string[]
  skills: string[]
  tools: string[]
  provider_id: string
  model: string
  max_iterations: number
  timeout_seconds: number
  next_run_at: string
  last_run_at: string
  last_status: string
  last_error: string
  run_count: number
  fail_count: number
}
interface ScheduleOptions {
  mcp_servers: Array<{ name: string; type: string; connected: boolean; tool_count: number; description: string }>
  skills: Array<{ name: string; description: string; enabled: boolean; source: string }>
  tools: Array<{ name: string; description: string; risk: string }>
  providers: Array<{ id: string; name: string; models: string[]; ready: boolean }>
}
interface TaskRun {
  id: string
  started_at: string
  finished_at: string
  status: string
  duration_ms: number
  session_id: string
  summary: string
  error: string
  trigger: string
}

const scheduleTasks = ref<ScheduleTask[]>([])
const scheduleAvailable = ref(true)
const scheduleError = ref('')
const scheduleOptions = ref<ScheduleOptions>({ mcp_servers: [], skills: [], tools: [], providers: [] })
const scheduleRuns = ref<Record<string, TaskRun[]>>({})
const scheduleRunning = ref('')
const scheduleSaving = ref(false)
const showScheduleForm = ref(false)

const WEEKDAYS = [
  { value: 0, label: '一' },
  { value: 1, label: '二' },
  { value: 2, label: '三' },
  { value: 3, label: '四' },
  { value: 4, label: '五' },
  { value: 5, label: '六' },
  { value: 6, label: '日' },
]

function emptyScheduleForm() {
  const now = new Date()
  now.setMinutes(now.getMinutes() + 10)
  const pad = (n: number) => String(n).padStart(2, '0')
  return {
    id: '',
    name: '',
    description: '',
    enabled: true,
    type: 'daily',
    time: '09:00',
    weekdays: [0, 1, 2, 3, 4] as number[],
    intervalValue: 1,
    intervalUnit: 'hours' as 'minutes' | 'hours',
    runAt: `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}T${pad(now.getHours())}:${pad(now.getMinutes())}`,
    prompt: '',
    mcpServers: [] as string[],
    skills: [] as string[],
    tools: [] as string[],
    providerId: '',
    model: '',
    maxIterations: 8,
    timeoutSeconds: 300,
  }
}
const scheduleForm = ref(emptyScheduleForm())

async function loadSchedules() {
  scheduleError.value = ''
  try {
    const data = await apiClient.get<{ available: boolean; tasks: ScheduleTask[] }>('/schedules')
    scheduleAvailable.value = data.available
    scheduleTasks.value = data.available ? data.tasks || [] : []
  } catch (e) {
    scheduleTasks.value = []
  }
}

async function loadScheduleOptions() {
  try {
    scheduleOptions.value = await apiClient.get<ScheduleOptions>('/schedules/options')
  } catch {
    scheduleOptions.value = { mcp_servers: [], skills: [], tools: [], providers: [] }
  }
}

function openScheduleForm(task?: ScheduleTask) {
  if (!task) {
    scheduleForm.value = emptyScheduleForm()
  } else {
    const s = task.schedule || ({} as ScheduleSpec)
    const minutes = s.interval_minutes || 60
    scheduleForm.value = {
      id: task.id,
      name: task.name,
      description: task.description,
      enabled: task.enabled,
      type: s.type || 'daily',
      time: s.time || '09:00',
      weekdays: s.weekdays && s.weekdays.length ? [...s.weekdays] : [0, 1, 2, 3, 4],
      intervalValue: minutes % 60 === 0 ? minutes / 60 : minutes,
      intervalUnit: minutes % 60 === 0 ? 'hours' : 'minutes',
      runAt: (s.run_at || '').replace(' ', 'T'),
      prompt: task.prompt,
      mcpServers: [...(task.mcp_servers || [])],
      skills: [...(task.skills || [])],
      tools: [...(task.tools || [])],
      providerId: task.provider_id || '',
      model: task.model || '',
      maxIterations: task.max_iterations || 8,
      timeoutSeconds: task.timeout_seconds || 300,
    }
  }
  showScheduleForm.value = true
  loadScheduleOptions()
}

function togglePick(field: 'mcpServers' | 'skills' | 'tools', value: string) {
  const list = scheduleForm.value[field]
  const idx = list.indexOf(value)
  if (idx >= 0) list.splice(idx, 1)
  else list.push(value)
}

function toggleWeekday(day: number) {
  const list = scheduleForm.value.weekdays
  const idx = list.indexOf(day)
  if (idx >= 0) list.splice(idx, 1)
  else list.push(day)
}

function buildSchedulePayload() {
  const f = scheduleForm.value
  const schedule: ScheduleSpec = {
    type: f.type,
    time: f.time,
    weekdays: f.type === 'weekly' ? [...f.weekdays].sort((a, b) => a - b) : [],
    interval_minutes: f.intervalUnit === 'hours' ? f.intervalValue * 60 : f.intervalValue,
    run_at: f.type === 'once' ? f.runAt.replace('T', ' ') : '',
  }
  return {
    name: f.name.trim(),
    description: f.description,
    enabled: f.enabled,
    schedule,
    prompt: f.prompt,
    mcp_servers: [...f.mcpServers],
    skills: [...f.skills],
    tools: [...f.tools],
    provider_id: f.providerId,
    model: f.model,
    max_iterations: Number(f.maxIterations) || 8,
    timeout_seconds: Number(f.timeoutSeconds) || 300,
  }
}

async function saveSchedule() {
  const f = scheduleForm.value
  scheduleError.value = ''
  if (!f.name.trim()) {
    scheduleError.value = t.value('schedule.errName')
    return
  }
  if (!f.prompt.trim()) {
    scheduleError.value = t.value('schedule.errPrompt')
    return
  }
  scheduleSaving.value = true
  try {
    const payload = buildSchedulePayload()
    if (f.id) {
      await apiClient.patch(`/schedules/${encodeURIComponent(f.id)}`, payload)
    } else {
      await apiClient.post('/schedules', payload)
    }
    showScheduleForm.value = false
    await loadSchedules()
  } catch (e) {
    scheduleError.value = String(e)
  } finally {
    scheduleSaving.value = false
  }
}

async function toggleSchedule(task: ScheduleTask) {
  scheduleError.value = ''
  try {
    await apiClient.patch(`/schedules/${encodeURIComponent(task.id)}`, { enabled: !task.enabled })
    await loadSchedules()
  } catch (e) {
    scheduleError.value = String(e)
  }
}

async function deleteSchedule(task: ScheduleTask) {
  if (!window.confirm(t.value('schedule.confirmDelete') + ` (${task.name})`)) return
  try {
    await apiClient.delete(`/schedules/${encodeURIComponent(task.id)}`)
    await loadSchedules()
  } catch (e) {
    scheduleError.value = String(e)
  }
}

async function runScheduleNow(task: ScheduleTask) {
  scheduleRunning.value = task.id
  scheduleError.value = ''
  try {
    const res = await apiClient.post<{ outcome: { status: string; summary: string; error: string } }>(
      `/schedules/${encodeURIComponent(task.id)}/run`,
      {},
    )
    const o = res.outcome
    window.alert(
      o.status === 'ok'
        ? `${t.value('schedule.runOk')}\n\n${(o.summary || '').slice(0, 800)}`
        : `${o.status}: ${o.error || ''}`,
    )
    await loadSchedules()
    if (scheduleRuns.value[task.id]) await loadRuns(task.id)
  } catch (e) {
    scheduleError.value = String(e)
  } finally {
    scheduleRunning.value = ''
  }
}

async function loadRuns(taskId: string) {
  try {
    const data = await apiClient.get<{ runs: TaskRun[] }>(
      `/schedules/${encodeURIComponent(taskId)}/runs?limit=10`,
    )
    scheduleRuns.value = { ...scheduleRuns.value, [taskId]: data.runs || [] }
  } catch (e) {
    scheduleError.value = String(e)
  }
}

async function toggleRuns(task: ScheduleTask) {
  if (scheduleRuns.value[task.id]) {
    const next = { ...scheduleRuns.value }
    delete next[task.id]
    scheduleRuns.value = next
    return
  }
  await loadRuns(task.id)
}

function modelChoices(providerId: string): string[] {
  const p = scheduleOptions.value.providers.find((x) => x.id === providerId)
  return p?.models || []
}

// ── 运行轨迹（Tracing） ─────────────────────
interface TraceSummary {
  trace_id: string
  session_id: string
  name: string
  started_at: string
  duration_ms: number
  span_count: number
  step_count: number
  total_tokens: number
  error_count: number
  status: string
}
interface SpanItem {
  id: string
  trace_id: string
  name: string
  kind: string
  status: string
  duration_ms: number
  prompt_tokens: number
  completion_tokens: number
  total_tokens: number
  input_preview: string
  output_preview: string
  error: string
  created_at: string
}
const traces = ref<TraceSummary[]>([])
const tracesAvailable = ref(true)
const traceError = ref('')
const traceDetailId = ref('')
const traceSpans = ref<SpanItem[]>([])

async function loadTraces() {
  traceError.value = ''
  try {
    const data = await apiClient.get<{ available: boolean; traces: TraceSummary[] }>('/traces?limit=100')
    tracesAvailable.value = data.available
    traces.value = data.available ? data.traces || [] : []
    if (traceDetailId.value && !traces.value.some(x => x.trace_id === traceDetailId.value)) {
      traceDetailId.value = ''
      traceSpans.value = []
    }
  } catch (e) {
    traces.value = []
  }
}

async function toggleTrace(traceId: string) {
  if (traceDetailId.value === traceId) {
    traceDetailId.value = ''
    traceSpans.value = []
    return
  }
  try {
    const data = await apiClient.get<{ spans: SpanItem[] }>(`/traces/${encodeURIComponent(traceId)}`)
    traceSpans.value = data.spans || []
    traceDetailId.value = traceId
  } catch (e) {
    traceError.value = String(e)
  }
}

async function handleDeleteTrace(traceId: string) {
  if (!window.confirm(t.value('tracing.confirmDelete') + ` (${traceId})`)) return
  try {
    await apiClient.delete(`/traces/${encodeURIComponent(traceId)}`)
    if (traceDetailId.value === traceId) {
      traceDetailId.value = ''
      traceSpans.value = []
    }
    await loadTraces()
  } catch (e) {
    traceError.value = String(e)
  }
}

// 泳道条：按记录顺序把非 run 的 span 依次铺开（顺序执行，累计偏移即真实位置）
const laneBars = computed(() => {
  const spans = traceSpans.value.filter(s => s.kind !== 'run')
  const total = spans.reduce((sum, s) => sum + Math.max(0, s.duration_ms), 0)
  let acc = 0
  return spans.map(s => {
    const dur = Math.max(0, s.duration_ms)
    const left = total > 0 ? (acc / total) * 100 : 0
    const width = total > 0 ? Math.max((dur / total) * 100, 0.5) : 0
    acc += dur
    return { ...s, left, width }
  })
})

function formatMs(n: number): string {
  if (n < 1000) return `${n} ms`
  return `${(n / 1000).toFixed(2)} s`
}
</script>

<template>
  <div class="settings-view">
    <!-- Header with back link as ghost button -->
    <header class="settings-header">
      <router-link to="/chat" class="back-link">
        <svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <line x1="19" y1="12" x2="5" y2="12" />
          <polyline points="12 19 5 12 12 5" />
        </svg>
        {{ t('settings.back') }}
      </router-link>
      <h1 class="settings-title">{{ t('settings.title') }}</h1>
    </header>

    <!-- Tab navigation as modern pill tabs -->
    <nav class="tab-nav">
      <button
        v-if="tabOverflow"
        class="tab-scroll-btn"
        :class="{ disabled: !canScrollLeft }"
        :disabled="!canScrollLeft"
        type="button"
        :aria-label="t('settings.tab.scrollLeft')"
        @click="scrollTabs(-1)"
      >
        <svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="15 18 9 12 15 6" /></svg>
      </button>
      <div ref="tabNavRef" class="tab-nav-scroll" @scroll="updateTabScrollState">
        <button :class="['tab-btn', { active: activeTab === 'providers' }]" @click="activeTab = 'providers'">
          {{ t('settings.tab.providers') }}
        </button>
        <button :class="['tab-btn', { active: activeTab === 'session' }]" @click="activeTab = 'session'">
          {{ t('settings.tab.session') }}
        </button>
        <button :class="['tab-btn', { active: activeTab === 'plugins' }]" @click="activeTab = 'plugins'">
          {{ t('settings.tab.plugins') }}
        </button>
        <button :class="['tab-btn', { active: activeTab === 'skills' }]" @click="activeTab = 'skills'">
          {{ t('settings.tab.skills') }}
        </button>
        <button :class="['tab-btn', { active: activeTab === 'mcp' }]" @click="activeTab = 'mcp'">
          {{ t('settings.tab.mcp') }}
        </button>
        <button :class="['tab-btn', { active: activeTab === 'artifacts' }]" @click="activeTab = 'artifacts'">
          {{ t('settings.tab.artifacts') }}
        </button>
        <button :class="['tab-btn', { active: activeTab === 'memory' }]" @click="activeTab = 'memory'">
          {{ t('settings.tab.memory') }}
        </button>
        <button :class="['tab-btn', { active: activeTab === 'tracing' }]" @click="activeTab = 'tracing'">
          {{ t('settings.tab.tracing') }}
        </button>
        <button :class="['tab-btn', { active: activeTab === 'schedule' }]" @click="activeTab = 'schedule'">
          {{ t('settings.tab.schedule') }}
        </button>
        <button :class="['tab-btn', { active: activeTab === 'general' }]" @click="activeTab = 'general'">
          {{ t('settings.tab.general') }}
        </button>
      </div>
      <button
        v-if="tabOverflow"
        class="tab-scroll-btn"
        :class="{ disabled: !canScrollRight }"
        :disabled="!canScrollRight"
        type="button"
        :aria-label="t('settings.tab.scrollRight')"
        @click="scrollTabs(1)"
      >
        <svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="9 18 15 12 9 6" /></svg>
      </button>
    </nav>

    <div class="settings-body">
      <!-- ═══════════════ Provider 设置 ═══════════════ -->
      <section v-if="activeTab === 'providers'" class="tab-content">
        <div class="section-header">
          <h2 class="section-title">{{ t('providers.title') }}</h2>
          <button class="btn-primary" @click="showAddProvider = !showAddProvider">
            <svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <line x1="12" y1="5" x2="12" y2="19" />
              <line x1="5" y1="12" x2="19" y2="12" />
            </svg>
            {{ showAddProvider ? t('providers.cancel') : t('providers.add') }}
          </button>
        </div>

        <!-- 新增 Provider 表单 -->
        <Transition name="fade-slide">
          <div v-if="showAddProvider" class="add-form card">
            <h3 class="form-card-title">{{ t('providers.add') }}</h3>
            <div class="form-grid">
              <div class="form-row">
                <label class="form-label">{{ t('providers.name') }}</label>
                <input v-model="newProvider.name" class="form-input" placeholder="My Custom Model" />
              </div>
              <div class="form-row">
                <label class="form-label">{{ t('providers.baseUrl') }}</label>
                <input v-model="newProvider.base_url" class="form-input" placeholder="https://api.example.com/v1" />
              </div>
              <div class="form-row">
                <label class="form-label">{{ t('providers.apiKey') }}</label>
                <input v-model="newProvider.api_key" type="password" class="form-input" placeholder="sk-..." />
              </div>
              <div class="form-row">
                <label class="form-label">{{ t('providers.models') }}</label>
                <input v-model="newProvider.models" class="form-input" placeholder="gpt-4o, gpt-4o-mini" />
              </div>
            </div>
            <button class="btn-primary" @click="handleAddProvider">{{ t('providers.submit') }}</button>
          </div>
        </Transition>

        <!-- Provider 列表 -->
        <div class="provider-list">
          <div v-for="p in providerStore.providers" :key="p.id" class="provider-card card">
            <!-- Normal view -->
            <div v-if="editingProvider !== p.id" class="provider-row">
              <div class="provider-info">
                <div class="provider-name">
                  {{ p.name }}
                  <!-- 密钥状态 -->
                  <span v-if="p.has_api_key" class="badge-pill badge-ok">
                    <svg class="icon-sm" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12" /></svg>
                    {{ t('providers.hasApiKey') }}
                  </span>
                  <span v-else class="badge-pill badge-warn">
                    <svg class="icon-sm" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10" /><line x1="12" y1="8" x2="12" y2="12" /><line x1="12" y1="16" x2="12.01" y2="16" /></svg>
                    {{ t('providers.noApiKey') }}
                  </span>
                  <!-- 启用/停用状态：仅有密钥时才显示 -->
                  <span v-if="p.has_api_key" :class="['badge-pill', p.enabled ? 'badge-enabled' : 'badge-disabled']">
                    {{ p.enabled ? t('providers.enabled') : t('providers.disabled') }}
                  </span>
                </div>
                <div class="provider-url">{{ p.base_url }}</div>
                <div class="provider-models">
                  <span v-for="m in p.models" :key="m" class="model-tag">{{ m }}</span>
                </div>
              </div>
              <div class="provider-actions">
                <button
                  class="btn-ghost"
                  @click="handleTestProvider(p.id)"
                  :disabled="!p.has_api_key || testStatus[p.id]?.loading"
                >
                  <svg v-if="testStatus[p.id]?.loading" class="icon spin" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="2" x2="12" y2="6" /><line x1="12" y1="18" x2="12" y2="22" /><line x1="4.93" y1="4.93" x2="7.76" y2="7.76" /><line x1="16.24" y1="16.24" x2="19.07" y2="19.07" /><line x1="2" y1="12" x2="6" y2="12" /><line x1="18" y1="12" x2="22" y2="12" /><line x1="4.93" y1="19.07" x2="7.76" y2="16.24" /><line x1="16.24" y1="7.76" x2="19.07" y2="4.93" /></svg>
                  <svg v-else class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2" /></svg>
                  {{ testStatus[p.id]?.loading ? t('providers.testing') : t('providers.test') }}
                </button>
                <span v-if="testStatus[p.id]?.result === true" class="test-result test-ok">
                  <svg class="icon-sm" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12" /></svg>
                  {{ t('providers.testOk') }}
                </span>
                <span v-if="testStatus[p.id]?.result === false" class="test-result test-fail">
                  <svg class="icon-sm" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" /></svg>
                  {{ t('providers.testFail') }}
                </span>
                <!-- 编辑按钮：始终可点击 -->
                <button class="btn-ghost" @click="startEditProvider(p.id)">
                  <svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7" /><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z" /></svg>
                  {{ t('providers.edit') }}
                </button>
                <!-- 启用/停用按钮：仅有密钥时可点击 -->
                <button
                  class="btn-ghost"
                  @click="handleToggleProvider(p.id)"
                  :disabled="!p.has_api_key"
                >
                  <svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18.36 6.64a9 9 0 1 1-12.73 0" /><line x1="12" y1="2" x2="12" y2="12" /></svg>
                  {{ p.enabled ? t('providers.disable') : t('providers.enable') }}
                </button>
                <!-- 删除按钮：仅自定义 provider 可删除；有密钥且启用时置灰 -->
                <button
                  v-if="p.id.startsWith('custom_')"
                  class="btn-ghost btn-danger"
                  @click="handleDeleteProvider(p.id)"
                  :disabled="p.has_api_key && p.enabled"
                >
                  <svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6" /><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" /></svg>
                  {{ t('providers.delete') }}
                </button>
              </div>
            </div>

            <!-- Edit view -->
            <div v-else class="edit-form">
              <div class="form-grid">
                <div class="form-row">
                  <label class="form-label">{{ t('providers.name') }}</label>
                  <input v-model="editForm.name" class="form-input" />
                </div>
                <div class="form-row">
                  <label class="form-label">{{ t('providers.baseUrl') }}</label>
                  <input v-model="editForm.base_url" class="form-input" />
                </div>
                <div class="form-row">
                  <label class="form-label">{{ t('providers.models') }}</label>
                  <input v-model="editForm.models" class="form-input" placeholder="model1, model2" />
                </div>
                <div class="form-row">
                  <label class="form-label">{{ t('providers.apiKey') }} (留空不修改)</label>
                  <input v-model="editForm.api_key" type="password" class="form-input" placeholder="sk-..." />
                </div>
              </div>
              <div class="edit-actions">
                <button class="btn-primary" @click="handleSaveProvider(p.id)" :disabled="editSaving">
                  {{ editSaving ? t('providers.testing') : t('providers.enable') }}
                </button>
                <button class="btn-ghost" @click="editingProvider = null" :disabled="editSaving">{{ t('providers.cancel') }}</button>
              </div>
              <div v-if="editTestError" class="edit-test-error">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10" /><line x1="12" y1="8" x2="12" y2="12" /><line x1="12" y1="16" x2="12.01" y2="16" /></svg>
                <span>{{ editTestError }}</span>
              </div>
            </div>
          </div>
          <div v-if="providerStore.providers.length === 0" class="empty-hint">
            {{ t('providers.empty') }}
          </div>
        </div>
      </section>

      <!-- ═══════════════ 会话级设置 ═══════════════ -->
      <section v-if="activeTab === 'session'" class="tab-content">
        <h2 class="section-title">{{ t('session.title') }}</h2>
        <div class="card session-card">
          <div class="form-row">
            <label class="form-label">{{ t('session.model') }}</label>
            <input v-model="sessionSettings.model" class="form-input" placeholder="deepseek-chat" />
          </div>
          <div class="form-row">
            <label class="form-label">{{ t('session.temperature') }}: <span class="temp-value">{{ sessionSettings.temperature }}</span></label>
            <input
              v-model.number="sessionSettings.temperature"
              type="range"
              class="temp-slider"
              min="0"
              max="2"
              step="0.1"
            />
          </div>
          <div class="form-row">
            <label class="form-label">{{ t('session.systemPrompt') }}</label>
            <textarea v-model="sessionSettings.system_prompt" rows="4" class="form-input mono-input" placeholder="..."></textarea>
          </div>
          <!-- S26: Save button -->
          <button class="btn-primary" @click="handleSaveSessionSettings">{{ t('session.save') }}</button>
        </div>
      </section>

      <!-- ═══════════════ 插件管理 ═══════════════ -->
      <section v-if="activeTab === 'plugins'" class="tab-content">
        <div class="section-header">
          <h2 class="section-title">{{ t('plugins.title') }}</h2>
          <button class="btn-primary" @click="handleToggleMarketplace">
            <svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <line x1="12" y1="5" x2="12" y2="19" /><line x1="5" y1="12" x2="19" y2="12" />
            </svg>
            {{ showMarketplace ? '收起' : '从插件市场安装插件' }}
          </button>
        </div>

        <!-- 插件市场 -->
        <Transition name="fade-slide">
          <div v-if="showMarketplace" class="marketplace-panel card">
            <h3 class="form-card-title">插件市场</h3>
            <div v-if="marketplaceLoading" class="modal-loading">加载中…</div>
            <div v-else-if="pluginStore.marketplace.length === 0" class="empty-hint" style="border:none;padding:var(--space-md) 0;">
              插件市场暂无可用插件
            </div>
            <div v-else class="marketplace-list">
              <div v-for="mp in pluginStore.marketplace" :key="mp.plugin_id" class="marketplace-card">
                <div class="marketplace-info">
                  <div class="marketplace-name">
                    {{ mp.name }}
                    <span v-if="mp.installed" class="badge-pill badge-ok">已安装</span>
                  </div>
                  <div class="marketplace-meta">
                    <span class="badge-pill badge-version">v{{ mp.version }}</span>
                    <span class="badge-pill badge-type">{{ mp.type }}</span>
                  </div>
                  <div class="marketplace-desc">{{ mp.description }}</div>
                </div>
                <div class="marketplace-actions">
                  <button class="btn-ghost" @click="marketplaceDescribeTarget = mp">
                    <svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10" /><line x1="12" y1="16" x2="12" y2="12" /><line x1="12" y1="8" x2="12.01" y2="8" /></svg>
                    说明
                  </button>
                  <button
                    v-if="!mp.installed"
                    class="btn-primary"
                    @click="handleMarketplaceInstall(mp)"
                    :disabled="marketplaceInstalling === mp.plugin_id"
                  >
                    {{ marketplaceInstalling === mp.plugin_id ? '安装中…' : '安装' }}
                  </button>
                </div>
              </div>
            </div>
          </div>
        </Transition>

        <div class="plugin-list">
          <div v-for="p in pluginStore.plugins" :key="p.id" class="plugin-card card">
            <div class="plugin-row">
              <div class="plugin-info">
                <div class="plugin-name">
                  {{ p.name }}
                  <span v-if="p.activated" class="badge-pill badge-enabled">已启用</span>
                  <span v-else class="badge-pill badge-disabled">已停用</span>
                  <span v-if="p.core" class="badge-pill badge-core">{{ t('plugins.core') }}</span>
                </div>
                <div class="plugin-meta">
                  <span class="badge-pill badge-version">v{{ p.version }}</span>
                  <span class="badge-pill badge-type">{{ p.type }}</span>
                </div>
                <div v-if="p.permissions && p.permissions.length" class="plugin-permissions">
                  <span class="perm-label">权限:</span>
                  <span v-for="perm in p.permissions" :key="perm" class="perm-tag">{{ perm }}</span>
                </div>
              </div>
              <div class="plugin-actions">
                <label class="switch">
                  <input type="checkbox" :checked="p.activated" :disabled="p.core" @change="p.activated ? handleDeactivatePlugin(p.id) : handleActivatePlugin(p.id)" />
                  <span class="slider"></span>
                </label>
                <span v-if="p.core" class="core-lock" title="核心插件不可停用">
                  <svg class="icon-sm" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="11" width="18" height="11" rx="2" ry="2" /><path d="M7 11V7a5 5 0 0 1 10 0v4" /></svg>
                </span>
                <button class="btn-ghost" @click="handleLoadPluginConfig(p)" title="配置">
                  <svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3" /><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z" /></svg>
                </button>
                <button v-if="p.source === 'marketplace'" class="btn-ghost btn-uninstall" @click="handleUninstallPlugin(p.id)" title="卸载插件">
                  <svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6" /><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" /></svg>
                </button>
              </div>
            </div>
          </div>
          <div v-if="pluginStore.plugins.length === 0" class="empty-hint">{{ t('plugins.empty') }}</div>
        </div>
      </section>

      <!-- ═══════════════ Skills ═══════════════ -->
      <section v-if="activeTab === 'skills'" class="tab-content">
        <div class="section-header">
          <h2 class="section-title">{{ t('skills.title') }}</h2>
          <button class="btn-primary" @click="handleReloadSkills" :disabled="skillReloading">
            <svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <polyline points="23 4 23 10 17 10" /><polyline points="1 20 1 14 7 14" />
              <path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15" />
            </svg>
            {{ skillReloading ? t('skills.scanning') : t('skills.rescan') }}
          </button>
        </div>

        <p class="skills-hint">{{ t('skills.hint') }}</p>

        <div v-if="skillStore.loading" class="empty-hint">{{ t('skills.loading') }}</div>
        <div v-else-if="skillStore.skills.length === 0" class="empty-hint">
          {{ t('skills.empty') }}
        </div>
        <div v-else class="skill-list">
          <div v-for="sk in skillStore.skills" :key="sk.name" class="skill-card card">
            <div class="skill-row">
              <div class="skill-info">
                <div class="skill-name">
                  <code>{{ sk.name }}</code>
                  <span v-if="sk.enabled" class="badge-pill badge-enabled">{{ t('skills.enabled') }}</span>
                  <span v-else class="badge-pill badge-disabled">{{ t('skills.disabled') }}</span>
                  <span class="badge-pill badge-type">{{ t('skills.source.' + sk.source) }}</span>
                </div>
                <div class="skill-desc">{{ sk.description }}</div>
                <div class="skill-meta">
                  <span v-if="sk.version" class="skill-meta-item">v{{ sk.version }}</span>
                  <span class="skill-meta-item">{{ t('skills.bodyChars') }}: {{ sk.body_chars }}</span>
                  <span v-if="sk.resources.length" class="skill-meta-item">
                    {{ t('skills.resources') }}: {{ sk.resources.length }}
                  </span>
                  <span v-if="sk.allowed_tools" class="skill-meta-item">{{ sk.allowed_tools }}</span>
                </div>
              </div>
              <div class="skill-actions">
                <label class="switch">
                  <input type="checkbox" :checked="sk.enabled" @change="handleToggleSkill(sk, !sk.enabled)" />
                  <span class="slider"></span>
                </label>
                <button class="btn-ghost" @click="handleOpenSkillDetail(sk)" :title="t('skills.viewDetail')">
                  <svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                    <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z" /><circle cx="12" cy="12" r="3" />
                  </svg>
                </button>
              </div>
            </div>
          </div>
        </div>

        <!-- Skill 详情弹窗 -->
        <div v-if="skillDetailTarget" class="modal-overlay" @click.self="handleCloseSkillDetail">
          <div class="modal-dialog skill-detail-modal">
            <div class="modal-header">
              <h3 class="modal-title">
                <code>{{ skillDetailTarget.name }}</code>
              </h3>
              <button class="modal-close" @click="handleCloseSkillDetail">
                <svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                  <line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" />
                </svg>
              </button>
            </div>
            <div class="modal-body">
              <div class="skill-detail-desc">{{ skillDetailTarget.description }}</div>
              <div v-if="skillDetailLoading" class="modal-loading">{{ t('skills.loading') }}</div>
              <template v-else-if="skillDetail">
                <div v-if="skillDetail.resources.length" class="skill-detail-section">
                  <div class="skill-detail-label">{{ t('skills.resources') }}</div>
                  <ul class="skill-resource-list">
                    <li v-for="r in skillDetail.resources" :key="r"><code>{{ r }}</code></li>
                  </ul>
                </div>
                <div class="skill-detail-section">
                  <div class="skill-detail-label">{{ t('skills.body') }}</div>
                  <pre class="skill-detail-body">{{ skillDetail.body }}</pre>
                </div>
              </template>
              <div v-else class="empty-hint">{{ t('skills.loadFail') }}</div>
            </div>
          </div>
        </div>
      </section>

      <!-- ═══════════════ MCP 客户端 ═══════════════ -->
      <section v-if="activeTab === 'mcp'" class="tab-content">
        <div class="section-header">
          <h2 class="section-title">{{ t('mcp.title') }}</h2>
          <button class="btn-primary btn-sm" @click="handleReloadMcp" :disabled="mcpLoading">
            {{ t('mcp.refresh') }}
          </button>
        </div>
        <p class="section-desc">{{ t('mcp.desc') }}</p>

        <!-- 已配置 server -->
        <div v-if="mcpServers.length === 0" class="empty-state small">
          <p>{{ t('mcp.empty') }}</p>
        </div>
        <div v-for="srv in mcpServers" :key="srv.name" class="mcp-server-card">
          <div class="mcp-server-head">
            <span class="mcp-dot" :class="{ on: srv.connected, off: !srv.connected }"></span>
            <span class="mcp-name">{{ srv.name }}</span>
            <span class="mcp-type-badge" :class="srv.type">{{ srv.type || 'stdio' }}</span>
            <span class="mcp-cmd" :title="srv.type === 'sse' ? srv.url : `${srv.command} ${(srv.args || []).join(' ')}`">
              {{ srv.type === 'sse' ? srv.url : `${srv.command} ${(srv.args || []).join(' ')}` }}
            </span>
            <span v-if="!srv.enabled" class="mcp-disabled-badge">{{ t('mcp.disabledTag') }}</span>
            <button class="btn-danger btn-xs" @click="handleDeleteMcp(srv.name)">
              {{ t('mcp.remove') }}
            </button>
          </div>
          <p v-if="srv.description" class="mcp-desc">{{ srv.description }}</p>
          <div class="mcp-tools">
            <span v-for="tname in srv.tools" :key="tname" class="mcp-tool-chip">
              {{ tname }}
            </span>
            <span v-if="srv.tools.length === 0" class="mcp-tool-empty">
              {{ t('mcp.noTools') }}
            </span>
          </div>
        </div>

        <!-- 新增 server -->
        <div class="card mcp-add-card">
          <h3 class="mcp-add-title">{{ t('mcp.addTitle') }}</h3>
          <div class="mcp-type-switch">
            <button
              :class="['mcp-type-btn', { active: mcpNew.type === 'stdio' }]"
              @click="mcpNew.type = 'stdio'"
            >{{ t('mcp.typeStdio') }}</button>
            <button
              :class="['mcp-type-btn', { active: mcpNew.type === 'sse' }]"
              @click="mcpNew.type = 'sse'"
            >{{ t('mcp.typeSse') }}</button>
          </div>
          <div class="form-row">
            <label>{{ t('mcp.fieldName') }}</label>
            <input v-model.trim="mcpNew.name" class="form-input" :placeholder="t('mcp.phName')" />
          </div>

          <template v-if="mcpNew.type === 'stdio'">
            <div class="form-row">
              <label>{{ t('mcp.fieldCommand') }}</label>
              <input v-model.trim="mcpNew.command" class="form-input" :placeholder="t('mcp.phCommand')" />
            </div>
            <div class="form-row">
              <label>{{ t('mcp.fieldArgs') }}</label>
              <input v-model.trim="mcpNew.args" class="form-input" :placeholder="t('mcp.phArgs')" />
            </div>
          </template>

          <template v-else>
            <div class="form-row">
              <label>{{ t('mcp.fieldUrl') }}</label>
              <input v-model.trim="mcpNew.url" class="form-input" :placeholder="t('mcp.phUrl')" />
            </div>
            <div class="form-row">
              <label>{{ t('mcp.fieldHeaders') }}</label>
              <input v-model.trim="mcpNew.headers" class="form-input" :placeholder="t('mcp.phHeaders')" />
            </div>
            <div class="form-row">
              <label>{{ t('mcp.fieldDescription') }}</label>
              <input v-model.trim="mcpNew.description" class="form-input" :placeholder="t('mcp.phDescription')" />
            </div>
          </template>

          <button
            class="btn-primary btn-sm"
            @click="handleAddMcp"
            :disabled="mcpLoading || !mcpNew.name || (mcpNew.type === 'stdio' ? !mcpNew.command : !mcpNew.url)"
          >
            {{ t('mcp.add') }}
          </button>
          <p v-if="mcpError" class="mcp-error">{{ mcpError }}</p>
        </div>
      </section>

      <!-- ═══════════════ 工件（大工具输出落盘） ═══════════════ -->
      <section v-if="activeTab === 'artifacts'" class="tab-content">
        <div class="section-header">
          <h2 class="section-title">{{ t('artifacts.title') }}</h2>
          <button class="btn-primary btn-sm" @click="loadArtifacts">{{ t('artifacts.refresh') }}</button>
        </div>
        <p class="section-desc">{{ t('artifacts.desc') }}</p>

        <div v-if="!artifactsAvailable" class="empty-state small">
          <p>{{ t('artifacts.unavailable') }}</p>
        </div>
        <div v-else-if="artifacts.length === 0" class="empty-state small">
          <p>{{ t('artifacts.empty') }}</p>
        </div>
        <div
          v-for="a in artifacts"
          :key="a.id"
          class="card artifact-card"
        >
          <div class="artifact-head">
            <span class="artifact-tool" :title="a.tool_name">{{ a.tool_name || '-' }}</span>
            <span class="artifact-id">{{ a.id }}</span>
            <span class="artifact-meta">{{ a.char_count }} 字 / {{ formatBytes(a.size_bytes) }}</span>
            <span class="artifact-time">{{ formatLocalTime(a.created_at) }}</span>
            <button class="btn-ghost btn-xs" @click="handleViewArtifact(a.id)">
              {{ artifactViewId === a.id ? t('artifacts.hide') : t('artifacts.view') }}
            </button>
            <button class="btn-danger btn-xs" @click="handleDeleteArtifact(a.id)">
              {{ t('artifacts.delete') }}
            </button>
          </div>
          <p class="artifact-summary">{{ a.summary }}</p>
          <pre v-if="artifactViewId === a.id" class="artifact-content">{{ artifactViewContent }}</pre>
        </div>
        <p v-if="artifactError" class="mcp-error">{{ artifactError }}</p>
      </section>

      <!-- ═══════════════ 长期记忆 ═══════════════ -->
      <section v-if="activeTab === 'memory'" class="tab-content">
        <div class="section-header">
          <h2 class="section-title">{{ t('memory.title') }}</h2>
          <div class="memory-search">
            <input
              v-model.trim="memoryQuery"
              class="form-input"
              :placeholder="t('memory.searchPh')"
              @keyup.enter="loadMemories"
            />
            <button class="btn-primary btn-sm" @click="loadMemories">{{ t('memory.search') }}</button>
          </div>
        </div>
        <p class="section-desc">{{ t('memory.desc') }}</p>

        <!-- 自动总结 -->
        <div class="card memory-summary-card">
          <div class="memory-summary-head">
            <div class="memory-summary-info">
              <h3 class="mcp-add-title">{{ t('memory.summaryTitle') }}</h3>
              <p class="memory-summary-meta">
                <span v-if="memorySummaryInfo">
                  {{ memorySummaryInfo.enabled ? t('memory.summaryOn') : t('memory.summaryOff') }}
                  · {{ t('memory.summaryInterval') }} {{ formatInterval(memorySummaryInfo.interval_seconds) }}
                  · {{ t('memory.summaryMaxSessions') }} {{ memorySummaryInfo.max_sessions }}
                </span>
                <span v-else>{{ t('memory.summaryUnknown') }}</span>
              </p>
            </div>
            <button
              class="btn-primary btn-sm"
              :disabled="memorySummarizing || !memorySummaryInfo?.available"
              @click="handleSummarizeNow"
            >
              {{ memorySummarizing ? t('memory.summarizing') : t('memory.summarizeNow') }}
            </button>
          </div>
          <p class="memory-summary-desc">{{ t('memory.summaryDesc') }}</p>
          <p v-if="memorySummaryMsg" class="memory-summary-msg">{{ memorySummaryMsg }}</p>
        </div>

        <div class="card memory-add-card">
          <h3 class="mcp-add-title">{{ t('memory.addTitle') }}</h3>
          <div class="memory-form">
            <input v-model.trim="memoryForm.key" class="form-input" :placeholder="t('memory.phKey')" />
            <input v-model="memoryForm.value" class="form-input memory-value-input" :placeholder="t('memory.phValue')" />
            <input v-model.trim="memoryForm.tags" class="form-input" :placeholder="t('memory.phTags')" />
            <select v-model="memoryForm.scope" class="form-input memory-scope-select">
              <option value="global">{{ t('memory.scopeGlobal') }}</option>
              <option value="session">{{ t('memory.scopeSession') }}</option>
            </select>
            <button class="btn-primary btn-sm" @click="handleSaveMemory">{{ t('memory.add') }}</button>
          </div>
        </div>

        <div v-if="!memoryAvailable" class="empty-state small">
          <p>{{ t('memory.unavailable') }}</p>
        </div>
        <div v-else-if="memories.length === 0" class="empty-state small">
          <p>{{ t('memory.empty') }}</p>
        </div>
        <div v-for="m in memories" :key="m.id" class="card memory-card">
          <div class="memory-head">
            <span class="memory-key">{{ m.key }}</span>
            <span class="memory-scope" :class="m.scope">{{ m.scope }}</span>
            <span v-for="tag in m.tags" :key="tag" class="memory-tag">{{ tag }}</span>
            <span class="artifact-time">{{ formatLocalTime(m.updated_at) }}</span>
            <button
              v-if="memoryEditingId !== m.id"
              class="btn-ghost btn-xs"
              @click="handleStartEditMemory(m)"
            >
              {{ t('memory.edit') }}
            </button>
            <button class="btn-danger btn-xs" @click="handleDeleteMemory(m.id)">
              {{ t('memory.delete') }}
            </button>
          </div>
          <template v-if="memoryEditingId === m.id">
            <input v-model="memoryEdit.value" class="form-input" :placeholder="t('memory.phValue')" />
            <input v-model.trim="memoryEdit.tags" class="form-input" :placeholder="t('memory.phTags')" />
            <div class="memory-edit-actions">
              <button class="btn-primary btn-sm" @click="handleSaveEditMemory(m.id)">{{ t('memory.save') }}</button>
              <button class="btn-ghost btn-sm" @click="memoryEditingId = ''">{{ t('memory.cancel') }}</button>
            </div>
          </template>
          <p v-else class="memory-value">{{ m.value }}</p>
        </div>
        <p v-if="memoryError" class="mcp-error">{{ memoryError }}</p>
      </section>

      <!-- ═══════════════ 运行轨迹（Tracing） ═══════════════ -->
      <section v-if="activeTab === 'tracing'" class="tab-content">
        <div class="section-header">
          <h2 class="section-title">{{ t('tracing.title') }}</h2>
          <button class="btn-primary btn-sm" @click="loadTraces">{{ t('tracing.refresh') }}</button>
        </div>
        <p class="section-desc">{{ t('tracing.desc') }}</p>

        <div v-if="!tracesAvailable" class="empty-state small">
          <p>{{ t('tracing.unavailable') }}</p>
        </div>
        <div v-else-if="traces.length === 0" class="empty-state small">
          <p>{{ t('tracing.empty') }}</p>
        </div>
        <div v-for="tr in traces" :key="tr.trace_id" class="card trace-card">
          <div class="trace-head">
            <span class="trace-status" :class="tr.status"></span>
            <span class="trace-name">{{ tr.name }}</span>
            <span class="artifact-time">{{ formatLocalTime(tr.started_at) }}</span>
            <span class="trace-meta">{{ t('tracing.steps') }} {{ tr.step_count }}</span>
            <span class="trace-meta">span {{ tr.span_count }}</span>
            <span class="trace-meta">{{ formatMs(tr.duration_ms) }}</span>
            <span class="trace-meta" v-if="tr.total_tokens">{{ tr.total_tokens }} tokens</span>
            <span class="trace-meta error" v-if="tr.error_count">{{ tr.error_count }} 错误</span>
            <button class="btn-ghost btn-xs" @click="toggleTrace(tr.trace_id)">
              {{ traceDetailId === tr.trace_id ? t('tracing.collapse') : t('tracing.expand') }}
            </button>
            <button class="btn-danger btn-xs" @click="handleDeleteTrace(tr.trace_id)">
              {{ t('tracing.delete') }}
            </button>
          </div>

          <div v-if="traceDetailId === tr.trace_id" class="trace-lane">
            <div v-if="laneBars.length === 0" class="trace-bar-empty">{{ t('tracing.noSpans') }}</div>
            <div v-for="bar in laneBars" :key="bar.id" class="trace-bar-row">
              <span class="trace-bar-label" :title="bar.name">
                <span class="trace-kind" :class="bar.kind">{{ bar.kind }}</span>
                {{ bar.name }}
              </span>
              <div class="trace-bar-track">
                <div
                  class="trace-bar"
                  :class="[bar.kind, bar.status]"
                  :style="{ left: bar.left + '%', width: bar.width + '%' }"
                  :title="`${bar.name} · ${formatMs(bar.duration_ms)}${bar.error ? ' · ' + bar.error : ''}`"
                ></div>
              </div>
              <span class="trace-bar-ms">{{ formatMs(bar.duration_ms) }}</span>
            </div>
            <div v-for="bar in laneBars" :key="'d-' + bar.id" class="trace-detail">
              <div class="trace-detail-name">{{ bar.name }}</div>
              <div v-if="bar.error" class="trace-detail-error">{{ bar.error }}</div>
              <div v-if="bar.input_preview" class="trace-detail-line">in: {{ bar.input_preview }}</div>
              <div v-if="bar.output_preview" class="trace-detail-line">out: {{ bar.output_preview }}</div>
              <div v-if="bar.total_tokens" class="trace-detail-line">
                tokens: {{ bar.prompt_tokens }} / {{ bar.completion_tokens }} / {{ bar.total_tokens }}
              </div>
            </div>
          </div>
        </div>
        <p v-if="traceError" class="mcp-error">{{ traceError }}</p>
      </section>

      <!-- ═══════════════ 定时任务 ═══════════════ -->
      <section v-if="activeTab === 'schedule'" class="tab-content">
        <div class="section-header">
          <h2 class="section-title">{{ t('schedule.title') }}</h2>
          <div class="sched-head-actions">
            <button class="btn-ghost btn-sm" @click="loadSchedules">{{ t('schedule.refresh') }}</button>
            <button class="btn-primary btn-sm" @click="openScheduleForm()">{{ t('schedule.new') }}</button>
          </div>
        </div>
        <p class="section-desc">{{ t('schedule.desc') }}</p>

        <!-- 新建 / 编辑表单 -->
        <div v-if="showScheduleForm" class="card sched-form-card">
          <h3 class="mcp-add-title">{{ scheduleForm.id ? t('schedule.edit') : t('schedule.new') }}</h3>

          <div class="sched-grid">
            <div class="form-row">
              <label class="form-label">{{ t('schedule.fName') }}</label>
              <input v-model.trim="scheduleForm.name" class="form-input" :placeholder="t('schedule.phName')" />
            </div>
            <div class="form-row">
              <label class="form-label">{{ t('schedule.fDesc') }}</label>
              <input v-model.trim="scheduleForm.description" class="form-input" :placeholder="t('schedule.phDesc')" />
            </div>
          </div>

          <!-- 调度策略 -->
          <div class="form-row">
            <label class="form-label">{{ t('schedule.fSchedule') }}</label>
            <div class="sched-type-switch">
              <button
                v-for="st in [
                  { v: 'daily', k: 'schedule.typeDaily' },
                  { v: 'weekly', k: 'schedule.typeWeekly' },
                  { v: 'interval', k: 'schedule.typeInterval' },
                  { v: 'once', k: 'schedule.typeOnce' },
                ]"
                :key="st.v"
                :class="['sched-type-btn', { active: scheduleForm.type === st.v }]"
                @click="scheduleForm.type = st.v"
              >{{ t(st.k) }}</button>
            </div>

            <div class="sched-spec-row">
              <template v-if="scheduleForm.type === 'weekly'">
                <span class="sched-inline-label">{{ t('schedule.weekdays') }}</span>
                <button
                  v-for="d in WEEKDAYS"
                  :key="d.value"
                  :class="['weekday-chip', { active: scheduleForm.weekdays.includes(d.value) }]"
                  @click="toggleWeekday(d.value)"
                >{{ d.label }}</button>
              </template>

              <template v-if="scheduleForm.type === 'daily' || scheduleForm.type === 'weekly'">
                <span class="sched-inline-label">{{ t('schedule.atTime') }}</span>
                <input v-model="scheduleForm.time" type="time" class="form-input sched-time-input" />
              </template>

              <template v-if="scheduleForm.type === 'interval'">
                <span class="sched-inline-label">{{ t('schedule.every') }}</span>
                <input v-model.number="scheduleForm.intervalValue" type="number" min="1" class="form-input sched-num-input" />
                <select v-model="scheduleForm.intervalUnit" class="form-input sched-unit-select">
                  <option value="minutes">{{ t('schedule.unitMinutes') }}</option>
                  <option value="hours">{{ t('schedule.unitHours') }}</option>
                </select>
              </template>

              <template v-if="scheduleForm.type === 'once'">
                <span class="sched-inline-label">{{ t('schedule.runAt') }}</span>
                <input v-model="scheduleForm.runAt" type="datetime-local" class="form-input sched-time-input" />
              </template>
            </div>
          </div>

          <div class="form-row">
            <label class="form-label">{{ t('schedule.fPrompt') }}</label>
            <textarea v-model="scheduleForm.prompt" rows="4" class="form-input" :placeholder="t('schedule.phPrompt')"></textarea>
          </div>

          <!-- MCP 多选 -->
          <div class="form-row">
            <label class="form-label">{{ t('schedule.fMcp') }}</label>
            <div v-if="scheduleOptions.mcp_servers.length === 0" class="pick-empty">{{ t('schedule.noMcp') }}</div>
            <div v-else class="pick-grid">
              <button
                v-for="m in scheduleOptions.mcp_servers"
                :key="m.name"
                :class="['pick-chip', { active: scheduleForm.mcpServers.includes(m.name) }]"
                @click="togglePick('mcpServers', m.name)"
              >
                <span class="pick-name">{{ m.name }}</span>
                <span class="pick-sub">{{ m.tool_count }} 工具 · {{ m.connected ? t('schedule.connected') : t('schedule.disconnected') }}</span>
              </button>
            </div>
          </div>

          <!-- Skills 多选 -->
          <div class="form-row">
            <label class="form-label">{{ t('schedule.fSkills') }}</label>
            <div v-if="scheduleOptions.skills.length === 0" class="pick-empty">{{ t('schedule.noSkills') }}</div>
            <div v-else class="pick-grid">
              <button
                v-for="s in scheduleOptions.skills"
                :key="s.name"
                :class="['pick-chip', { active: scheduleForm.skills.includes(s.name) }]"
                @click="togglePick('skills', s.name)"
                :title="s.description"
              >
                <span class="pick-name">{{ s.name }}</span>
                <span class="pick-sub">{{ s.source }}<span v-if="!s.enabled"> · {{ t('schedule.disabled') }}</span></span>
              </button>
            </div>
          </div>

          <!-- 其他工具多选 -->
          <div class="form-row">
            <label class="form-label">{{ t('schedule.fTools') }} <span class="form-hint-inline">{{ t('schedule.fToolsHint') }}</span></label>
            <div class="pick-grid">
              <button
                v-for="tl in scheduleOptions.tools"
                :key="tl.name"
                :class="['pick-chip', { active: scheduleForm.tools.includes(tl.name), danger: tl.risk === 'dangerous' }]"
                @click="togglePick('tools', tl.name)"
                :title="tl.description"
              >
                <span class="pick-name">{{ tl.name }}</span>
                <span class="pick-sub">{{ tl.risk }}</span>
              </button>
            </div>
          </div>

          <!-- 模型与运行参数 -->
          <div class="sched-grid sched-grid-3">
            <div class="form-row">
              <label class="form-label">{{ t('schedule.fProvider') }}</label>
              <select v-model="scheduleForm.providerId" class="form-input">
                <option value="">{{ t('schedule.providerAuto') }}</option>
                <option v-for="p in scheduleOptions.providers" :key="p.id" :value="p.id">
                  {{ p.name }}{{ p.ready ? '' : '（未配置 Key）' }}
                </option>
              </select>
            </div>
            <div class="form-row">
              <label class="form-label">{{ t('schedule.fModel') }}</label>
              <select v-model="scheduleForm.model" class="form-input">
                <option value="">{{ t('schedule.modelAuto') }}</option>
                <option v-for="m in modelChoices(scheduleForm.providerId)" :key="m" :value="m">{{ m }}</option>
                <option v-for="m in scheduleOptions.providers.flatMap(p => scheduleForm.providerId ? [] : p.models)" :key="'a-' + m" :value="m">{{ m }}</option>
              </select>
            </div>
            <div class="form-row">
              <label class="form-label">{{ t('schedule.fLimits') }}</label>
              <div class="sched-limits">
                <input v-model.number="scheduleForm.maxIterations" type="number" min="1" max="20" class="form-input sched-num-input" :title="t('schedule.fMaxIter')" />
                <span class="sched-inline-label">{{ t('schedule.maxIterShort') }}</span>
                <input v-model.number="scheduleForm.timeoutSeconds" type="number" min="30" class="form-input sched-num-input" :title="t('schedule.fTimeout')" />
                <span class="sched-inline-label">{{ t('schedule.timeoutShort') }}</span>
              </div>
            </div>
          </div>

          <div class="sched-form-footer">
            <label class="sched-enable">
              <input v-model="scheduleForm.enabled" type="checkbox" />
              <span>{{ t('schedule.fEnabled') }}</span>
            </label>
            <div class="sched-form-buttons">
              <button class="btn-ghost btn-sm" @click="showScheduleForm = false">{{ t('schedule.cancel') }}</button>
              <button class="btn-primary btn-sm" :disabled="scheduleSaving" @click="saveSchedule">
                {{ scheduleSaving ? t('schedule.saving') : t('schedule.save') }}
              </button>
            </div>
          </div>
          <p v-if="scheduleError" class="mcp-error">{{ scheduleError }}</p>
        </div>

        <!-- 任务列表 -->
        <div v-if="!scheduleAvailable" class="empty-state small">
          <p>{{ t('schedule.unavailable') }}</p>
        </div>
        <div v-else-if="scheduleTasks.length === 0 && !showScheduleForm" class="empty-state small">
          <p>{{ t('schedule.empty') }}</p>
        </div>

        <div v-for="task in scheduleTasks" :key="task.id" class="card sched-card">
          <div class="sched-card-head">
            <span class="sched-dot" :class="{ on: task.enabled, off: !task.enabled }"></span>
            <span class="sched-name">{{ task.name }}</span>
            <span class="sched-badge">{{ task.schedule_desc }}</span>
            <span v-if="task.next_run_at" class="sched-meta">{{ t('schedule.next') }} {{ formatLocalTime(task.next_run_at) }}</span>
            <span v-else class="sched-meta">{{ t('schedule.noNext') }}</span>
            <span
              v-if="task.last_status"
              class="sched-status"
              :class="task.last_status"
            >{{ t('schedule.lastStatus') }} {{ t('schedule.status.' + task.last_status) }}</span>
            <span class="sched-meta">×{{ task.run_count }}</span>
          </div>
          <p v-if="task.description" class="sched-desc">{{ task.description }}</p>
          <p class="sched-prompt">{{ task.prompt }}</p>
          <div class="sched-tags">
            <span v-for="m in task.mcp_servers" :key="'m-' + m" class="sched-tag mcp">MCP · {{ m }}</span>
            <span v-for="s in task.skills" :key="'s-' + s" class="sched-tag skill">Skill · {{ s }}</span>
            <span v-for="tl in task.tools" :key="'t-' + tl" class="sched-tag tool">Tool · {{ tl }}</span>
            <span v-if="!task.mcp_servers.length && !task.skills.length && !task.tools.length" class="sched-tag none">
              {{ t('schedule.noToolsTag') }}
            </span>
          </div>
          <p v-if="task.last_error" class="sched-error">{{ task.last_error }}</p>

          <div class="sched-actions">
            <button class="btn-ghost btn-xs" @click="toggleSchedule(task)">
              {{ task.enabled ? t('schedule.disable') : t('schedule.enable') }}
            </button>
            <button class="btn-primary btn-xs" :disabled="scheduleRunning === task.id" @click="runScheduleNow(task)">
              {{ scheduleRunning === task.id ? t('schedule.running') : t('schedule.runNow') }}
            </button>
            <button class="btn-ghost btn-xs" @click="openScheduleForm(task)">{{ t('schedule.edit') }}</button>
            <button class="btn-ghost btn-xs" @click="toggleRuns(task)">
              {{ scheduleRuns[task.id] ? t('schedule.hideRuns') : t('schedule.showRuns') }}
            </button>
            <button class="btn-danger btn-xs" @click="deleteSchedule(task)">{{ t('schedule.delete') }}</button>
          </div>

          <div v-if="scheduleRuns[task.id]" class="sched-runs">
            <div v-if="scheduleRuns[task.id].length === 0" class="pick-empty">{{ t('schedule.noRuns') }}</div>
            <div v-for="r in scheduleRuns[task.id]" :key="r.id" class="sched-run">
              <span class="sched-run-dot" :class="r.status"></span>
              <span class="sched-run-time">{{ formatLocalTime(r.started_at) }}</span>
              <span class="sched-run-status">{{ t('schedule.status.' + r.status) }}</span>
              <span class="sched-run-meta">{{ formatMs(r.duration_ms) }} · {{ r.trigger === 'manual' ? t('schedule.triggerManual') : t('schedule.triggerSchedule') }}</span>
              <p v-if="r.summary" class="sched-run-summary">{{ r.summary }}</p>
              <p v-if="r.error" class="sched-run-error">{{ r.error }}</p>
            </div>
          </div>
        </div>
      </section>

      <!-- ═══════════════ 通用设置 ═══════════════ -->
      <section v-if="activeTab === 'general'" class="tab-content">
        <h2 class="section-title">{{ t('general.title') }}</h2>
        <div class="card general-card">
          <div class="form-row">
            <label class="form-label">{{ t('general.theme') }}</label>
            <div class="theme-grid">
              <button
                v-for="tm in THEME_LIST"
                :key="tm.id"
                :class="['theme-card', { active: settingsStore.theme === tm.id }]"
                @click="settingsStore.setTheme(tm.id as Theme)"
              >
                <span class="theme-swatch" :style="{ background: tm.color }"></span>
                <span class="theme-name">{{ tm.name }}</span>
                <svg v-if="settingsStore.theme === tm.id" class="theme-check" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
                  <polyline points="20 6 9 17 4 12" />
                </svg>
              </button>
            </div>
          </div>
          <div class="form-row">
            <label class="form-label">{{ t('general.language') }}</label>
            <div class="pill-group">
              <button
                :class="['pill-btn', { active: settingsStore.language === 'zh' }]"
                @click="settingsStore.setLanguage('zh')"
              >中文</button>
              <button
                :class="['pill-btn', { active: settingsStore.language === 'en' }]"
                @click="settingsStore.setLanguage('en')"
              >English</button>
            </div>
          </div>
        </div>

        <!-- 工具权限与人工确认 -->
        <h2 class="section-title" style="margin-top: var(--space-lg);">
          {{ t('permissions.title') }}
        </h2>
        <div class="card general-card">
          <div class="form-row">
            <label class="form-label">{{ t('permissions.mode') }}</label>
            <select
              class="form-input"
              :value="permissionStore.state.mode ?? ''"
              :disabled="!permissionStore.state.available"
              @change="handleChangePermissionMode"
            >
              <option v-for="m in permissionStore.state.modes" :key="m" :value="m">
                {{ t('permissions.mode.' + m) }}
              </option>
            </select>
          </div>
          <p class="permissions-hint">{{ t('permissions.hint') }}</p>

          <div v-if="permissionStore.state.tools.length" class="permission-tools">
            <div v-for="tp in permissionStore.state.tools" :key="tp.name" class="permission-tool-row">
              <div class="permission-tool-info">
                <code class="permission-tool-name">{{ tp.name }}</code>
                <span :class="['risk-badge', 'risk-' + tp.risk]">{{ t('permissions.risk.' + tp.risk) }}</span>
                <span class="permission-tool-action">{{ t('permissions.action.' + tp.action) }}</span>
              </div>
              <select
                class="form-input permission-tool-select"
                :value="permissionStore.state.overrides[tp.name] ?? ''"
                @change="handleChangeToolOverride(tp.name, $event)"
              >
                <option value="">{{ t('permissions.followGlobal') }}</option>
                <option value="auto">{{ t('permissions.action.auto') }}</option>
                <option value="confirm">{{ t('permissions.action.confirm') }}</option>
                <option value="deny">{{ t('permissions.action.deny') }}</option>
              </select>
            </div>
          </div>
          <div v-else class="empty-hint">{{ t('permissions.empty') }}</div>
        </div>
      </section>
    </div>

    <!-- ═══════════════ 插件配置弹窗 ═══════════════ -->
    <Transition name="modal-fade">
      <div v-if="selectedPluginForConfig && selectedPlugin" class="modal-overlay" @click.self="handleCancelPluginConfig">
        <div class="modal-dialog">
          <div class="modal-header">
            <h3 class="modal-title">{{ t('plugins.config') }} — {{ selectedPlugin.name }}</h3>
            <button class="modal-close" @click="handleCancelPluginConfig">
              <svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" /></svg>
            </button>
          </div>
          <div class="modal-body">
            <div v-if="configLoading" class="modal-loading">{{ t('plugins.config') }}…</div>
            <template v-else>
              <div v-if="getConfigSchemaProperties(selectedPlugin).length === 0" class="empty-hint" style="border:none;padding:var(--space-md) 0;">
                该插件无可配置项
              </div>
              <div v-else class="form-grid">
                <div v-for="prop in getConfigSchemaProperties(selectedPlugin)" :key="prop.key" class="form-row">
                  <label class="form-label">{{ prop.key }}{{ prop.description ? ` (${prop.description})` : '' }}</label>
                  <input v-if="prop.type === 'string'" v-model="pluginConfigForm[prop.key as string]" class="form-input" :placeholder="String(prop.default ?? '')" />
                  <input v-else-if="prop.type === 'number'" type="number" class="form-input" v-model.number="pluginConfigForm[prop.key as string]" :placeholder="String(prop.default ?? '')" />
                  <input v-else-if="prop.type === 'boolean'" type="checkbox" v-model="pluginConfigForm[prop.key as string]" />
                  <input v-else v-model="pluginConfigForm[prop.key as string]" class="form-input" :placeholder="String(prop.default ?? '')" />
                </div>
              </div>
            </template>
          </div>
          <div class="modal-footer">
            <button class="btn-ghost" @click="handleCancelPluginConfig">{{ t('plugins.cancelConfig') }}</button>
            <button class="btn-primary" @click="handleSavePluginConfig(selectedPlugin.id)" :disabled="configLoading">{{ t('plugins.saveConfig') }}</button>
          </div>
        </div>
      </div>
    </Transition>

    <!-- ═══════════════ 插件市场说明弹窗 ═══════════════ -->
    <Transition name="modal-fade">
      <div v-if="marketplaceDescribeTarget" class="modal-overlay" @click.self="marketplaceDescribeTarget = null">
        <div class="modal-dialog">
          <div class="modal-header">
            <h3 class="modal-title">{{ marketplaceDescribeTarget.name }} <span class="badge-pill badge-version">v{{ marketplaceDescribeTarget.version }}</span></h3>
            <button class="modal-close" @click="marketplaceDescribeTarget = null">
              <svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" /></svg>
            </button>
          </div>
          <div class="modal-body">
            <p class="marketplace-desc" style="margin-bottom:var(--space-md);">{{ marketplaceDescribeTarget.description }}</p>
            <div class="marketplace-long-desc">{{ marketplaceDescribeTarget.long_description }}</div>
          </div>
          <div class="modal-footer">
            <button class="btn-ghost" @click="marketplaceDescribeTarget = null">关闭</button>
            <button
              v-if="!marketplaceDescribeTarget.installed"
              class="btn-primary"
              @click="handleMarketplaceInstall(marketplaceDescribeTarget)"
            >
              安装
            </button>
          </div>
        </div>
      </div>
    </Transition>
  </div>
</template>

<style scoped>
/* ── Layout ────────────────────────────────────────────── */
.settings-view {
  display: flex;
  flex-direction: column;
  max-width: 860px;
  height: 100vh;
  box-sizing: border-box;
  margin: 0 auto;
  padding: var(--space-lg);
  overflow: hidden;
  animation: slideUp 0.3s ease;
}

/* ── Header ─────────────────────────────────────────────── */
.settings-header {
  display: flex;
  align-items: center;
  gap: var(--space-md);
  margin-bottom: var(--space-lg);
  flex-shrink: 0;
}

.back-link {
  display: inline-flex;
  align-items: center;
  gap: var(--space-xs);
  padding: var(--space-sm) var(--space-md);
  border-radius: var(--radius-md);
  color: var(--color-text-secondary);
  font-size: var(--font-size-sm);
  font-weight: 500;
  transition: var(--transition-base);
}

.back-link:hover {
  background: var(--bg-hover);
  color: var(--color-primary);
}

.back-link .icon {
  width: 18px;
  height: 18px;
}

.settings-title {
  font-size: var(--font-size-2xl);
  font-weight: 700;
  color: var(--color-text);
  letter-spacing: -0.02em;
}

/* ── Tab Navigation ─────────────────────────────────────── */
.tab-nav {
  display: flex;
  align-items: center;
  gap: var(--space-xs);
  background: var(--bg-surface);
  padding: var(--space-xs);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-sm);
  margin-bottom: var(--space-lg);
  flex-shrink: 0;
}

.tab-nav-scroll {
  display: flex;
  gap: var(--space-xs);
  flex: 1 1 auto;
  min-width: 0;
  overflow-x: auto;
  scrollbar-width: none;
  -ms-overflow-style: none;
  -webkit-overflow-scrolling: touch;
}

.tab-nav-scroll::-webkit-scrollbar {
  display: none;
}

.tab-scroll-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  width: 30px;
  height: 30px;
  border: none;
  border-radius: var(--radius-md);
  background: var(--bg-hover);
  color: var(--color-text-secondary);
  cursor: pointer;
  transition: var(--transition-base);
}

.tab-scroll-btn:hover:not(.disabled) {
  background: var(--color-primary-light);
  color: var(--color-primary);
}

.tab-scroll-btn.disabled {
  opacity: 0.3;
  cursor: default;
}

.tab-scroll-btn .icon {
  width: 18px;
  height: 18px;
}

.tab-btn {
  position: relative;
  padding: var(--space-sm) var(--space-lg);
  border-radius: var(--radius-md);
  font-size: var(--font-size-sm);
  font-weight: 500;
  color: var(--color-text-secondary);
  transition: var(--transition-base);
  white-space: nowrap;
  z-index: 1;
}

.tab-btn::after {
  content: '';
  position: absolute;
  bottom: 0;
  left: 50%;
  width: 0;
  height: 3px;
  background: var(--color-primary);
  border-radius: var(--radius-full);
  transition: var(--transition-base);
  transform: translateX(-50%);
}

.tab-btn:hover {
  color: var(--color-text);
  background: var(--bg-hover);
}

.tab-btn.active {
  color: var(--color-primary);
  background: var(--color-primary-light);
  font-weight: 600;
}

.tab-btn.active::after {
  width: 60%;
}

/* ── Scrollable Body ──────────────────────────────────── */
/* 根布局 #app 固定为 100vh，故此处让内容区独立纵向滚动，
   页头与标签栏常驻顶部，避免内容超高被裁、需缩小窗口才看得到底部。 */
.settings-body {
  flex: 1 1 auto;
  min-height: 0;
  overflow-y: auto;
  overflow-x: hidden;
  padding-right: var(--space-xs);
}

.settings-body::-webkit-scrollbar {
  width: 8px;
}

.settings-body::-webkit-scrollbar-track {
  background: var(--bg-main);
  border-radius: var(--radius-sm);
}

.settings-body::-webkit-scrollbar-thumb {
  background: var(--border-strong);
  border-radius: var(--radius-sm);
}

.settings-body::-webkit-scrollbar-thumb:hover {
  background: var(--color-primary);
}

/* ── Tab Content ────────────────────────────────────────── */
.tab-content {
  animation: slideUp 0.25s ease;
}

/* ── Section Header ────────────────────────────────────── */
.section-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: var(--space-lg);
}

.section-title {
  font-size: var(--font-size-xl);
  font-weight: 700;
  color: var(--color-text);
}

/* ── Card Base ──────────────────────────────────────────── */
.card {
  background: var(--bg-surface);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-sm);
  border: 1px solid var(--border-light);
}

/* ── Provider / Plugin Lists ───────────────────────────── */
.provider-list,
.plugin-list {
  display: flex;
  flex-direction: column;
  gap: var(--space-md);
  max-height: 60vh;
  overflow-y: auto;
  padding-right: var(--space-xs);
}

/* 自定义滚动条 */
.provider-list::-webkit-scrollbar,
.plugin-list::-webkit-scrollbar {
  width: 6px;
}

.provider-list::-webkit-scrollbar-track,
.plugin-list::-webkit-scrollbar-track {
  background: var(--bg-main);
  border-radius: var(--radius-sm);
}

.provider-list::-webkit-scrollbar-thumb,
.plugin-list::-webkit-scrollbar-thumb {
  background: var(--border-strong);
  border-radius: var(--radius-sm);
}

.provider-list::-webkit-scrollbar-thumb:hover,
.plugin-list::-webkit-scrollbar-thumb:hover {
  background: var(--color-primary);
}

/* ── Provider Cards ────────────────────────────────────── */
.provider-card {
  padding: var(--space-lg);
  transition: var(--transition-base);
}

.provider-card:hover {
  box-shadow: var(--shadow-md);
  transform: translateY(-2px);
}

.provider-row,
.plugin-row {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: var(--space-md);
}

.provider-info,
.plugin-info {
  flex: 1;
  min-width: 0;
}

.provider-name,
.plugin-name {
  font-size: var(--font-size-md);
  font-weight: 600;
  color: var(--color-text);
  margin-bottom: var(--space-xs);
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: var(--space-xs);
}

.provider-url {
  font-family: var(--font-mono);
  font-size: var(--font-size-sm);
  color: var(--color-text-secondary);
  margin-bottom: var(--space-sm);
  word-break: break-all;
}

.provider-models {
  display: flex;
  gap: var(--space-xs);
  flex-wrap: wrap;
}

/* ── Badge Pills ───────────────────────────────────────── */
.badge-pill {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 2px var(--space-sm);
  border-radius: var(--radius-full);
  font-size: var(--font-size-xs);
  font-weight: 600;
  line-height: 1.5;
}

.badge-ok {
  background: var(--color-success-light);
  color: var(--color-success);
}

.badge-warn {
  background: var(--color-warning-light);
  color: var(--color-warning);
}

.badge-enabled {
  background: var(--bg-success);
  color: var(--color-success);
}

.badge-disabled {
  background: var(--bg-disabled);
  color: var(--color-text-secondary);
}

.badge-core {
  background: var(--color-core-light);
  color: var(--color-core);
}

.badge-version {
  background: var(--bg-tag);
  color: var(--color-tag);
}

.badge-type {
  background: var(--bg-hover);
  color: var(--color-text-secondary);
}

.icon-sm {
  width: 14px;
  height: 14px;
  flex-shrink: 0;
}

/* ── Model Tags ────────────────────────────────────────── */
.model-tag {
  background: var(--bg-tag);
  color: var(--color-tag);
  padding: 3px var(--space-sm);
  border-radius: var(--radius-full);
  font-size: var(--font-size-xs);
  font-weight: 500;
}

/* ── Provider / Plugin Actions ─────────────────────────── */
.provider-actions,
.plugin-actions {
  display: flex;
  align-items: center;
  gap: var(--space-sm);
  flex-shrink: 0;
  flex-wrap: wrap;
}

/* ── Ghost Buttons ──────────────────────────────────────── */
.btn-ghost {
  display: inline-flex;
  align-items: center;
  gap: var(--space-xs);
  padding: var(--space-sm) var(--space-md);
  border-radius: var(--radius-md);
  border: 1px solid var(--border-color);
  background: var(--bg-surface);
  color: var(--color-text-secondary);
  font-size: var(--font-size-sm);
  font-weight: 500;
  transition: var(--transition-base);
}

.btn-ghost:hover {
  background: var(--bg-hover);
  color: var(--color-text);
  border-color: var(--border-strong);
}

.btn-ghost:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}

.btn-ghost.btn-danger:hover {
  background: var(--color-danger-light);
  color: var(--color-danger);
  border-color: var(--color-danger);
}

.btn-ghost .icon {
  width: 16px;
  height: 16px;
  flex-shrink: 0;
}

/* ── Primary Button ─────────────────────────────────────── */
.btn-primary {
  display: inline-flex;
  align-items: center;
  gap: var(--space-xs);
  padding: var(--space-sm) var(--space-lg);
  border-radius: var(--radius-md);
  background: var(--color-primary);
  color: white;
  font-size: var(--font-size-sm);
  font-weight: 600;
  transition: var(--transition-base);
  box-shadow: var(--shadow-primary);
}

.btn-primary:hover {
  background: var(--color-primary-hover);
  transform: translateY(-1px);
}

.btn-primary:active {
  transform: translateY(0);
}

.btn-primary .icon {
  width: 16px;
  height: 16px;
}

/* ── Test Result ───────────────────────────────────────── */
.test-result {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: var(--font-size-xs);
  font-weight: 600;
}

.test-ok {
  color: var(--color-success);
}

.test-fail {
  color: var(--color-danger);
}

/* ── Loading Spinner ───────────────────────────────────── */
.spin {
  animation: spin 0.8s linear infinite;
}

@keyframes spin {
  to {
    transform: rotate(360deg);
  }
}

/* ── Add / Edit Form ────────────────────────────────────── */
.add-form,
.edit-form {
  padding: var(--space-lg);
  margin-bottom: var(--space-lg);
}

.form-card-title {
  font-size: var(--font-size-md);
  font-weight: 600;
  margin-bottom: var(--space-md);
  color: var(--color-text);
}

.form-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: var(--space-md);
}

.form-row {
  margin-bottom: var(--space-md);
}

.form-label {
  display: block;
  font-size: var(--font-size-sm);
  font-weight: 500;
  color: var(--color-text-secondary);
  margin-bottom: var(--space-xs);
}

.form-input {
  width: 100%;
  padding: var(--space-sm) var(--space-md);
  border: 1px solid var(--border-color);
  border-radius: var(--radius-md);
  font-size: var(--font-size-sm);
  font-family: inherit;
  background: var(--bg-input);
  color: var(--color-text);
  transition: var(--transition-fast);
}

.form-input:focus {
  outline: none;
  border-color: var(--color-primary);
  box-shadow: 0 0 0 3px var(--color-primary-light);
}

.form-input::placeholder {
  color: var(--color-text-tertiary);
}

.mono-input {
  font-family: var(--font-mono);
  font-size: var(--font-size-sm);
  resize: vertical;
}

.edit-actions {
  display: flex;
  gap: var(--space-sm);
}

.edit-test-error {
  display: flex;
  align-items: center;
  gap: var(--space-xs);
  margin-top: var(--space-sm);
  padding: var(--space-sm) var(--space-md);
  border-radius: var(--radius-md);
  background: var(--color-danger-light);
  color: var(--color-danger);
  font-size: var(--font-size-sm);
  font-weight: 500;
}

/* ── Session Settings ──────────────────────────────────── */
.session-card {
  padding: var(--space-lg);
}

.temp-value {
  font-weight: 700;
  color: var(--color-primary);
}

.temp-slider {
  -webkit-appearance: none;
  appearance: none;
  width: 100%;
  height: 6px;
  border-radius: var(--radius-full);
  background: var(--bg-slider);
  outline: none;
  cursor: pointer;
}

.temp-slider::-webkit-slider-thumb {
  -webkit-appearance: none;
  appearance: none;
  width: 20px;
  height: 20px;
  border-radius: 50%;
  background: var(--color-primary);
  cursor: pointer;
  box-shadow: var(--shadow-sm);
  transition: var(--transition-base);
  border: 3px solid var(--bg-surface);
}

.temp-slider::-webkit-slider-thumb:hover {
  transform: scale(1.2);
  box-shadow: var(--shadow-primary);
}

.temp-slider::-moz-range-thumb {
  width: 20px;
  height: 20px;
  border-radius: 50%;
  background: var(--color-primary);
  cursor: pointer;
  box-shadow: var(--shadow-sm);
  border: 3px solid var(--bg-surface);
}

/* ── Plugin Cards ──────────────────────────────────────── */
.plugin-card {
  padding: var(--space-lg);
  transition: var(--transition-base);
}

.plugin-card:hover {
  box-shadow: var(--shadow-md);
  transform: translateY(-2px);
}

.plugin-meta {
  display: flex;
  gap: var(--space-xs);
  margin-bottom: var(--space-sm);
}

.plugin-permissions {
  display: flex;
  align-items: center;
  gap: var(--space-xs);
  flex-wrap: wrap;
  margin-top: var(--space-xs);
}

.perm-label {
  font-size: var(--font-size-xs);
  color: var(--color-text-secondary);
  font-weight: 500;
}

.perm-tag {
  background: var(--bg-tag);
  color: var(--color-tag);
  padding: 2px var(--space-sm);
  border-radius: var(--radius-sm);
  font-size: var(--font-size-xs);
  font-weight: 500;
}

.core-lock {
  color: var(--color-core);
  cursor: help;
  display: inline-flex;
  align-items: center;
}

/* ── Toggle Switch ─────────────────────────────────────── */
.switch {
  position: relative;
  display: inline-block;
  width: 44px;
  height: 24px;
  flex-shrink: 0;
}

.switch input {
  opacity: 0;
  width: 0;
  height: 0;
}

.slider {
  position: absolute;
  cursor: pointer;
  inset: 0;
  background: var(--bg-slider);
  transition: var(--transition-base);
  border-radius: var(--radius-full);
}

.slider::before {
  position: absolute;
  content: '';
  height: 18px;
  width: 18px;
  left: 3px;
  bottom: 3px;
  background: white;
  transition: var(--transition-bounce);
  border-radius: 50%;
  box-shadow: var(--shadow-sm);
}

.switch input:checked + .slider {
  background: var(--color-primary);
}

.switch input:checked + .slider::before {
  transform: translateX(20px);
}

.switch input:disabled + .slider {
  opacity: 0.5;
  cursor: not-allowed;
}

/* ── Plugin Config Form ────────────────────────────────── */
.plugin-config-form {
  border-top: 1px solid var(--border-color);
  padding-top: var(--space-md);
  margin-top: var(--space-md);
}

.config-title {
  font-size: var(--font-size-sm);
  font-weight: 600;
  margin-bottom: var(--space-md);
  color: var(--color-text);
}

/* ── Modal Dialog ──────────────────────────────────────── */
.modal-overlay {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.45);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 1000;
  padding: var(--space-lg);
}

.modal-dialog {
  background: var(--bg-surface);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-md);
  width: 100%;
  max-width: 560px;
  max-height: 80vh;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.modal-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: var(--space-lg);
  border-bottom: 1px solid var(--border-light);
}

.modal-title {
  font-size: var(--font-size-md);
  font-weight: 600;
  color: var(--color-text);
  margin: 0;
}

.modal-close {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 32px;
  height: 32px;
  border-radius: var(--radius-md);
  border: none;
  background: transparent;
  color: var(--color-text-secondary);
  cursor: pointer;
  transition: var(--transition-base);
}

.modal-close:hover {
  background: var(--bg-hover);
  color: var(--color-text);
}

.modal-close .icon {
  width: 18px;
  height: 18px;
}

.modal-body {
  padding: var(--space-lg);
  overflow-y: auto;
  flex: 1;
}

.modal-loading {
  text-align: center;
  padding: var(--space-lg);
  color: var(--color-text-secondary);
}

.modal-footer {
  display: flex;
  justify-content: flex-end;
  gap: var(--space-sm);
  padding: var(--space-lg);
  border-top: 1px solid var(--border-light);
}

/* Modal transition */
.modal-fade-enter-active,
.modal-fade-leave-active {
  transition: opacity 0.2s ease;
}

.modal-fade-enter-active .modal-dialog,
.modal-fade-leave-active .modal-dialog {
  transition: transform 0.2s ease;
}

.modal-fade-enter-from,
.modal-fade-leave-to {
  opacity: 0;
}

.modal-fade-enter-from .modal-dialog,
.modal-fade-leave-to .modal-dialog {
  transform: scale(0.95) translateY(10px);
}

/* ── General Settings ──────────────────────────────────── */
.general-card {
  padding: var(--space-lg);
}

.pill-group {
  display: flex;
  gap: var(--space-sm);
}

/* ── Theme Grid ────────────────────────────────────────── */
.theme-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(120px, 1fr));
  gap: var(--space-sm);
}

.theme-card {
  position: relative;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: var(--space-xs);
  padding: var(--space-md) var(--space-sm);
  border-radius: var(--radius-md);
  border: 2px solid var(--border-color);
  background: var(--bg-input);
  color: var(--color-text-secondary);
  font-size: var(--font-size-sm);
  font-weight: 500;
  cursor: pointer;
  transition: var(--transition-base);
}

.theme-card:hover {
  border-color: var(--border-strong);
  color: var(--color-text);
  transform: translateY(-2px);
}

.theme-card.active {
  border-color: var(--color-primary);
  color: var(--color-primary);
  background: var(--bg-active);
  box-shadow: var(--shadow-primary);
}

.theme-swatch {
  width: 40px;
  height: 40px;
  border-radius: var(--radius-full);
  box-shadow: 0 0 12px currentColor;
  flex-shrink: 0;
}

.theme-name {
  font-size: var(--font-size-xs);
}

.theme-check {
  position: absolute;
  top: 6px;
  right: 6px;
  width: 14px;
  height: 14px;
  color: var(--color-primary);
}

.pill-btn {
  display: inline-flex;
  align-items: center;
  gap: var(--space-xs);
  padding: var(--space-md) var(--space-lg);
  border-radius: var(--radius-full);
  border: 2px solid var(--border-color);
  background: var(--bg-surface);
  color: var(--color-text-secondary);
  font-size: var(--font-size-sm);
  font-weight: 600;
  transition: var(--transition-base);
}

.pill-btn:hover {
  border-color: var(--color-primary);
  color: var(--color-text);
}

.pill-btn.active {
  background: var(--color-primary);
  border-color: var(--color-primary);
  color: white;
  box-shadow: var(--shadow-primary);
}

.pill-btn .icon {
  width: 18px;
  height: 18px;
}

/* ── Empty Hint ────────────────────────────────────────── */
.empty-hint {
  padding: var(--space-xl) var(--space-md);
  text-align: center;
  color: var(--color-text-tertiary);
  font-size: var(--font-size-sm);
  background: var(--bg-surface);
  border-radius: var(--radius-lg);
  border: 1px dashed var(--border-color);
}

/* ── Responsive ─────────────────────────────────────────── */
@media (max-width: 640px) {
  .settings-view {
    padding: var(--space-md);
  }

  .form-grid {
    grid-template-columns: 1fr;
  }

  .provider-row,
  .plugin-row {
    flex-direction: column;
  }

  .provider-actions,
  .plugin-actions {
    width: 100%;
  }

  .pill-group {
    flex-wrap: wrap;
  }
}

/* ── 插件安装 ── */
.code-textarea {
  font-family: var(--font-mono, 'JetBrains Mono', Consolas, monospace);
  font-size: var(--font-size-sm, 0.875rem);
  line-height: 1.6;
  resize: vertical;
  min-height: 200px;
}

.btn-uninstall {
  color: var(--color-danger, #ef4444);
}

.btn-uninstall:hover {
  background: var(--color-error-bg, rgba(239, 68, 68, 0.08));
}

.badge-enabled {
  background: var(--color-success-light, rgba(34, 197, 94, 0.1));
  color: var(--color-success, #22c55e);
}

.badge-disabled {
  background: var(--bg-disabled, #e2e8f0);
  color: var(--color-text-secondary, #64748b);
}

/* ── 插件市场 ── */
.marketplace-panel {
  padding: var(--space-lg);
  margin-bottom: var(--space-lg);
}

.marketplace-list {
  display: flex;
  flex-direction: column;
  gap: var(--space-md);
}

.marketplace-card {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: var(--space-md);
  padding: var(--space-md);
  border: 1px solid var(--border-light);
  border-radius: var(--radius-md);
  transition: var(--transition-base);
}

.marketplace-card:hover {
  border-color: var(--border-strong);
  background: var(--bg-hover);
}

.marketplace-info {
  flex: 1;
  min-width: 0;
}

.marketplace-name {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: var(--space-xs);
  font-size: var(--font-size-md);
  font-weight: 600;
  color: var(--color-text);
  margin-bottom: var(--space-xs);
}

.marketplace-meta {
  display: flex;
  gap: var(--space-xs);
  margin-bottom: var(--space-xs);
}

.marketplace-desc {
  font-size: var(--font-size-sm);
  color: var(--color-text-secondary);
  line-height: 1.6;
}

.marketplace-long-desc {
  font-size: var(--font-size-sm);
  color: var(--color-text);
  line-height: 1.7;
  white-space: pre-line;
  background: var(--bg-input);
  border: 1px solid var(--border-light);
  border-radius: var(--radius-md);
  padding: var(--space-md);
}

.marketplace-actions {
  display: flex;
  align-items: center;
  gap: var(--space-sm);
  flex-shrink: 0;
}

@media (max-width: 640px) {
  .marketplace-card {
    flex-direction: column;
  }

  .marketplace-actions {
    width: 100%;
  }
}

/* ── 权限策略 ─────────────────────────────────────── */
.permissions-hint {
  font-size: var(--font-size-sm);
  color: var(--color-text-secondary);
  line-height: 1.7;
  margin: 0 0 var(--space-md);
}

/* 工具数量随插件增长，同样限定高度并纵向滚动 */
.permission-tools {
  display: flex;
  flex-direction: column;
  gap: var(--space-xs);
  max-height: min(40vh, 320px);
  overflow-y: auto;
  overscroll-behavior: contain;
  padding-right: 4px;
}

.permission-tools::-webkit-scrollbar {
  width: 8px;
}

.permission-tools::-webkit-scrollbar-track {
  background: transparent;
}

.permission-tools::-webkit-scrollbar-thumb {
  background: var(--border-strong, rgba(100, 116, 139, 0.4));
  border-radius: 999px;
}

.permission-tool-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: var(--space-md);
  padding: var(--space-xs) 0;
  border-top: 1px solid var(--border-light);
  flex-shrink: 0;
}

.permission-tool-info {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: var(--space-xs);
  min-width: 0;
}

.permission-tool-name {
  font-family: var(--font-mono, monospace);
  font-size: var(--font-size-sm);
  background: var(--bg-input);
  padding: 2px 6px;
  border-radius: var(--radius-sm);
}

.permission-tool-action {
  font-size: var(--font-size-xs);
  color: var(--color-text-secondary);
}

.permission-tool-select {
  width: auto;
  min-width: 120px;
  padding: 4px 8px;
  font-size: var(--font-size-sm);
}

.risk-badge {
  font-size: var(--font-size-xs);
  padding: 2px 8px;
  border-radius: 999px;
  border: 1px solid var(--border-light);
}

.risk-read {
  color: var(--color-success, #16a34a);
}

.risk-write {
  color: var(--color-warning, #d97706);
}

.risk-dangerous {
  color: var(--color-danger, #dc2626);
  font-weight: 600;
}

/* ── Skills ───────────────────────────────────────── */
.skills-hint {
  font-size: var(--font-size-sm);
  color: var(--color-text-secondary);
  line-height: 1.7;
  margin: 0 0 var(--space-md);
}

/* Skills 可能很多：限定高度并纵向滚动，避免一屏看不到下方信息 */
.skill-list {
  display: flex;
  flex-direction: column;
  gap: var(--space-sm);
  max-height: min(52vh, 460px);
  overflow-y: auto;
  overscroll-behavior: contain;
  padding-right: 4px;
  /* 顶部/底部渐隐提示还有更多内容 */
  mask-image: linear-gradient(
    to bottom,
    transparent 0,
    #000 10px,
    #000 calc(100% - 10px),
    transparent 100%
  );
}

.skill-list::-webkit-scrollbar {
  width: 8px;
}

.skill-list::-webkit-scrollbar-track {
  background: transparent;
}

.skill-list::-webkit-scrollbar-thumb {
  background: var(--border-strong, rgba(100, 116, 139, 0.4));
  border-radius: 999px;
}

.skill-list::-webkit-scrollbar-thumb:hover {
  background: var(--color-text-secondary, #64748b);
}

.skill-card {
  padding: var(--space-md);
  /* 滚动容器内卡片不被压缩 */
  flex-shrink: 0;
}

.skill-row {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: var(--space-md);
}

.skill-info {
  flex: 1;
  min-width: 0;
}

.skill-name {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: var(--space-xs);
  font-size: var(--font-size-md);
  font-weight: 600;
  color: var(--color-text);
  margin-bottom: var(--space-xs);
}

.skill-name code {
  font-family: var(--font-mono, monospace);
  background: var(--bg-input);
  padding: 2px 6px;
  border-radius: var(--radius-sm);
}

.skill-desc {
  font-size: var(--font-size-sm);
  color: var(--color-text-secondary);
  line-height: 1.6;
  margin-bottom: var(--space-xs);
  word-break: break-word;
}

.skill-meta {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-sm);
  font-size: var(--font-size-xs);
  color: var(--color-text-muted, var(--color-text-secondary));
}

.skill-actions {
  display: flex;
  align-items: center;
  gap: var(--space-sm);
  flex-shrink: 0;
}

.skill-detail-modal .modal-body {
  max-height: 60vh;
  overflow-y: auto;
}

.skill-detail-desc {
  font-size: var(--font-size-sm);
  color: var(--color-text-secondary);
  line-height: 1.7;
  margin-bottom: var(--space-md);
}

.skill-detail-section {
  margin-bottom: var(--space-md);
}

.skill-detail-label {
  font-size: var(--font-size-xs);
  font-weight: 600;
  color: var(--color-text-secondary);
  margin-bottom: var(--space-xs);
}

.skill-resource-list {
  margin: 0;
  padding-left: var(--space-lg);
  font-size: var(--font-size-sm);
}

.skill-detail-body {
  margin: 0;
  padding: var(--space-md);
  background: var(--bg-input);
  border: 1px solid var(--border-light);
  border-radius: var(--radius-md);
  font-size: var(--font-size-sm);
  line-height: 1.7;
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 40vh;
  overflow-y: auto;
}

@media (max-width: 640px) {
  .skill-row {
    flex-direction: column;
  }

  .skill-actions {
    width: 100%;
  }
}

/* ── Small / XS Button Utilities ──────────────────────── */
.btn-sm {
  padding: var(--space-xs) var(--space-md);
  font-size: var(--font-size-xs);
}
.btn-xs {
  padding: 3px var(--space-sm);
  font-size: var(--font-size-xs);
  border-radius: var(--radius-sm);
}
.btn-danger {
  display: inline-flex;
  align-items: center;
  gap: var(--space-xs);
  padding: var(--space-sm) var(--space-md);
  border-radius: var(--radius-md);
  border: 1px solid var(--color-danger);
  background: var(--color-danger-light);
  color: var(--color-danger);
  font-size: var(--font-size-sm);
  font-weight: 600;
  transition: var(--transition-base);
}
.btn-danger:hover {
  background: var(--color-danger);
  color: #fff;
}

/* ── Section Description ──────────────────────────────── */
.section-desc {
  font-size: var(--font-size-sm);
  color: var(--color-text-secondary);
  margin-bottom: var(--space-md);
  line-height: 1.6;
}

/* ── Empty State ──────────────────────────────────────── */
.empty-state {
  text-align: center;
  padding: var(--space-xl);
  color: var(--color-text-tertiary);
  font-size: var(--font-size-sm);
}
.empty-state.small {
  padding: var(--space-lg);
}

/* ── MCP Tab ──────────────────────────────────────────── */
.mcp-server-card {
  padding: var(--space-lg);
  margin-bottom: var(--space-md);
  transition: var(--transition-base);
}
.mcp-server-card:hover {
  box-shadow: var(--shadow-md);
}
.mcp-server-head {
  display: flex;
  align-items: center;
  gap: var(--space-sm);
  flex-wrap: wrap;
}
.mcp-dot {
  width: 9px;
  height: 9px;
  border-radius: var(--radius-full);
  flex-shrink: 0;
}
.mcp-dot.on {
  background: var(--color-success);
  box-shadow: 0 0 6px var(--color-success-light);
}
.mcp-dot.off {
  background: var(--color-text-tertiary);
}
.mcp-name {
  font-size: var(--font-size-md);
  font-weight: 600;
  color: var(--color-text);
}
.mcp-cmd {
  flex: 1;
  min-width: 0;
  font-family: var(--font-mono);
  font-size: var(--font-size-xs);
  color: var(--color-text-secondary);
  word-break: break-all;
}
.mcp-tools {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-xs);
  margin-top: var(--space-md);
}
.mcp-tool-chip {
  background: var(--bg-tag);
  color: var(--color-tag);
  padding: 3px var(--space-sm);
  border-radius: var(--radius-full);
  font-size: var(--font-size-xs);
  font-weight: 500;
  font-family: var(--font-mono);
}
.mcp-tool-empty {
  margin-top: var(--space-sm);
  font-size: var(--font-size-xs);
  color: var(--color-text-tertiary);
}
.mcp-add-card {
  padding: var(--space-lg);
  margin-top: var(--space-lg);
}
.mcp-add-title {
  font-size: var(--font-size-md);
  font-weight: 600;
  margin-bottom: var(--space-md);
  color: var(--color-text);
}
.mcp-error {
  margin-top: var(--space-sm);
  font-size: var(--font-size-sm);
  color: var(--color-danger);
}

.mcp-type-badge {
  font-size: 10px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  padding: 2px 7px;
  border-radius: var(--radius-sm);
  background: var(--bg-tag);
  color: var(--color-tag);
  flex-shrink: 0;
}
.mcp-type-badge.sse {
  background: var(--color-warning-light);
  color: var(--color-warning);
}
.mcp-disabled-badge {
  font-size: 10px;
  padding: 2px 7px;
  border-radius: var(--radius-sm);
  background: var(--bg-disabled);
  color: var(--color-text-tertiary);
}
.mcp-desc {
  margin-top: var(--space-xs);
  font-size: var(--font-size-xs);
  color: var(--color-text-secondary);
}
.mcp-type-switch {
  display: inline-flex;
  gap: var(--space-xs);
  padding: 3px;
  margin-bottom: var(--space-md);
  background: var(--bg-main);
  border-radius: var(--radius-md);
}
.mcp-type-btn {
  padding: var(--space-xs) var(--space-md);
  border-radius: var(--radius-sm);
  font-size: var(--font-size-xs);
  font-weight: 600;
  color: var(--color-text-secondary);
  cursor: pointer;
  transition: var(--transition-base);
}
.mcp-type-btn.active {
  background: var(--color-primary);
  color: var(--color-text-inverse);
}

/* ── Artifacts Tab ────────────────────────────────────── */
.artifact-card {
  padding: var(--space-md) var(--space-lg);
  margin-bottom: var(--space-md);
}
.artifact-head {
  display: flex;
  align-items: center;
  gap: var(--space-sm);
  flex-wrap: wrap;
}
.artifact-tool {
  font-family: var(--font-mono);
  font-size: var(--font-size-xs);
  font-weight: 600;
  color: var(--color-primary);
  background: var(--bg-tag);
  padding: 2px var(--space-sm);
  border-radius: var(--radius-sm);
  max-width: 160px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.artifact-id {
  font-family: var(--font-mono);
  font-size: var(--font-size-xs);
  color: var(--color-text-secondary);
}
.artifact-meta,
.artifact-time {
  font-size: var(--font-size-xs);
  color: var(--color-text-tertiary);
}
.artifact-summary {
  margin-top: var(--space-sm);
  font-size: var(--font-size-sm);
  color: var(--color-text-secondary);
  word-break: break-word;
}
.artifact-content {
  margin-top: var(--space-sm);
  padding: var(--space-md);
  background: var(--bg-code-block);
  border: 1px solid var(--border-light);
  border-radius: var(--radius-md);
  font-family: var(--font-mono);
  font-size: var(--font-size-xs);
  color: var(--color-text);
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 40vh;
  overflow: auto;
}

/* ── Memory Tab ───────────────────────────────────────── */
.memory-search {
  display: flex;
  gap: var(--space-sm);
  align-items: center;
}
.memory-search .form-input {
  width: 220px;
}
.memory-add-card {
  padding: var(--space-lg);
  margin-bottom: var(--space-lg);
}
.memory-form {
  display: grid;
  grid-template-columns: 1fr 2fr 1fr 130px auto;
  gap: var(--space-sm);
  align-items: center;
}
.memory-scope-select {
  cursor: pointer;
}
.memory-card {
  padding: var(--space-md) var(--space-lg);
  margin-bottom: var(--space-sm);
}
.memory-head {
  display: flex;
  align-items: center;
  gap: var(--space-sm);
  flex-wrap: wrap;
}
.memory-key {
  font-family: var(--font-mono);
  font-size: var(--font-size-sm);
  font-weight: 600;
  color: var(--color-primary);
}
.memory-scope {
  font-size: var(--font-size-xs);
  padding: 1px var(--space-sm);
  border-radius: var(--radius-full);
  background: var(--bg-tag);
  color: var(--color-tag);
}
.memory-scope.session {
  background: var(--bg-snapshot);
  color: var(--color-text-secondary);
}
.memory-tag {
  font-size: var(--font-size-xs);
  padding: 1px var(--space-sm);
  border-radius: var(--radius-sm);
  background: var(--bg-tool);
  color: var(--color-warning);
}
.memory-value {
  margin-top: var(--space-sm);
  font-size: var(--font-size-sm);
  color: var(--color-text-secondary);
  word-break: break-word;
  white-space: pre-wrap;
}
.memory-card .form-input {
  margin-top: var(--space-sm);
}

/* ── 自动总结卡片 ── */
.memory-summary-card {
  padding: var(--space-lg);
  margin-bottom: var(--space-lg);
}
.memory-summary-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--space-md);
}
.memory-summary-info {
  min-width: 0;
}
.memory-summary-meta {
  margin: 0 0 var(--space-xs);
  font-size: var(--font-size-xs);
  color: var(--color-primary);
  font-weight: 600;
}
.memory-summary-desc {
  margin: 0;
  font-size: var(--font-size-xs);
  line-height: 1.6;
  color: var(--color-text-secondary);
}
.memory-summary-msg {
  margin: var(--space-sm) 0 0;
  font-size: var(--font-size-xs);
  color: var(--color-warning);
}

/* ── 定时任务 ─────────────────────────────────────────── */
.sched-head-actions {
  display: flex;
  gap: var(--space-sm);
}
.sched-form-card {
  padding: var(--space-lg);
  margin-bottom: var(--space-lg);
}
.sched-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: var(--space-md);
}
.sched-grid-3 {
  grid-template-columns: repeat(3, 1fr);
}
.sched-type-switch {
  display: inline-flex;
  flex-wrap: wrap;
  gap: var(--space-xs);
  padding: 3px;
  margin-bottom: var(--space-sm);
  background: var(--bg-main);
  border-radius: var(--radius-md);
}
.sched-type-btn {
  padding: var(--space-xs) var(--space-md);
  border-radius: var(--radius-sm);
  font-size: var(--font-size-xs);
  font-weight: 600;
  color: var(--color-text-secondary);
  cursor: pointer;
  transition: var(--transition-base);
}
.sched-type-btn.active {
  background: var(--color-primary);
  color: var(--color-text-inverse);
}
.sched-spec-row {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: var(--space-sm);
}
.sched-inline-label {
  font-size: var(--font-size-xs);
  color: var(--color-text-secondary);
  white-space: nowrap;
}
.sched-time-input,
.sched-num-input,
.sched-unit-select {
  width: auto;
  min-width: 110px;
}
.sched-num-input {
  min-width: 76px;
}
.sched-limits {
  display: flex;
  align-items: center;
  gap: var(--space-xs);
  flex-wrap: wrap;
}
.weekday-chip {
  width: 30px;
  height: 30px;
  border-radius: var(--radius-full);
  border: 1px solid var(--border-color);
  background: var(--bg-surface);
  color: var(--color-text-secondary);
  font-size: var(--font-size-xs);
  font-weight: 600;
  cursor: pointer;
  transition: var(--transition-base);
}
.weekday-chip.active {
  background: var(--color-primary);
  border-color: var(--color-primary);
  color: var(--color-text-inverse);
}
.form-hint-inline {
  font-weight: 400;
  color: var(--color-text-tertiary);
}
.pick-grid {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-sm);
}
.pick-chip {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 1px;
  padding: 6px 12px;
  border-radius: var(--radius-md);
  border: 1px solid var(--border-color);
  background: var(--bg-surface);
  color: var(--color-text-secondary);
  cursor: pointer;
  transition: var(--transition-base);
  text-align: left;
}
.pick-chip:hover {
  border-color: var(--color-primary);
}
.pick-chip.active {
  background: var(--color-primary-light);
  border-color: var(--color-primary);
  color: var(--color-text);
}
.pick-chip.danger.active {
  background: var(--color-danger-light);
  border-color: var(--color-danger);
}
.pick-name {
  font-size: var(--font-size-xs);
  font-weight: 600;
}
.pick-sub {
  font-size: 10px;
  color: var(--color-text-tertiary);
}
.pick-empty {
  font-size: var(--font-size-xs);
  color: var(--color-text-tertiary);
}
.sched-form-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-top: var(--space-md);
  padding-top: var(--space-md);
  border-top: 1px solid var(--border-light);
}
.sched-enable {
  display: flex;
  align-items: center;
  gap: var(--space-xs);
  font-size: var(--font-size-sm);
  color: var(--color-text-secondary);
  cursor: pointer;
}
.sched-form-buttons {
  display: flex;
  gap: var(--space-sm);
}

/* 任务卡片 */
.sched-card {
  padding: var(--space-md) var(--space-lg);
  margin-bottom: var(--space-md);
}
.sched-card-head {
  display: flex;
  align-items: center;
  gap: var(--space-sm);
  flex-wrap: wrap;
}
.sched-dot {
  width: 9px;
  height: 9px;
  border-radius: var(--radius-full);
  flex-shrink: 0;
}
.sched-dot.on {
  background: var(--color-success);
  box-shadow: 0 0 6px var(--color-success-light);
}
.sched-dot.off {
  background: var(--color-text-tertiary);
}
.sched-name {
  font-size: var(--font-size-md);
  font-weight: 600;
  color: var(--color-text);
}
.sched-badge {
  font-size: 10px;
  font-weight: 700;
  padding: 2px 8px;
  border-radius: var(--radius-full);
  background: var(--bg-tag);
  color: var(--color-tag);
}
.sched-meta {
  font-size: var(--font-size-xs);
  color: var(--color-text-tertiary);
}
.sched-status {
  font-size: 10px;
  font-weight: 700;
  padding: 2px 8px;
  border-radius: var(--radius-sm);
}
.sched-status.ok {
  background: var(--color-success-light);
  color: var(--color-success);
}
.sched-status.error {
  background: var(--color-danger-light);
  color: var(--color-danger);
}
.sched-status.skipped {
  background: var(--color-warning-light);
  color: var(--color-warning);
}
.sched-desc {
  margin: var(--space-xs) 0 0;
  font-size: var(--font-size-xs);
  color: var(--color-text-secondary);
}
.sched-prompt {
  margin: var(--space-sm) 0 0;
  font-size: var(--font-size-sm);
  color: var(--color-text-secondary);
  background: var(--bg-code-block);
  border-radius: var(--radius-sm);
  padding: var(--space-sm) var(--space-md);
  white-space: pre-wrap;
  word-break: break-word;
}
.sched-tags {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-xs);
  margin-top: var(--space-sm);
}
.sched-tag {
  font-size: 10px;
  padding: 2px 8px;
  border-radius: var(--radius-sm);
  background: var(--bg-hover);
  color: var(--color-text-secondary);
}
.sched-tag.mcp {
  background: var(--color-warning-light);
  color: var(--color-warning);
}
.sched-tag.skill {
  background: var(--bg-tag);
  color: var(--color-tag);
}
.sched-tag.tool {
  background: var(--color-primary-light);
  color: var(--color-primary);
}
.sched-tag.none {
  background: var(--bg-disabled);
  color: var(--color-text-tertiary);
}
.sched-error {
  margin: var(--space-sm) 0 0;
  font-size: var(--font-size-xs);
  color: var(--color-danger);
}
.sched-actions {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-sm);
  margin-top: var(--space-md);
  padding-top: var(--space-sm);
  border-top: 1px solid var(--border-light);
}
.sched-runs {
  margin-top: var(--space-sm);
  display: flex;
  flex-direction: column;
  gap: var(--space-sm);
  max-height: 320px;
  overflow-y: auto;
}
.sched-run {
  padding: var(--space-sm) var(--space-md);
  background: var(--bg-main);
  border-radius: var(--radius-md);
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: var(--space-sm);
}
.sched-run-dot {
  width: 8px;
  height: 8px;
  border-radius: var(--radius-full);
  flex-shrink: 0;
}
.sched-run-dot.ok {
  background: var(--color-success);
}
.sched-run-dot.error {
  background: var(--color-danger);
}
.sched-run-dot.skipped {
  background: var(--color-warning);
}
.sched-run-dot.running {
  background: var(--color-primary);
}
.sched-run-time {
  font-size: var(--font-size-xs);
  font-family: var(--font-mono);
  color: var(--color-text-secondary);
}
.sched-run-status {
  font-size: var(--font-size-xs);
  font-weight: 600;
  color: var(--color-text);
}
.sched-run-meta {
  font-size: 10px;
  color: var(--color-text-tertiary);
}
.sched-run-summary,
.sched-run-error {
  flex-basis: 100%;
  margin: 0;
  font-size: var(--font-size-xs);
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-word;
}
.sched-run-summary {
  color: var(--color-text-secondary);
}
.sched-run-error {
  color: var(--color-danger);
}

@media (max-width: 720px) {
  .sched-grid,
  .sched-grid-3 {
    grid-template-columns: 1fr;
  }
}
.memory-edit-actions {
  display: flex;
  gap: var(--space-sm);
  margin-top: var(--space-sm);
}

@media (max-width: 720px) {
  .memory-form {
    grid-template-columns: 1fr;
  }
  .memory-search .form-input {
    width: 100%;
  }
  .trace-bar-row {
    grid-template-columns: 110px 1fr 56px;
  }
  .artifact-head,
  .memory-head,
  .trace-head {
    row-gap: var(--space-xs);
  }
}

/* ── Tracing Tab ──────────────────────────────────────── */
.trace-card {
  padding: var(--space-md) var(--space-lg);
  margin-bottom: var(--space-md);
}
.trace-head {
  display: flex;
  align-items: center;
  gap: var(--space-sm);
  flex-wrap: wrap;
}
.trace-status {
  width: 9px;
  height: 9px;
  border-radius: var(--radius-full);
  flex-shrink: 0;
}
.trace-status.ok {
  background: var(--color-success);
}
.trace-status.error {
  background: var(--color-danger);
}
.trace-name {
  font-weight: 600;
  color: var(--color-text);
  font-size: var(--font-size-sm);
}
.trace-meta {
  font-size: var(--font-size-xs);
  color: var(--color-text-tertiary);
}
.trace-meta.error {
  color: var(--color-danger);
}
.trace-lane {
  margin-top: var(--space-md);
  border-top: 1px solid var(--border-light);
  padding-top: var(--space-md);
  /* 步骤多时可纵向滚动，避免内容被截断看不到 */
  max-height: 46vh;
  overflow-y: auto;
  overscroll-behavior: contain;
  padding-right: var(--space-xs);
}
.trace-lane::-webkit-scrollbar {
  width: 8px;
}
.trace-lane::-webkit-scrollbar-track {
  background: var(--bg-main);
  border-radius: var(--radius-full);
}
.trace-lane::-webkit-scrollbar-thumb {
  background: var(--border-strong);
  border-radius: var(--radius-full);
}
.trace-lane::-webkit-scrollbar-thumb:hover {
  background: var(--color-primary);
}
.trace-bar-row {
  display: grid;
  grid-template-columns: 180px 1fr 70px;
  align-items: center;
  gap: var(--space-sm);
  margin-bottom: var(--space-xs);
}
.trace-bar-label {
  font-size: var(--font-size-xs);
  color: var(--color-text-secondary);
  display: flex;
  align-items: center;
  gap: var(--space-xs);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.trace-kind {
  font-size: 10px;
  padding: 0 5px;
  border-radius: var(--radius-sm);
  background: var(--bg-tag);
  color: var(--color-tag);
  flex-shrink: 0;
}
.trace-kind.tool {
  background: var(--bg-tool);
  color: var(--color-warning);
}
.trace-bar-track {
  position: relative;
  height: 14px;
  background: var(--bg-main);
  border-radius: var(--radius-sm);
  overflow: hidden;
}
.trace-bar {
  position: absolute;
  top: 0;
  height: 100%;
  border-radius: var(--radius-sm);
  background: var(--color-primary);
  opacity: 0.85;
  min-width: 2px;
}
.trace-bar.tool {
  background: var(--color-warning);
}
.trace-bar.error {
  background: var(--color-danger);
}
.trace-bar-ms {
  font-size: var(--font-size-xs);
  color: var(--color-text-tertiary);
  text-align: right;
}
.trace-bar-empty {
  font-size: var(--font-size-sm);
  color: var(--color-text-tertiary);
}
.trace-detail {
  margin-top: var(--space-sm);
  padding: var(--space-sm) var(--space-md);
  background: var(--bg-code-block);
  border-radius: var(--radius-md);
  font-size: var(--font-size-xs);
}
.trace-detail-name {
  font-family: var(--font-mono);
  font-weight: 600;
  color: var(--color-text);
  margin-bottom: 2px;
}
.trace-detail-line {
  color: var(--color-text-secondary);
  word-break: break-word;
}
.trace-detail-error {
  color: var(--color-danger);
  word-break: break-word;
}
</style>
