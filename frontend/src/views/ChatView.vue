<script setup lang="ts">
import { ref, watch, computed, onMounted, onUnmounted, nextTick } from 'vue'
import { useChatStore } from '../stores/chat'
import { useProviderStore } from '../stores/providers'
import { useTodoStore } from '../stores/todos'
import { apiClient, wsConnectionStatus, ApiError } from '../api/client'
import type { WSFrame } from '../api/types'
import { useLanguage } from '../composables/useLanguage'
import { useSettingsStore } from '../stores/settings'
import { useAuthStore } from '../stores/auth'
import SessionSidebar from '../components/SessionSidebar.vue'
import MessageItem from '../components/MessageItem.vue'
import AttachmentImage from '../components/AttachmentImage.vue'
import ModelSelector from '../components/ModelSelector.vue'
import MarkdownRenderer from '../components/MarkdownRenderer.vue'
import ProcessTrace from '../components/ProcessTrace.vue'
import type { Message, ProcessStep } from '../api/types'
import { pickOnboardingTarget } from '../utils/onboarding'
import { modelsForProvider } from '../utils/modelCatalog'

const chatStore = useChatStore()
const providerStore = useProviderStore()
const settingsStore = useSettingsStore()
const authStore = useAuthStore()
const todoStore = useTodoStore()
const { t } = useLanguage()

const inputText = ref('')
const selectedModel = ref(settingsStore.sessionSettings.model || 'deepseek-flash')
const selectedProvider = ref('')
const messagesContainer = ref<HTMLElement | null>(null)
// 是否位于消息列表底部（用户上滑时停止自动滚动）
const isAtBottom = ref(true)

// ── 输入框自适应高度（3→7 行；超 7 行可放大到整页） ──────────
const textareaRef = ref<HTMLTextAreaElement | null>(null)
const isExpanded = ref(false) // 是否处于「放大到整页」状态
const canExpand = ref(false) // 内容是否超过 7 行（决定是否显示放大按钮）
const MIN_ROWS = 1
const MAX_ROWS = 7

/** 读取 textarea 单行高度与上下内边距（跟随主题字号，避免硬编码）。 */
function _metrics(el: HTMLTextAreaElement): { line: number; pad: number } {
  const cs = window.getComputedStyle(el)
  const lh = parseFloat(cs.lineHeight)
  const line = Number.isFinite(lh) && lh > 0 ? lh : parseFloat(cs.fontSize) * 1.6
  const pad = (parseFloat(cs.paddingTop) || 0) + (parseFloat(cs.paddingBottom) || 0)
  return { line, pad }
}

/** 放大模式下输入框可用的最大高度（视口高度减去顶栏与边距）。 */
function _maxExpandedHeight(): number {
  return Math.max(200, window.innerHeight - 150)
}

/** 根据内容与展开状态计算并应用 textarea 高度。 */
function autoResize() {
  const el = textareaRef.value
  if (!el) return
  const { line, pad } = _metrics(el)
  const minH = line * MIN_ROWS + pad
  const maxH = line * MAX_ROWS + pad

  // 先置 auto 以测量真实内容高度
  el.style.height = 'auto'
  const contentH = el.scrollHeight

  // 内容超过 7 行才显示放大按钮
  canExpand.value = contentH > maxH + 1

  // 内容缩回 7 行以内时自动退出放大态：
  // 否则放大按钮消失后，输入框会卡在整页高度且无法缩小。
  if (!canExpand.value && isExpanded.value) {
    isExpanded.value = false
  }

  if (isExpanded.value) {
    // 放大：撑到页面最大高度，内容超出则内部滚动
    const h = _maxExpandedHeight()
    el.style.height = h + 'px'
    el.style.overflowY = contentH > h ? 'auto' : 'hidden'
  } else {
    // 常规：在 3~7 行之间自适应
    const h = Math.min(Math.max(contentH, minH), maxH)
    el.style.height = h + 'px'
    el.style.overflowY = contentH > maxH ? 'auto' : 'hidden'
  }
}

/** 切换放大 / 缩小。 */
function toggleExpand() {
  isExpanded.value = !isExpanded.value
  nextTick(autoResize)
}

// 内容变化时重新计算高度
watch(inputText, () => {
  nextTick(autoResize)
})

// ── 历史消息归并（按提问分组 + 重新回答版本） ──────────────
// 一条「轮次（turn）」= 一个 user 提问 + 它名下所有 assistant 答案版本。
// 重新回答会在同一 user 消息下再追加一条 parent_id 指向它的 assistant 答案，
// 因此一个轮次可能有多条答案版本。每条版本 = 一段按时间排序的 assistant 消息中
// 以「带正文的最终答案」结尾的切片；切片内该最终答案之前的中间消息（过渡语 /
// 工具调用）作为这一版的「执行过程」归并展示（逻辑继承 0.0.24 的归并修复）。 ──
interface Version {
  answer: Message
  steps: ProcessStep[]
}
interface Turn {
  id: string
  userMessage: Message
  versions: Version[]
}

/** 把若干条中间 assistant 消息归并为「执行过程」步骤（思维链 / 说明 / 工具调用）。 */
function toVersionSteps(msgs: Message[]): ProcessStep[] {
  const steps: ProcessStep[] = []
  for (const m of msgs) {
    if (m.reasoning && m.reasoning.trim()) {
      steps.push({ kind: 'reasoning', text: m.reasoning })
    }
    if ((m.content || '').trim()) {
      steps.push({ kind: 'text', text: m.content!.trim() })
    }
    for (const tc of m.tool_calls || []) {
      steps.push({
        kind: 'tool',
        tool: {
          tool_name: tc.tool_name,
          args: tc.args,
          result: tc.result,
          error: tc.error || undefined,
        },
      })
    }
  }
  return steps
}

const turns = computed<{ turns: Turn[]; standalone: Message[] }>(() => {
  const msgs = chatStore.messages
  const turnsList: Turn[] = []
  const standalone: Message[] = []
  const rawByTurn = new Map<string, Message[]>()
  let currentUser: Message | null = null

  // 第一遍：归类——user 开新轮次；system 单独渲染；assistant 按 parent_id（缺省回退当前 user）归桶
  for (const m of msgs) {
    if (m.role === 'user') {
      currentUser = m
      turnsList.push({ id: m.id, userMessage: m, versions: [] })
      rawByTurn.set(m.id, [])
    } else if (m.role === 'system') {
      standalone.push(m)
      currentUser = null
    } else if (m.role === 'assistant') {
      const key = m.parent_id ?? currentUser?.id
      const bucket = key && rawByTurn.has(key) ? rawByTurn.get(key)! : currentUser && rawByTurn.has(currentUser.id) ? rawByTurn.get(currentUser.id)! : null
      bucket?.push(m)
    }
  }

  // 第二遍：每个轮次把 assistant 按时间排序后，以「终答」切分为版本。
  // 终答 = 无 tool_calls 且有正文的 assistant 消息；带 tool_calls 的消息
  // （即使模型附带了过渡文字，如"我先查一下天气"）属于执行过程，
  // 归入当前版本的 steps——否则一次重新回答会被拆成两个假版本。
  for (const turn of turnsList) {
    const raw = (rawByTurn.get(turn.id) || [])
      .slice()
      .sort((a, b) => (a.created_at || '').localeCompare(b.created_at || ''))
    const isFinalAnswer = (m: Message) =>
      (m.content || '').trim() !== '' && !(m.tool_calls && m.tool_calls.length)
    let segment: Message[] = []
    for (const m of raw) {
      segment.push(m)
      if (isFinalAnswer(m)) {
        turn.versions.push({
          answer: m,
          steps: toVersionSteps(segment.slice(0, -1)),
        })
        segment = []
      }
    }
    // 末尾无终答的片段（如被中断的轮次）丢弃，不形成版本
  }

  return { turns: turnsList, standalone }
})

/** 每轮当前选中版本下标（缺省指向最新版）。 */
const selectedVersion = ref<Record<string, number>>({})
/** 记录每轮历史版本数，用于「新增版本时自动跳到最新版」。 */
const prevVersionCounts = ref<Record<string, number>>({})

function versionIndex(turn: Turn): number {
  const n = turn.versions.length
  if (n === 0) return -1
  const i = selectedVersion.value[turn.id]
  if (i === undefined) return n - 1
  return Math.min(Math.max(i, 0), n - 1)
}

function setVersion(turnId: string, idx: number) {
  selectedVersion.value = { ...selectedVersion.value, [turnId]: idx }
}

function prevVersion(turn: Turn) {
  const i = versionIndex(turn)
  if (i > 0) setVersion(turn.id, i - 1)
}

function nextVersion(turn: Turn) {
  const i = versionIndex(turn)
  if (i < turn.versions.length - 1) setVersion(turn.id, i + 1)
}

// 轮次/版本数变化时：新增版本（重新回答）自动定位到最新版；下标越界则夹紧
watch(
  turns,
  (val) => {
    const nextSel = { ...selectedVersion.value }
    const nextCnt = { ...prevVersionCounts.value }
    for (const turn of val.turns) {
      const n = turn.versions.length
      const prev = nextCnt[turn.id] ?? n
      if (n > prev) {
        // 刚重新生成了一条新答案 → 自动展示最新版
        nextSel[turn.id] = n - 1
      } else {
        const cur = nextSel[turn.id]
        if (cur === undefined || cur > n - 1) nextSel[turn.id] = n - 1
      }
      nextCnt[turn.id] = n
    }
    selectedVersion.value = nextSel
    prevVersionCounts.value = nextCnt
  },
)

// 初始化 provider 选择：优先 deepseek（且已配置 Key、已启用）
watch(() => providerStore.providers, (providers) => {
  if (providers.length > 0 && !selectedProvider.value) {
    const usable = providers.filter((p) => p.enabled !== false && p.has_api_key)
    const deepseek = usable.find((p) => p.id === 'deepseek' || p.id.includes('deepseek'))
    if (deepseek) {
      selectedProvider.value = deepseek.id
    } else if (usable.length > 0) {
      selectedProvider.value = usable[0].id
    }
  }
}, { immediate: true })

// ── 模型区三态（右上角 provider/模型选项） ──────────
// unconfigured：没有任何 provider 配置 API Key → 仅显示「未配置模型」；
// all-disabled：有配置但全部停用 → 仅显示「模型全部已停用」；
// ready：至少一个已配置且启用 → 正常显示 provider + 模型下拉。
const modelAreaState = computed<'loading' | 'unconfigured' | 'all-disabled' | 'ready'>(() => {
  if (!providerStore.providersLoaded || providerStore.loadError) return 'loading'
  const providers = providerStore.providers
  if (providers.length === 0) return 'unconfigured'
  if (!providers.some((p) => p.has_api_key)) return 'unconfigured'
  if (!providers.some((p) => p.has_api_key && p.enabled !== false)) return 'all-disabled'
  return 'ready'
})

// selectedModel 与所选 provider 的可用模型保持同步：
// localStorage 里可能存着已下线的旧模型名（如 deepseek-v4-flash），
// 不校正会让下拉显示为空白、发消息时模型 404。
watch(
  [selectedProvider, () => providerStore.providers],
  () => {
    if (modelAreaState.value !== 'ready' || !selectedProvider.value) return
    const p = providerStore.providers.find((x) => x.id === selectedProvider.value)
    const available = modelsForProvider(selectedProvider.value, p?.models || [])
    if (available.length === 0) return
    if (!available.some((o) => o.id === selectedModel.value)) {
      selectedModel.value = available[0].id
    }
  },
  { immediate: true },
)

// ── 人工确认（Human-in-the-loop） ──────────────────
interface ConfirmRequest {
  request_id: string
  tool_name: string
  args: Record<string, unknown>
  risk: string
  reason: string
}

const confirmRequest = ref<ConfirmRequest | null>(null)

const RISK_LABEL: Record<string, string> = {
  read: '只读',
  write: '写操作',
  dangerous: '危险（可执行代码）',
}

function riskLabel(risk: string) {
  return RISK_LABEL[risk] ?? risk
}

function formattedArgs(args: Record<string, unknown>) {
  try {
    return JSON.stringify(args, null, 2)
  } catch {
    return String(args)
  }
}

function replyConfirm(approved: boolean) {
  if (!confirmRequest.value) return
  apiClient.ws.send({
    type: 'confirm_reply',
    data: {
      request_id: confirmRequest.value.request_id,
      approved,
    },
  })
  confirmRequest.value = null
}

// WS 消息处理
function onWSMessage(data: unknown) {
  const frame = data as WSFrame
  // 人工确认请求不进消息流，单独弹窗处理
  if (frame?.type === 'confirm_request') {
    confirmRequest.value = frame.data as unknown as ConfirmRequest
    return
  }
  if (frame?.type === 'confirm_timeout' && confirmRequest.value) {
    confirmRequest.value = null
  }
  // AI 写完待办列表或一轮结束后刷新任务面板
  if (frame?.type === 'tool_event' && frame.data?.tool_name === 'todo_write') {
    refreshTodos()
  }
  if (frame?.type === 'done') {
    refreshTodos()
  }
  chatStore.handleWSFrame(frame)
}

// ── 任务清单（Todo） ────────────────────────────────
function refreshTodos() {
  if (chatStore.currentSessionId) {
    todoStore.loadTodos(chatStore.currentSessionId)
  }
}

function clearTodos() {
  if (chatStore.currentSessionId) {
    todoStore.clearTodos(chatStore.currentSessionId)
  }
}

const TODO_MARK: Record<string, string> = {
  pending: '○',
  in_progress: '◐',
  completed: '●',
}

const TODO_LABEL: Record<string, string> = {
  pending: '待开始',
  in_progress: '进行中',
  completed: '已完成',
}

// 切换会话时同步刷新任务清单
watch(
  () => chatStore.currentSessionId,
  (id) => {
    if (id) todoStore.loadTodos(id)
    else todoStore.reset()
  },
  { immediate: true }
)

// 滚动到底部
function scrollToBottom() {
  nextTick(() => {
    if (messagesContainer.value) {
      messagesContainer.value.scrollTop = messagesContainer.value.scrollHeight
      isAtBottom.value = true
    }
  })
}

// 滚动监听：判断是否停在底部（用户上滑后停止自动滚动）
function onMessagesScroll() {
  const el = messagesContainer.value
  if (!el) return
  const distance = el.scrollHeight - el.scrollTop - el.clientHeight
  isAtBottom.value = distance < 50
}

// 向下按钮：流式中直接滚到最新文字；非流式逐条跳到下一条提问
function handleScrollDown() {
  if (chatStore.isStreaming) {
    scrollToBottom()
    return
  }
  const el = messagesContainer.value
  if (!el) return
  const containerRect = el.getBoundingClientRect()
  // 视口参考线：容器顶部往下 15% 处
  const refY = containerRect.top + containerRect.height * 0.15
  const userRows = Array.from(el.querySelectorAll<HTMLElement>('.message-row.user'))
  let next: HTMLElement | null = null
  for (const row of userRows) {
    if (row.getBoundingClientRect().top > refY) {
      next = row
      break
    }
  }
  if (next) {
    next.scrollIntoView({ behavior: 'smooth', block: 'start' })
  } else {
    // 已到最后的提问之后，直接回到底部
    scrollToBottom()
  }
}

// 发送消息
function handleSend() {
  const hasText = inputText.value.trim().length > 0
  const hasFiles = chatStore.pendingAttachments.length > 0
  // 既无文本又无附件，或正在流式输出时，不发送
  if ((!hasText && !hasFiles) || chatStore.isStreaming) return
  let text = inputText.value
  // 追问模式：把被追问的问答作为引用前缀一起发出（同一会话上下文已含原文）
  if (followUpQuote.value) {
    const q = followUpQuote.value.question.trim().slice(0, 80)
    const prefix = q ? `> 【追问】针对上文回答（提问：「${q}」）\n\n` : '> 【追问】针对上一条回答\n\n'
    text = prefix + text
    followUpQuote.value = null
  }
  chatStore.sendMessage(
    text,
    selectedProvider.value,
    selectedModel.value || undefined,
    chatStore.pendingAttachments,
  )
  inputText.value = ''
  // 发送后收起放大状态，输入框回到 3 行起步
  isExpanded.value = false
  nextTick(autoResize)
  scrollToBottom()
}

// ── 附件上传 ──────────────────────────────────
const fileInput = ref<HTMLInputElement | null>(null)

const ALLOWED_EXT = [
  'txt', 'md', 'markdown', 'csv', 'json', 'yaml', 'yml', 'log', 'py', 'js', 'ts',
  'tsx', 'jsx', 'html', 'htm', 'xml', 'ini', 'toml', 'cfg', 'tex', 'rst', 'sh',
  'bat', 'ps1', 'env.example', 'gitignore', 'png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp',
]
const MAX_FILES = 8
const MAX_DOCUMENTS = 5
const MAX_IMAGES = 4

function openFilePicker() {
  fileInput.value?.click()
}

function extOf(name: string): string {
  const m = name.toLowerCase().match(/\.([a-z0-9.]+)$/)
  return m ? m[1] : ''
}

async function handleFileSelect(e: Event) {
  const target = e.target as HTMLInputElement
  const files = Array.from(target.files || [])
  target.value = '' // 允许重复选择同一文件
  if (!files.length) return
  // 客户端初步过滤不支持的类型，减少无效请求
  const accepted: File[] = []
  const docs: File[] = []
  const images: File[] = []
  let rejected = 0
  for (const f of files) {
    if (!ALLOWED_EXT.includes(extOf(f.name))) {
      rejected++
      continue
    }
    const isImage = ['png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp'].includes(
      extOf(f.name),
    )
    if (isImage) images.push(f)
    else docs.push(f)
    accepted.push(f)
  }
  if (rejected > 0) {
    window.alert(t.value('chat.attachmentUnsupported'))
  }
  if (!accepted.length) return
  // 总数上限
  const remaining = MAX_FILES - chatStore.pendingAttachments.length
  if (accepted.length > remaining) {
    window.alert(
      t.value('chat.attachmentLimit').replace('{n}', String(MAX_FILES)),
    )
    return
  }
  // 按类型各自上限
  const pendingDocs = chatStore.pendingAttachments.filter(
    (a: { kind: string }) => a.kind === 'document',
  ).length
  const pendingImages = chatStore.pendingAttachments.filter(
    (a: { kind: string }) => a.kind === 'image',
  ).length
  if (docs.length > MAX_DOCUMENTS - pendingDocs) {
    window.alert(
      t.value('chat.attachmentDocLimit').replace('{n}', String(MAX_DOCUMENTS)),
    )
    return
  }
  if (images.length > MAX_IMAGES - pendingImages) {
    window.alert(
      t.value('chat.attachmentImageLimit').replace('{n}', String(MAX_IMAGES)),
    )
    return
  }
  try {
    await chatStore.uploadPending(accepted)
  } catch (err) {
    // P2-5：不再直接拼接原始错误对象，统一取可读文案
    const msg = err instanceof ApiError ? err.message : String(err)
    window.alert(t.value('chat.attachmentUploadFail') + '：' + msg)
  }
}

function removeAttachment(id: string) {
  chatStore.removePendingAttachment(id)
}

// ── 问答对操作（复制 / 物理删除整轮 / 追问） ─────────────
const followUpQuote = ref<{ question: string; answer: string } | null>(null)

/** 删除某条提问所在的整轮问答（物理删除）。 */
async function handleDeleteTurn(msg: { id: string }) {
  if (chatStore.isStreaming) return
  if (!window.confirm(t.value('chat.confirmDeleteTurn'))) return
  try {
    const removed = await chatStore.deleteTurn(msg.id)
    if (followUpQuote.value) followUpQuote.value = null
    if (removed === 0) {
      window.alert(t.value('chat.deleteTurnEmpty'))
    }
  } catch (e) {
    window.alert(t.value('chat.deleteTurnFail') + ': ' + e)
  }
}

/** 对某条回答「追问」：记录引用并在输入框聚焦。 */
function handleFollowUp(msg: { id: string; content?: string }) {
  const list = chatStore.messages
  const idx = list.findIndex((m) => m.id === msg.id)
  let question = ''
  for (let i = idx - 1; i >= 0; i--) {
    if (list[i].role === 'user') {
      question = list[i].content || ''
      break
    }
  }
  followUpQuote.value = { question, answer: msg.content || '' }
  nextTick(() => {
    const el = document.querySelector('.input-textarea') as HTMLTextAreaElement | null
    el?.focus()
  })
}

function clearFollowUp() {
  followUpQuote.value = null
}

/** 从某条消息处「分叉」出新会话（复制到该消息为止的历史）。 */
async function handleForkAt(msg: { id: string }) {
  if (!chatStore.currentSessionId) return
  try {
    await chatStore.forkSession(chatStore.currentSessionId, msg.id)
  } catch (e) {
    window.alert(t.value('sessions.forkFail') + ': ' + e)
  }
}

/** 重新回答：保留该提问下全部历史答案版本，仅针对同一提问再生成一条新答案。
 *  优先用答案自身的 parent_id 定位提问；旧数据（无 parent_id）回退到向上找 user 消息。 */
function handleRetry(msg: { id: string; parent_id?: string | null }) {
  if (chatStore.isStreaming) return
  const list = chatStore.messages
  let userMsgId = msg.parent_id || ''
  if (!userMsgId) {
    const idx = list.findIndex((m) => m.id === msg.id)
    if (idx < 0) return
    for (let i = idx - 1; i >= 0; i--) {
      if (list[i].role === 'user') {
        userMsgId = list[i].id
        break
      }
    }
  }
  if (!userMsgId) return
  const userMsg = list.find((m) => m.id === userMsgId)
  const content = userMsg?.content || ''
  try {
    chatStore.regenerateAnswer(userMsgId, content, selectedProvider.value)
    scrollToBottom()
  } catch (e) {
    window.alert(t.value('chat.retryFail') + ': ' + e)
  }
}

// 停止生成
function handleStop() {
  chatStore.stopStreaming()
}

// 工具箱下拉（5 大功能入口）
const showToolbox = ref(false)

// ── 模板实例化后的首条提问：WS 就绪即自动发送（走正常链路才有回答） ──
function tryFlushPendingAutoSend() {
  const text = chatStore.pendingAutoSend
  if (!text) return
  // 已流式中 / 未选中 provider：保持待发，等下一个触发点
  if (chatStore.isStreaming || !selectedProvider.value) return
  // 目标会话必须正是当前会话（用户中途切走则不误发）
  if (chatStore.pendingAutoSendSession && chatStore.currentSessionId !== chatStore.pendingAutoSendSession) {
    return
  }
  // 真实对话要求 WS 已连接（模拟模式不发 WS）
  if (!chatStore.mockMode && wsConnectionStatus.value !== 'connected') return
  // 防重：用户若已手动发过同一内容，直接清空待发
  const alreadySent = chatStore.messages.some((m) => m.role === 'user' && m.content === text)
  if (alreadySent) {
    chatStore.pendingAutoSend = ''
    chatStore.pendingAutoSendSession = null
    return
  }
  chatStore.pendingAutoSend = ''
  chatStore.pendingAutoSendSession = null
  chatStore.sendMessage(
    text,
    selectedProvider.value,
    selectedModel.value || undefined,
  )
  scrollToBottom()
}

// WS 连接状态变化 + 会话切换 + provider 就绪时，尝试冲刷待发提问
watch([() => wsConnectionStatus.value, () => chatStore.currentSessionId, () => selectedProvider.value], () => {
  if (wsConnectionStatus.value === 'connected') {
    nextTick(() => tryFlushPendingAutoSend())
  }
}, { immediate: true })

// ── 首次使用引导：配置 DeepSeek API Key（会话页内直接完成，不跳页） ──
const onboardingKey = ref('')
const onboardingSaving = ref(false)
const onboardingError = ref('')
const onboardingDismissed = ref(false)
const onboardingDone = ref(false)

const onboarding = computed(() =>
  pickOnboardingTarget(providerStore.providers, chatStore.mockMode),
)
const showOnboarding = computed(
  () =>
    onboarding.value.needed &&
    !onboardingDismissed.value &&
    !onboardingDone.value,
)

/** 保存引导卡里填入的 API Key（PATCH 保存后刷新状态，并做一次弱连通性校验）。 */
async function saveOnboardingKey() {
  const target = onboarding.value.provider
  const key = onboardingKey.value.trim()
  if (!target || !key || onboardingSaving.value) return
  onboardingSaving.value = true
  onboardingError.value = ''
  try {
    await apiClient.patch(`/providers/${target.id}`, { api_key: key })
    await providerStore.loadProviders()
    if (!chatStore.currentSessionId) {
      // 尚无会话时先建一条，保存后立即可发消息
      await chatStore.createSession()
    }
    onboardingDone.value = true
    onboardingKey.value = ''
    scrollToBottom()
    // 弱校验：连通性失败只提示，不回滚（Key 已保存，可能只是网络问题）
    try {
      const res = await apiClient.post<{ connected: boolean }>(
        `/providers/${target.id}/test`,
        {},
      )
      if (res && res.connected === false) {
        chatStore.error = t.value('chat.onboardTestWarn')
      }
    } catch {
      // test 端点不可用时忽略
    }
  } catch (e) {
    onboardingError.value = e instanceof Error ? e.message : String(e)
  } finally {
    onboardingSaving.value = false
  }
}

// 监听消息变化自动滚动（仅当用户停在底部时；上滑查看历史则停止跟随）
watch(() => [chatStore.messages.length, chatStore.streamingContent, chatStore.processEvents.length, chatStore.processEvents[chatStore.processEvents.length - 1]?.text?.length, chatStore.toolEvents.length], () => {
  if (isAtBottom.value) scrollToBottom()
})

// 切换会话时回到底部
watch(() => chatStore.currentSessionId, async () => {
  isAtBottom.value = true
  await nextTick()
  onMessagesScroll()
  scrollToBottom()
})

// 连接状态文字
function statusText(status: string): string {
  switch (status) {
    case 'connected': return t.value('chat.connected')
    case 'connecting': return t.value('chat.connecting')
    case 'reconnecting': return t.value('chat.reconnecting')
    default: return t.value('chat.disconnected')
  }
}

// WS 消息取消注册函数（避免组件卸载后重复处理 token_delta）
let unregisterWS: (() => void) | null = null

onMounted(() => {
  // 连接 WS
  apiClient.ws.connect()
  unregisterWS = apiClient.ws.onMessage(onWSMessage)
  // 加载 providers
  providerStore.loadProviders()
  providerStore.loadModels()
  // 初始化输入框高度，并监听窗口尺寸变化（放大模式需随视口重算）
  nextTick(autoResize)
  window.addEventListener('resize', autoResize)
})

onUnmounted(() => {
  if (unregisterWS) {
    unregisterWS()
    unregisterWS = null
  }
  window.removeEventListener('resize', autoResize)
  apiClient.ws.disconnect()
})
</script>

<template>
  <div class="chat-view">
    <SessionSidebar />
    <div class="chat-main">
      <!-- Header -->
      <header class="chat-header">
        <div class="header-title">
          <span class="brand-title">andy-harness</span>
          <span class="title-separator">|</span>
          <h2>{{ chatStore.currentSession?.title || '新会话' }}</h2>
        </div>
        <div class="header-controls">
          <button
            :class="['mock-toggle', { active: chatStore.mockMode }]"
            @click="chatStore.mockMode = !chatStore.mockMode"
            :title="chatStore.mockMode ? '模拟模式已开启' : '点击开启模拟模式（无需 API Key）'"
          >
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z" />
              <polyline points="3.27 6.96 12 12.01 20.73 6.96" />
              <line x1="12" y1="22.08" x2="12" y2="12" />
            </svg>
            <span>{{ chatStore.mockMode ? '模拟中' : '模拟' }}</span>
          </button>
          <!-- 模型区三态：未配置 / 全部停用 时仅显示提示文字，否则显示 provider + 模型下拉 -->
          <template v-if="modelAreaState === 'ready'">
            <div class="select-wrapper">
              <select v-model="selectedProvider" class="provider-select">
                <option v-for="p in providerStore.providers" :key="p.id" :value="p.id">
                  {{ p.name }}
                </option>
              </select>
              <svg class="select-arrow" width="12" height="12" viewBox="0 0 24 24" fill="none">
                <path d="M6 9l6 6 6-6" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
              </svg>
            </div>
            <ModelSelector v-model="selectedModel" :provider="selectedProvider" />
          </template>
          <span
            v-else-if="modelAreaState === 'unconfigured'"
            class="model-area-hint"
            :title="t('chat.noModelHint')"
          >{{ t('chat.noModelConfigured') }}</span>
          <span
            v-else-if="modelAreaState === 'all-disabled'"
            class="model-area-hint"
            :title="t('chat.allDisabledHint')"
          >{{ t('chat.allModelsDisabled') }}</span>
          <!-- 连接状态：仅模型可用时展示（未配置/全部停用时避免与模型状态文案冲突） -->
          <div
            v-if="modelAreaState === 'ready'"
            class="conn-status"
            :class="`status-${wsConnectionStatus}`"
          >
            <span class="conn-dot"></span>
            <span class="conn-text">{{ statusText(wsConnectionStatus) }}</span>
          </div>
          <button
            v-if="authStore.enabled"
            class="btn-logout"
            :title="t('auth.logout')"
            @click="authStore.logout"
          >
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4" />
              <polyline points="16 17 21 12 16 7" />
              <line x1="21" y1="12" x2="9" y2="12" />
            </svg>
          </button>
          <!-- 工具箱：5 大功能的会话页入口（下拉收纳，避免头部拥挤） -->
          <div class="toolbox-wrap">
            <button
              :class="['btn-settings', { active: showToolbox }]"
              :title="t('chat.toolbox')"
              @click="showToolbox = !showToolbox"
            >
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <rect x="3" y="3" width="7" height="7" /><rect x="14" y="3" width="7" height="7" />
                <rect x="14" y="14" width="7" height="7" /><rect x="3" y="14" width="7" height="7" />
              </svg>
            </button>
            <div v-if="showToolbox" class="toolbox-backdrop" @click="showToolbox = false"></div>
            <div v-if="showToolbox" class="toolbox-menu">
              <router-link to="/insights" class="toolbox-item" @click="showToolbox = false">
                <span>📊</span><span>{{ t('nav.insights') }}</span>
              </router-link>
              <router-link to="/evals" class="toolbox-item" @click="showToolbox = false">
                <span>🎯</span><span>{{ t('nav.evals') }}</span>
              </router-link>
              <router-link to="/workflows" class="toolbox-item" @click="showToolbox = false">
                <span>🔀</span><span>{{ t('nav.workflows') }}</span>
              </router-link>
              <router-link to="/templates" class="toolbox-item" @click="showToolbox = false">
                <span>📦</span><span>{{ t('nav.templates') }}</span>
              </router-link>
              <router-link to="/devkit" class="toolbox-item" @click="showToolbox = false">
                <span>🧩</span><span>{{ t('nav.devkit') }}</span>
              </router-link>
            </div>
          </div>
          <router-link to="/settings" class="btn-settings" title="设置">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <circle cx="12" cy="12" r="3" />
              <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z" />
            </svg>
          </router-link>
        </div>
      </header>

      <!-- 任务清单（AI 用 todo_write 维护） -->
      <div v-if="todoStore.todos.length > 0" class="todo-panel">
        <div class="todo-header">
          <svg class="todo-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <path d="M9 11l3 3L22 4" />
            <path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11" />
          </svg>
          <span class="todo-title">任务清单</span>
          <span class="todo-progress">
            {{ todoStore.summary.completed || 0 }} / {{ todoStore.todos.length }}
          </span>
          <button class="btn-ghost todo-clear" @click="clearTodos" title="清空任务清单">清空</button>
        </div>
        <ul class="todo-list">
          <li
            v-for="item in todoStore.todos"
            :key="item.id"
            :class="['todo-item', 'todo-' + item.status]"
          >
            <span class="todo-mark">{{ TODO_MARK[item.status] }}</span>
            <span class="todo-content">{{ item.content }}</span>
            <span class="todo-status">{{ TODO_LABEL[item.status] }}</span>
          </li>
        </ul>
      </div>

      <!-- Messages -->
      <div ref="messagesContainer" class="messages-container" @scroll="onMessagesScroll">
        <div class="messages-inner">
          <!-- 首次使用引导：未配置任何 API Key 时，在会话页内直接完成 DeepSeek 配置 -->
          <div v-if="showOnboarding" class="onboarding-card" :class="{ done: onboardingDone }">
            <div class="onboarding-head">
              <svg class="onboarding-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <path d="M15 7h3a5 5 0 0 1 5 5 5 5 0 0 1-5 5h-3m-6 0H6a5 5 0 0 1-5-5 5 5 0 0 1 5-5h3" />
                <line x1="8" y1="12" x2="16" y2="12" />
              </svg>
              <div class="onboarding-title-wrap">
                <h3 class="onboarding-title">{{ t('chat.onboardTitle') }}</h3>
                <p class="onboarding-desc">
                  {{ t('chat.onboardDesc') }}
                  <a href="https://platform.deepseek.com" target="_blank" rel="noopener" class="onboarding-link">
                    {{ t('chat.onboardGetKey') }}
                  </a>
                </p>
              </div>
            </div>
            <div v-if="onboarding.provider" class="onboarding-form">
              <input
                v-model="onboardingKey"
                type="password"
                class="onboarding-input"
                :placeholder="t('chat.onboardKeyPh')"
                autocomplete="off"
                @keydown.enter="saveOnboardingKey"
              />
              <button class="btn-primary onboarding-save" :disabled="!onboardingKey.trim() || onboardingSaving" @click="saveOnboardingKey">
                {{ onboardingSaving ? t('chat.onboardSaving') : t('chat.onboardSave') }}
              </button>
            </div>
            <p v-if="onboardingError" class="onboarding-error">{{ onboardingError }}</p>
            <div class="onboarding-foot">
              <router-link to="/settings" class="onboarding-ghost-link">{{ t('chat.onboardToSettings') }}</router-link>
              <button class="onboarding-later" @click="onboardingDismissed = true">
                {{ t('chat.onboardLater') }}
              </button>
            </div>
          </div>

          <!-- 历史消息：按提问分组为多版本问答对。每个提问只展示一次，
               其下可有多个答案版本，用 ‹ i/N › 在版本间切换（旧版本保留不删除）。 -->
          <template v-for="turn in turns.turns" :key="`turn-${turn.id}`">
            <!-- 提问气泡（用户消息） -->
            <MessageItem
              :message="turn.userMessage"
              @delete-turn="handleDeleteTurn"
              @follow-up="handleFollowUp"
              @fork="handleForkAt"
            />

            <template v-if="turn.versions.length">
              <!-- 版本导航：仅当存在多个答案版本时显示 -->
              <div v-if="turn.versions.length > 1" class="version-nav">
                <button
                  class="icon-btn"
                  :disabled="versionIndex(turn) === 0"
                  :title="t('chat.versionPrev')"
                  @click="prevVersion(turn)"
                >
                  &lt;
                </button>
                <span class="version-label">
                  {{ t('chat.versionLabel').replace('{0}', String(versionIndex(turn) + 1)).replace('{1}', String(turn.versions.length)) }}
                </span>
                <button
                  class="icon-btn"
                  :disabled="versionIndex(turn) === turn.versions.length - 1"
                  :title="t('chat.versionNext')"
                  @click="nextVersion(turn)"
                >
                  &gt;
                </button>
              </div>

              <!-- 重新生成中提示 -->
              <div v-if="chatStore.regeneratingParentId === turn.id" class="version-regenerating">
                {{ t('chat.regenerating') }}
              </div>

              <!-- 当前版本的执行过程（思维链 / 工具调用） -->
              <div
                v-if="turn.versions[versionIndex(turn)]?.steps?.length"
                class="history-process-row"
              >
                <ProcessTrace :steps="turn.versions[versionIndex(turn)].steps" />
              </div>

              <!-- 当前版本的回答 -->
              <MessageItem
                :message="turn.versions[versionIndex(turn)].answer"
                @retry="handleRetry"
                @follow-up="handleFollowUp"
                @fork="handleForkAt"
              />
            </template>
          </template>

          <!-- 系统消息单独渲染（不属于任何提问轮次） -->
          <template v-for="sys in turns.standalone" :key="`sys-${sys.id}`">
            <MessageItem :message="sys" />
          </template>

          <!-- 流式渲染中：思维链 + 工具调用统一进「执行过程」框实时展示；
               最终答案开始输出时过程框自动收缩（可点击展开回看） -->
          <div v-if="chatStore.isStreaming && (chatStore.processEvents.length || chatStore.streamingContent)" class="streaming-message">
            <div class="message-avatar assistant-avatar">
              <svg width="20" height="20" viewBox="0 0 24 24" fill="none">
                <path d="M12 2L2 7l10 5 10-5-10-5z" stroke="currentColor" stroke-width="2" stroke-linejoin="round"/>
                <path d="M2 17l10 5 10-5M2 12l10 5 10-5" stroke="currentColor" stroke-width="2" stroke-linejoin="round"/>
              </svg>
            </div>
            <div class="streaming-body">
              <ProcessTrace
                v-if="chatStore.processEvents.length"
                :steps="chatStore.processEvents"
                live
                :collapse-trigger="chatStore.streamingContent.length > 0"
              />

              <!-- 正文回复（仅最终答案；中间说明已被移交进过程框） -->
              <div v-if="chatStore.streamingContent" class="streaming-content">
                <MarkdownRenderer :content="chatStore.streamingContent" /><span class="streaming-cursor"></span>
              </div>
            </div>
          </div>

          <!-- 错误提示 -->
          <div v-if="chatStore.error" class="error-banner">
            <svg class="error-icon" width="20" height="20" viewBox="0 0 24 24" fill="none">
              <path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z" stroke="currentColor" stroke-width="2" stroke-linejoin="round"/>
              <path d="M12 9v4M12 17h.01" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>
            </svg>
            <span>{{ chatStore.error }}</span>
          </div>
        </div>
      </div>

      <!-- 向下按钮：上滑查看历史时出现（固定在聊天区内，不随内容滚动） -->
      <button
        v-if="!isAtBottom"
        class="scroll-down-btn"
        @click="handleScrollDown"
        :title="chatStore.isStreaming ? '回到最新回复' : '跳到下一条提问'"
      >
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
          <path d="M12 5v14M5 12l7 7 7-7" />
        </svg>
      </button>

      <!-- Input area -->
      <!-- 人工确认弹窗：AI 请求执行受权限管控的工具 -->
      <div v-if="confirmRequest" class="confirm-overlay">
        <div class="confirm-dialog">
          <div class="confirm-header">
            <span :class="['confirm-icon-wrap', 'risk-' + confirmRequest.risk]">
              <svg class="confirm-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z" />
                <line x1="12" y1="9" x2="12" y2="13" /><line x1="12" y1="17" x2="12.01" y2="17" />
              </svg>
            </span>
            <div class="confirm-header-text">
              <h3 class="confirm-title">AI 请求执行工具</h3>
              <p class="confirm-subtitle">该工具受权限策略管控，需要你确认后才会执行</p>
            </div>
            <span :class="['risk-badge', 'risk-' + confirmRequest.risk]">
              {{ riskLabel(confirmRequest.risk) }}
            </span>
          </div>
          <div class="confirm-body">
            <div class="confirm-field">
              <span class="confirm-label">工具</span>
              <code class="confirm-tool">{{ confirmRequest.tool_name }}</code>
            </div>
            <div v-if="confirmRequest.reason" class="confirm-field">
              <span class="confirm-label">原因</span>
              <span class="confirm-reason">{{ confirmRequest.reason }}</span>
            </div>
            <div class="confirm-field confirm-field-block">
              <span class="confirm-label">参数</span>
              <pre class="confirm-args">{{ formattedArgs(confirmRequest.args) }}</pre>
            </div>
          </div>
          <div class="confirm-actions">
            <button class="btn-ghost" @click="replyConfirm(false)">拒绝</button>
            <button class="btn-primary" @click="replyConfirm(true)">允许执行</button>
          </div>
        </div>
      </div>

      <div class="input-area">
        <!-- 追问引用条：提示当前是在对某条回答追问 -->
        <div v-if="followUpQuote" class="followup-strip">
          <svg class="followup-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <polyline points="9 17 4 12 9 7" /><path d="M20 18v-2a4 4 0 0 0-4-4H4" />
          </svg>
          <div class="followup-text">
            <span class="followup-label">{{ t('chat.followUpLabel') }}</span>
            <span v-if="followUpQuote.question" class="followup-q">「{{ followUpQuote.question.slice(0, 60) }}{{ followUpQuote.question.length > 60 ? '…' : '' }}」</span>
            <span class="followup-hint">{{ t('chat.followUpHint') }}</span>
          </div>
          <button class="icon-btn" :title="t('chat.followUpCancel')" @click="clearFollowUp">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" />
            </svg>
          </button>
        </div>
        <div :class="['input-wrapper', { 'can-expand': canExpand }]">
          <!-- 待发送附件预览条 -->
          <!-- P1-1：服务端确认附件未被采纳时的可见提示（否则用户以为文件已附上） -->
          <div v-if="chatStore.attachmentWarning" class="attach-warning">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z" /><line x1="12" y1="9" x2="12" y2="13" /><line x1="12" y1="17" x2="12.01" y2="17" />
            </svg>
            <span class="attach-warning-text">{{ chatStore.attachmentWarning }}</span>
            <button class="attach-warning-close" title="关闭" @click="chatStore.attachmentWarning = ''">
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
                <line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" />
              </svg>
            </button>
          </div>

          <div v-if="chatStore.pendingAttachments.length" class="attach-strip">
            <div
              v-for="att in chatStore.pendingAttachments"
              :key="att.id"
              class="attach-chip"
            >
              <AttachmentImage
                v-if="att.kind === 'image' && chatStore.currentSessionId"
                class="attach-thumb"
                :session-id="chatStore.currentSessionId"
                :attachment-id="att.id"
                :filename="att.filename"
              />
              <span v-else class="attach-doc-icon">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                  <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/>
                </svg>
              </span>
              <span class="attach-name" :title="att.filename">{{ att.filename }}</span>
              <button class="attach-remove" :title="t('chat.attachmentRemove')" @click="removeAttachment(att.id)">
                <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
                  <line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" />
                </svg>
              </button>
            </div>
          </div>

          <textarea
            ref="textareaRef"
            v-model="inputText"
            :class="['input-textarea', { expanded: isExpanded }]"
            :placeholder="t('chat.placeholder')"
            @keydown.enter.exact.prevent="handleSend"
            :disabled="chatStore.isStreaming"
            rows="1"
          />
          <!-- 放大 / 缩小按钮：内容超过 7 行时出现在输入框右上角 -->
          <button
            v-if="canExpand"
            class="btn-expand"
            :title="isExpanded ? t('chat.collapseInput') : t('chat.expandInput')"
            @click="toggleExpand"
          >
            <svg v-if="!isExpanded" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <polyline points="15 3 21 3 21 9" /><polyline points="9 21 3 21 3 15" />
              <line x1="21" y1="3" x2="14" y2="10" /><line x1="3" y1="21" x2="10" y2="14" />
            </svg>
            <svg v-else width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <polyline points="4 14 10 14 10 20" /><polyline points="20 10 14 10 14 4" />
              <line x1="14" y1="10" x2="21" y2="3" /><line x1="3" y1="21" x2="10" y2="14" />
            </svg>
          </button>
          <button
            class="btn-attach"
            :title="t('chat.attachTitle')"
            :disabled="chatStore.isStreaming"
            @click="openFilePicker"
          >
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48" />
            </svg>
          </button>
          <button
            v-if="!chatStore.isStreaming"
            class="btn-send"
            v-ripple
            @click="handleSend"
            :disabled="(!inputText.trim() && chatStore.pendingAttachments.length === 0) || !chatStore.currentSessionId"
          >
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none">
              <path d="M22 2L11 13M22 2l-7 20-4-9-9-4 20-7z" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
            </svg>
            <span>{{ t('chat.send') }}</span>
          </button>
          <button v-else class="btn-stop" v-ripple @click="handleStop">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none">
              <rect x="6" y="6" width="12" height="12" rx="2" fill="currentColor"/>
            </svg>
            <span>{{ t('chat.stop') }}</span>
          </button>
          <input
            ref="fileInput"
            type="file"
            multiple
            class="hidden-file-input"
            @change="handleFileSelect"
          />
        </div>

        <!-- AI 生成内容提示条：常驻输入框下方 -->
        <div class="ai-notice-bar">
          <span class="ai-notice-text">{{ t('chat.aiGeneratedNotice') }}</span>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.chat-view {
  display: flex;
  height: 100vh;
  overflow: hidden;
}

.chat-main {
  flex: 1;
  display: flex;
  flex-direction: column;
  background: var(--bg-main);
  min-width: 0;
  position: relative;
}

/* ── Header ── */
.chat-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 0 var(--space-lg);
  height: var(--header-height);
  border-bottom: 1px solid var(--border-color);
  background: var(--bg-surface);
  flex-shrink: 0;
}

.header-title {
  display: flex;
  align-items: center;
  gap: var(--space-sm);
  min-width: 0;
}

.brand-title {
  font-size: var(--font-size-md);
  font-weight: 700;
  color: var(--color-primary);
  white-space: nowrap;
  letter-spacing: -0.02em;
}

.title-separator {
  color: var(--color-text-tertiary);
  flex-shrink: 0;
}

.header-title h2 {
  font-size: var(--font-size-md);
  font-weight: 600;
  color: var(--color-text);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.header-controls {
  display: flex;
  gap: var(--space-sm);
  align-items: center;
}

.mock-toggle {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 6px 12px;
  border-radius: var(--radius-md);
  border: 1px solid var(--border-color);
  background: var(--bg-surface);
  color: var(--color-text-secondary);
  font-size: var(--font-size-sm);
  font-weight: 500;
  cursor: pointer;
  transition: var(--transition-base);
  white-space: nowrap;
}

.mock-toggle:hover {
  border-color: var(--color-primary);
  color: var(--color-primary);
}

.mock-toggle.active {
  background: var(--color-primary-light);
  border-color: var(--color-primary);
  color: var(--color-primary);
  font-weight: 600;
}

.select-wrapper {
  position: relative;
  display: flex;
  align-items: center;
}

.provider-select {
  appearance: none;
  -webkit-appearance: none;
  padding: 6px 32px 6px 12px;
  border-radius: var(--radius-md);
  border: 1px solid var(--border-color);
  font-size: var(--font-size-sm);
  font-family: var(--font-sans);
  background: var(--bg-input);
  color: var(--color-text);
  cursor: pointer;
  transition: border-color var(--transition-base), box-shadow var(--transition-base);
}

.provider-select:hover {
  border-color: var(--color-primary);
}

.provider-select:focus {
  border-color: var(--color-primary);
  box-shadow: 0 0 0 3px var(--color-primary-light);
}

.select-arrow {
  position: absolute;
  right: 10px;
  pointer-events: none;
  color: var(--color-text-secondary);
}

/* 模型区三态提示文字（未配置 / 全部停用） */
.model-area-hint {
  font-size: var(--font-size-xs);
  color: var(--color-text-secondary);
  padding: 6px 12px;
  border: 1px dashed var(--border-color);
  border-radius: var(--radius-md);
  white-space: nowrap;
  cursor: help;
}

/* ── AI 生成内容提示条（常驻输入框下方，无底色、文字居中） ── */
.ai-notice-bar {
  display: flex;
  align-items: center;
  justify-content: center;
  max-width: var(--content-max-width);
  margin: 4px auto 0;
  color: var(--color-text-tertiary);
  font-size: var(--font-size-xs);
  line-height: 1.3;
  text-align: center;
}

.ai-notice-text {
  font-weight: 400;
}

/* ── 工具箱下拉（5 大功能入口） ── */
.toolbox-wrap {
  position: relative;
  display: flex;
  align-items: center;
}

.toolbox-wrap .btn-settings.active {
  background: var(--bg-hover);
  color: var(--color-primary);
}

.toolbox-backdrop {
  position: fixed;
  inset: 0;
  z-index: 90;
}

.toolbox-menu {
  position: absolute;
  right: 0;
  top: calc(100% + 6px);
  z-index: 100;
  min-width: 190px;
  padding: 6px;
  background: var(--bg-surface);
  border: 1px solid var(--border-strong);
  border-radius: var(--radius-md);
  box-shadow: var(--shadow-xl);
  display: flex;
  flex-direction: column;
  animation: fadeIn var(--transition-fast) ease-out;
}

.toolbox-item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 9px 12px;
  border-radius: var(--radius-sm);
  color: var(--color-text);
  text-decoration: none;
  font-size: var(--font-size-sm);
  transition: background var(--transition-fast), color var(--transition-fast);
}

.toolbox-item:hover {
  background: var(--bg-hover);
  color: var(--color-primary);
}

/* ── Connection status ── */
.conn-status {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: var(--font-size-xs);
  padding: 4px 10px;
  border-radius: var(--radius-full);
  font-weight: 500;
  transition: var(--transition-base);
}

.conn-dot {
  width: 8px;
  height: 8px;
  border-radius: var(--radius-full);
  flex-shrink: 0;
}

.conn-status.status-connected {
  background: var(--color-success-light);
  color: var(--color-success);
}
.conn-status.status-connected .conn-dot {
  background: var(--color-success);
}

.conn-status.status-connecting,
.conn-status.status-reconnecting {
  background: var(--color-warning-light);
  color: var(--color-warning);
}
.conn-status.status-connecting .conn-dot,
.conn-status.status-reconnecting .conn-dot {
  background: var(--color-warning);
  animation: pulse 1.2s ease-in-out infinite;
}

.conn-status.status-disconnected {
  background: var(--color-danger-light);
  color: var(--color-danger);
}
.conn-status.status-disconnected .conn-dot {
  background: var(--color-danger);
}

/* ── Settings button ── */
.btn-settings,
.btn-logout {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 32px;
  height: 32px;
  border-radius: var(--radius-md);
  color: var(--color-text-secondary);
  transition: var(--transition-base);
  flex-shrink: 0;
  border: none;
  background: transparent;
  cursor: pointer;
}

.btn-settings:hover,
.btn-logout:hover {
  background: var(--bg-hover);
  color: var(--color-primary);
}

.btn-settings:active {
  transform: rotate(60deg);
}

/* ── Messages container ── */
.messages-container {
  flex: 1;
  overflow-y: auto;
  padding: var(--space-lg) var(--space-md);
  position: relative;
}

/* 向下按钮：定位在聊天区内（相对 chat-main），浮于输入区上方，不随内容滚动 */
.scroll-down-btn {
  position: absolute;
  right: 20px;
  bottom: 110px;
  width: 40px;
  height: 40px;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  background: var(--color-primary);
  color: #fff;
  border: none;
  cursor: pointer;
  box-shadow: 0 4px 14px rgba(0, 0, 0, 0.3);
  z-index: 10;
  transition: transform var(--transition-base);
}

.scroll-down-btn:hover {
  transform: scale(1.1);
}

.scroll-down-btn:active {
  transform: scale(0.95);
}

.messages-inner {
  max-width: var(--content-max-width);
  margin: 0 auto;
  width: 100%;
}

/* ── 首次使用引导卡（配置 API Key） ── */
.onboarding-card {
  margin: var(--space-md) 0 var(--space-lg);
  padding: var(--space-md) var(--space-lg);
  background: var(--bg-surface);
  border: 1px solid var(--color-primary);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-md), 0 0 24px rgba(99, 102, 241, 0.12);
  animation: slideUp var(--transition-base) ease-out;
}

.onboarding-head {
  display: flex;
  align-items: flex-start;
  gap: var(--space-sm);
}

.onboarding-icon {
  width: 22px;
  height: 22px;
  color: var(--color-primary);
  flex-shrink: 0;
  margin-top: 2px;
}

.onboarding-title {
  margin: 0;
  font-size: var(--font-size-md);
  font-weight: 700;
  color: var(--color-text);
}

.onboarding-desc {
  margin: 4px 0 0;
  font-size: var(--font-size-sm);
  line-height: 1.6;
  color: var(--color-text-secondary);
}

.onboarding-link {
  color: var(--color-primary);
  text-decoration: none;
  font-weight: 600;
  white-space: nowrap;
}

.onboarding-link:hover {
  text-decoration: underline;
}

.onboarding-form {
  display: flex;
  gap: var(--space-sm);
  margin-top: var(--space-md);
}

.onboarding-input {
  flex: 1;
  min-width: 0;
  padding: 9px 12px;
  border: 1px solid var(--border-color);
  border-radius: var(--radius-md);
  background: var(--bg-input);
  color: var(--color-text);
  font-size: var(--font-size-sm);
  font-family: var(--font-mono, monospace);
  transition: border-color var(--transition-base), box-shadow var(--transition-base);
}

.onboarding-input:focus {
  outline: none;
  border-color: var(--color-primary);
  box-shadow: 0 0 0 3px var(--color-primary-light);
}

.onboarding-save {
  flex-shrink: 0;
  padding: 9px 18px;
  border: none;
  border-radius: var(--radius-md);
  background: linear-gradient(135deg, var(--color-primary), var(--color-primary-dark));
  color: var(--color-text-inverse);
  font-size: var(--font-size-sm);
  font-weight: 600;
  cursor: pointer;
  transition: transform var(--transition-base), box-shadow var(--transition-base), opacity var(--transition-base);
}

.onboarding-save:hover:not(:disabled) {
  transform: translateY(-1px);
  box-shadow: var(--shadow-primary);
}

.onboarding-save:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.onboarding-error {
  margin: var(--space-sm) 0 0;
  font-size: var(--font-size-xs);
  color: var(--color-danger);
}

.onboarding-foot {
  display: flex;
  align-items: center;
  gap: var(--space-md);
  margin-top: var(--space-md);
}

.onboarding-ghost-link {
  font-size: var(--font-size-xs);
  color: var(--color-text-secondary);
  text-decoration: none;
}

.onboarding-ghost-link:hover {
  color: var(--color-primary);
}

.onboarding-later {
  margin-left: auto;
  border: none;
  background: transparent;
  padding: 2px 6px;
  font-size: var(--font-size-xs);
  color: var(--color-text-tertiary);
  cursor: pointer;
}

.onboarding-later:hover {
  color: var(--color-text-secondary);
}

/* ── Streaming message ── */
.streaming-message {
  display: flex;
  gap: var(--space-md);
  margin: var(--space-md) 0;
  animation: slideUp var(--transition-base) ease-out;
}

.assistant-avatar {
  width: 36px;
  height: 36px;
  border-radius: var(--radius-full);
  background: linear-gradient(135deg, var(--color-primary), var(--color-primary-dark));
  color: var(--color-text-inverse);
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  box-shadow: var(--shadow-sm);
}

.streaming-body {
  flex: 1;
  min-width: 0;
  padding-top: 4px;
}

/* 历史消息中的「执行过程」行：与 assistant 消息体左对齐（留出头像位） */
.history-process-row {
  margin: var(--space-sm) 0;
  margin-left: 48px;
  max-width: calc(100% - 48px);
}

/* ── 重新回答版本导航（‹ i/N ›） ── */
.version-nav {
  display: flex;
  align-items: center;
  gap: var(--space-xs);
  margin: var(--space-xs) 0 0;
  margin-left: 48px;
  max-width: calc(100% - 48px);
}

.version-nav .icon-btn {
  width: 26px;
  height: 26px;
  border: 1px solid var(--border-color);
  border-radius: var(--radius-md);
  background: var(--bg-surface);
  color: var(--color-text-secondary);
  font-size: var(--font-size-md);
  font-weight: 700;
  line-height: 1;
  opacity: 1;
}

.version-nav .icon-btn:hover:not(:disabled) {
  border-color: var(--color-primary);
  color: var(--color-primary);
  background: var(--bg-hover);
}

.version-nav .icon-btn:disabled {
  opacity: 0.35;
  cursor: not-allowed;
}

.version-label {
  font-size: var(--font-size-xs);
  font-weight: 600;
  color: var(--color-text-secondary);
  font-family: var(--font-mono);
  padding: 2px 8px;
  border-radius: var(--radius-full);
  background: var(--bg-tag);
  white-space: nowrap;
}

.version-regenerating {
  margin: var(--space-xs) 0 0;
  margin-left: 48px;
  max-width: calc(100% - 48px);
  font-size: var(--font-size-xs);
  color: var(--color-primary);
  display: inline-flex;
  align-items: center;
  gap: 6px;
}

.version-regenerating::before {
  content: '';
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--color-primary);
  animation: pulse 1.2s ease-in-out infinite;
}

.streaming-content {
  /* 流式正文直接显示为与完成后一致的 assistant 气泡，
     避免「裸文字 → 完成后突然变气泡」的视觉跳变。 */
  background: var(--bg-assistant);
  border: 1px solid var(--border-color);
  border-radius: var(--radius-lg) var(--radius-lg) var(--radius-lg) var(--radius-sm);
  padding: 12px 16px;
  box-shadow: var(--shadow-sm);
  line-height: 1.7;
  font-size: var(--font-size-base);
  color: var(--color-text);
  margin-top: var(--space-sm);
  word-break: break-word;
}

.streaming-cursor {
  display: inline-block;
  width: 10px;
  height: 1.1em;
  background: linear-gradient(180deg, var(--color-primary) 0%, var(--color-primary-dark) 100%);
  border-radius: 2px;
  margin-left: 2px;
  vertical-align: text-bottom;
  animation: blink 1s step-end infinite;
  box-shadow: 0 0 8px var(--color-primary), 0 0 16px rgba(99, 102, 241, 0.3);
}

/* ── Error banner ── */
.error-banner {
  display: flex;
  align-items: center;
  gap: 10px;
  margin: var(--space-md) 0;
  padding: 12px var(--space-md);
  background: var(--bg-error);
  border: 1px solid var(--border-error);
  border-radius: var(--radius-md);
  font-size: var(--font-size-sm);
  color: var(--color-danger);
  animation: slideUp var(--transition-base) ease-out;
}

.error-icon {
  color: var(--color-danger);
  flex-shrink: 0;
}

/* ── 任务清单 ── */
.todo-panel {
  margin: 0 var(--space-md);
  padding: var(--space-sm) var(--space-md);
  background: var(--bg-surface);
  border: 1px solid var(--border-light);
  border-radius: var(--radius-md);
  flex-shrink: 0;
}

.todo-header {
  display: flex;
  align-items: center;
  gap: var(--space-xs);
  margin-bottom: var(--space-xs);
}

.todo-icon {
  width: 16px;
  height: 16px;
  color: var(--color-text-secondary);
}

.todo-title {
  font-size: var(--font-size-sm);
  font-weight: 600;
  color: var(--color-text);
}

.todo-progress {
  font-size: var(--font-size-xs);
  color: var(--color-text-secondary);
}

.todo-clear {
  margin-left: auto;
  font-size: var(--font-size-xs);
  padding: 2px 8px;
}

.todo-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
  max-height: 160px;
  overflow-y: auto;
}

.todo-item {
  display: flex;
  align-items: center;
  gap: var(--space-xs);
  font-size: var(--font-size-sm);
  color: var(--color-text);
  line-height: 1.6;
}

.todo-mark {
  width: 14px;
  text-align: center;
  flex-shrink: 0;
}

.todo-content {
  flex: 1;
  min-width: 0;
  word-break: break-word;
}

.todo-status {
  font-size: var(--font-size-xs);
  color: var(--color-text-secondary);
  flex-shrink: 0;
}

.todo-pending .todo-mark {
  color: var(--color-text-secondary);
}

.todo-in_progress .todo-mark {
  color: var(--color-warning, #d97706);
}

.todo-in_progress .todo-content {
  font-weight: 600;
}

.todo-completed .todo-mark {
  color: var(--color-success, #16a34a);
}

.todo-completed .todo-content {
  color: var(--color-text-secondary);
  text-decoration: line-through;
}

/* ── 人工确认弹窗 ── */
.confirm-overlay {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.62);
  backdrop-filter: blur(3px);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 1000;
  padding: var(--space-md);
  animation: fadeIn var(--transition-fast) ease-out;
}

.confirm-dialog {
  width: min(600px, 100%);
  max-height: 82vh;
  overflow-y: auto;
  /* 实色背景：原来的 --bg-card / --bg-primary 两个变量都不存在，
     会 fallback 成透明，导致文字与按钮淹没在页面里。 */
  background: var(--bg-surface);
  border: 1px solid var(--border-strong);
  border-top: 3px solid var(--color-warning);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-xl);
  padding: var(--space-lg);
  animation: slideUp var(--transition-base) ease-out;
}

.confirm-header {
  display: flex;
  align-items: flex-start;
  gap: var(--space-md);
  margin-bottom: var(--space-lg);
}

.confirm-icon-wrap {
  width: 40px;
  height: 40px;
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: var(--radius-md);
  background: var(--color-warning-light);
  color: var(--color-warning);
}

.confirm-icon-wrap.risk-read {
  background: var(--color-success-light);
  color: var(--color-success);
}

.confirm-icon-wrap.risk-write {
  background: var(--color-warning-light);
  color: var(--color-warning);
}

.confirm-icon-wrap.risk-dangerous {
  background: var(--color-danger-light);
  color: var(--color-danger);
}

.confirm-icon {
  width: 22px;
  height: 22px;
}

.confirm-header-text {
  flex: 1;
  min-width: 0;
}

.confirm-title {
  margin: 0;
  font-size: var(--font-size-lg);
  font-weight: 700;
  color: var(--color-text);
}

.confirm-subtitle {
  margin: 3px 0 0;
  font-size: var(--font-size-xs);
  line-height: 1.5;
  color: var(--color-text-secondary);
}

.risk-badge {
  align-self: center;
  flex-shrink: 0;
  font-size: var(--font-size-xs);
  font-weight: 700;
  padding: 3px 10px;
  border-radius: var(--radius-full);
  border: 1px solid currentColor;
  white-space: nowrap;
}

.risk-badge.risk-read {
  color: var(--color-success);
  background: var(--color-success-light);
}

.risk-badge.risk-write {
  color: var(--color-warning);
  background: var(--color-warning-light);
}

.risk-badge.risk-dangerous {
  color: var(--color-danger);
  background: var(--color-danger-light);
}

.confirm-body {
  display: flex;
  flex-direction: column;
  gap: var(--space-md);
}

.confirm-field {
  display: flex;
  align-items: baseline;
  gap: var(--space-sm);
}

.confirm-field-block {
  flex-direction: column;
  align-items: stretch;
  gap: var(--space-xs);
}

.confirm-label {
  font-size: var(--font-size-xs);
  font-weight: 700;
  letter-spacing: 0.04em;
  text-transform: uppercase;
  color: var(--color-text-tertiary);
  flex-shrink: 0;
}

.confirm-tool {
  font-family: var(--font-mono);
  font-size: var(--font-size-sm);
  font-weight: 600;
  color: var(--color-primary);
  background: var(--bg-tag);
  padding: 3px 10px;
  border-radius: var(--radius-sm);
}

.confirm-reason {
  font-size: var(--font-size-sm);
  line-height: 1.6;
  color: var(--color-text-secondary);
}

.confirm-args {
  margin: 0;
  padding: var(--space-md);
  background: var(--bg-code-block);
  border: 1px solid var(--border-color);
  border-radius: var(--radius-md);
  font-family: var(--font-mono);
  font-size: var(--font-size-xs);
  line-height: 1.65;
  color: var(--color-text);
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 260px;
  overflow-y: auto;
}

.confirm-actions {
  display: flex;
  justify-content: flex-end;
  gap: var(--space-md);
  margin-top: var(--space-lg);
  padding-top: var(--space-md);
  border-top: 1px solid var(--border-light);
}

/* ── 追问引用条 ── */
.followup-strip {
  max-width: var(--content-max-width);
  margin: 0 auto var(--space-sm);
  display: flex;
  align-items: center;
  gap: var(--space-sm);
  padding: var(--space-sm) var(--space-md);
  background: var(--color-warning-light);
  border: 1px solid var(--color-warning);
  border-radius: var(--radius-md);
  font-size: var(--font-size-xs);
}

.followup-icon {
  width: 16px;
  height: 16px;
  color: var(--color-warning);
  flex-shrink: 0;
}

.followup-text {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 6px;
}

.followup-label {
  font-weight: 700;
  color: var(--color-warning);
}

.followup-q {
  color: var(--color-text);
}

.followup-hint {
  color: var(--color-text-secondary);
}

/* ── Input area ── */
.input-area {
  padding: var(--space-sm) var(--space-md) var(--space-sm);
  border-top: 1px solid var(--border-color);
  background: var(--bg-surface);
  flex-shrink: 0;
}

.input-wrapper {
  max-width: var(--content-max-width);
  margin: 0 auto;
  display: flex;
  flex-wrap: wrap;
  align-items: flex-end;
  gap: var(--space-sm);
  background: var(--bg-input);
  border: 1px solid var(--border-color);
  border-radius: var(--radius-xl);
  padding: 6px 6px 6px 14px;
  box-shadow: var(--shadow-sm);
  transition: border-color var(--transition-base), box-shadow var(--transition-base);
  position: relative;
}

/* P1-1：附件未被采纳的提示条 */
.attach-warning {
  display: flex;
  align-items: center;
  gap: var(--space-xs);
  max-width: var(--content-max-width);
  margin: 0 auto var(--space-xs);
  padding: 6px 10px;
  background: var(--bg-surface);
  border: 1px solid var(--border-color);
  border-left: 3px solid #d89614;
  border-radius: var(--radius-md);
  color: var(--color-text);
  font-size: var(--font-size-xs);
  line-height: 1.5;
}

.attach-warning svg {
  flex-shrink: 0;
  color: #d89614;
}

.attach-warning-text {
  flex: 1;
  min-width: 0;
}

.attach-warning-close {
  flex-shrink: 0;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 20px;
  height: 20px;
  padding: 0;
  border: none;
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--color-text-muted, #888);
  cursor: pointer;
}

.attach-warning-close:hover {
  background: var(--bg-input);
}

/* ── 附件预览条 ── */
.attach-strip {
  flex-basis: 100%;
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-xs);
  margin-bottom: var(--space-xs);
}

.attach-chip {
  display: flex;
  align-items: center;
  gap: 6px;
  max-width: 220px;
  padding: 4px 6px 4px 8px;
  background: var(--bg-surface);
  border: 1px solid var(--border-color);
  border-radius: var(--radius-md);
}

.attach-thumb {
  width: 26px;
  height: 26px;
  object-fit: cover;
  border-radius: 4px;
  flex-shrink: 0;
}

.attach-doc-icon {
  display: flex;
  align-items: center;
  color: var(--color-primary);
  flex-shrink: 0;
}

.attach-name {
  font-size: var(--font-size-xs);
  color: var(--color-text);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  max-width: 150px;
}

.attach-remove {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 18px;
  height: 18px;
  border: none;
  border-radius: 50%;
  background: transparent;
  color: var(--color-text-secondary);
  cursor: pointer;
  flex-shrink: 0;
}

.attach-remove:hover {
  background: var(--bg-hover);
  color: var(--color-danger);
}

.hidden-file-input {
  display: none;
}

.btn-attach {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 38px;
  height: 38px;
  border: none;
  border-radius: var(--radius-md);
  background: transparent;
  color: var(--color-text-secondary);
  cursor: pointer;
  flex-shrink: 0;
  transition: background var(--transition-base), color var(--transition-base);
}

.btn-attach:hover:not(:disabled) {
  background: var(--bg-hover);
  color: var(--color-primary);
}

.btn-attach:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.input-wrapper:focus-within {
  border-color: var(--color-primary);
  box-shadow: var(--shadow-md), 0 0 0 3px var(--color-primary-light);
}

.input-textarea {
  flex: 1;
  border: none;
  background: transparent;
  font-size: var(--font-size-base);
  font-family: var(--font-sans);
  color: var(--color-text);
  resize: none;
  line-height: 1.6;
  padding: 4px 0;
  overflow-y: hidden;
  /* 高度由 JS 动态设置（3~7 行自适应 / 放大整页），加过渡更平滑 */
  transition: height 0.12s ease;
  min-height: 0;
}

/* 放大状态：占据页面大部分高度，内部滚动 */
.input-textarea.expanded {
  overflow-y: auto;
}

/* ── 放大 / 缩小按钮（输入框右上角） ── */
.btn-expand {
  position: absolute;
  top: 6px;
  right: 8px;
  z-index: 2;
  display: flex;
  align-items: center;
  justify-content: center;
  width: 26px;
  height: 26px;
  border: 1px solid var(--border-color);
  border-radius: var(--radius-md);
  background: var(--bg-surface);
  color: var(--color-text-secondary);
  cursor: pointer;
  box-shadow: var(--shadow-sm);
  transition: background var(--transition-base), color var(--transition-base),
    border-color var(--transition-base);
}

.btn-expand:hover {
  background: var(--bg-hover);
  color: var(--color-primary);
  border-color: var(--color-primary);
}

/* 放大按钮可见时，为文本与附件条预留右上角空间，避免遮挡 */
.input-wrapper.can-expand .input-textarea {
  padding-right: 34px;
}

.input-wrapper.can-expand .attach-strip {
  padding-right: 34px;
}

.input-textarea::placeholder {
  color: var(--color-text-tertiary);
}

.input-textarea:focus {
  outline: none;
}

.input-textarea:disabled {
  opacity: 0.6;
}

/* ── Send button ── */
.btn-send {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 8px 18px;
  border: none;
  border-radius: var(--radius-lg);
  background: linear-gradient(135deg, var(--color-primary), var(--color-primary-dark));
  color: var(--color-text-inverse);
  font-size: var(--font-size-sm);
  font-weight: 600;
  font-family: var(--font-sans);
  cursor: pointer;
  transition: transform var(--transition-base), box-shadow var(--transition-base), opacity var(--transition-base);
  box-shadow: var(--shadow-primary);
  white-space: nowrap;
}

.btn-send:hover:not(:disabled) {
  transform: translateY(-2px);
  box-shadow: var(--shadow-lg), var(--shadow-primary);
}

.btn-send:active:not(:disabled) {
  transform: translateY(0);
}

.btn-send:disabled {
  opacity: 0.5;
  cursor: not-allowed;
  box-shadow: none;
}

/* ── Stop button ── */
.btn-stop {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 8px 18px;
  border: none;
  border-radius: var(--radius-lg);
  background: var(--color-danger);
  color: var(--color-text-inverse);
  font-size: var(--font-size-sm);
  font-weight: 600;
  font-family: var(--font-sans);
  cursor: pointer;
  transition: transform var(--transition-base), background var(--transition-base);
  animation: pulse 1.5s ease-in-out infinite;
  white-space: nowrap;
}

.btn-stop:hover {
  background: var(--color-danger-hover);
  transform: translateY(-2px);
}

.btn-stop:active {
  transform: translateY(0);
}

/* ── Responsive ── */
@media (max-width: 768px) {
  .chat-header {
    padding: 0 var(--space-sm);
  }

  .header-title h2 {
    font-size: var(--font-size-sm);
    max-width: 100px;
  }

  .header-controls {
    gap: 4px;
  }

  .conn-status .conn-text {
    display: none;
  }

  .messages-container {
    padding: var(--space-sm);
  }

  .input-area {
    padding: var(--space-sm);
  }

  .input-wrapper {
    border-radius: var(--radius-lg);
    padding: 6px 6px 6px 12px;
  }

  .input-textarea {
    font-size: var(--font-size-sm);
  }

  .btn-send span,
  .btn-stop span {
    display: none;
  }

  .btn-send,
  .btn-stop {
    padding: 8px 10px;
  }

  .streaming-message {
    gap: var(--space-sm);
  }

  .assistant-avatar {
    width: 30px;
    height: 30px;
  }

  /* 移动端：历史过程行与消息体对齐（头像 30px + 间距） */
  .history-process-row {
    margin-left: 38px;
    max-width: calc(100% - 38px);
  }

  .version-nav,
  .version-regenerating {
    margin-left: 38px;
    max-width: calc(100% - 38px);
  }
}
</style>
