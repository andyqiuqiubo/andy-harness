<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { useChatStore } from '../stores/chat'
import { usePluginLoaderStore } from '../stores/plugin-loader'
import { useLanguage } from '../composables/useLanguage'
import { apiClient } from '../api/client'
import type { Session } from '../api/types'

const chatStore = useChatStore()
const pluginLoaderStore = usePluginLoaderStore()
const { t, language } = useLanguage()

// 显示归档
const showArchived = ref(false)

// ── 搜索（E9：会话标题 / 消息内容） ──────────────────
interface SearchMatch {
  message_id: string
  role: string
  snippet: string
  created_at: string
}
interface SearchResult {
  session: Session
  title_hit: boolean
  msg_hits: number
  matches: SearchMatch[]
}

const searchQuery = ref('')
const searchResults = ref<SearchResult[]>([])
const searching = ref(false)
let searchTimer: ReturnType<typeof setTimeout> | null = null

async function runSearch(q: string) {
  const term = q.trim()
  if (!term) {
    searchResults.value = []
    searching.value = false
    return
  }
  searching.value = true
  try {
    const params = new URLSearchParams({ q: term })
    searchResults.value = await apiClient.get<SearchResult[]>(
      `/sessions/search?${params.toString()}`,
    )
  } catch {
    searchResults.value = []
  } finally {
    searching.value = false
  }
}

function onSearchInput() {
  if (searchTimer) clearTimeout(searchTimer)
  searchTimer = setTimeout(() => {
    void runSearch(searchQuery.value)
  }, 300)
}

function clearSearch() {
  if (searchTimer) clearTimeout(searchTimer)
  searchQuery.value = ''
  searchResults.value = []
  searching.value = false
}

async function openSearchResult(result: SearchResult) {
  await chatStore.selectSession(result.session.id)
  clearSearch()
}


// 轻提示（3 秒自动消失）：用于「导入成功 / 导出成功 / 分叉成功」等反馈
const toastText = ref('')
let toastTimer: ReturnType<typeof setTimeout> | null = null

function showToast(text: string) {
  toastText.value = text
  if (toastTimer) clearTimeout(toastTimer)
  toastTimer = setTimeout(() => {
    toastText.value = ''
  }, 3000)
}

// 会话操作菜单（点 ⋮ 或右键打开）
const menu = ref<{ visible: boolean; x: number; y: number; sessionId: string }>({
  visible: false, x: 0, y: 0, sessionId: '',
})

const visibleSessions = computed(() =>
  chatStore.sessions.filter((s) => showArchived.value || !s.archived),
)

// ── 按「年月日」分组 + 排序（近期日期靠上）+ 可收缩/展开 ──────────
interface SessionGroup {
  key: string
  label: string
  sessions: Session[]
}

/** 从 ISO 时间串取本地日期键 YYYY-MM-DD（后端存的是本地朴素时间，无时区）。 */
function toDateKey(iso: string): string {
  const d = new Date(iso)
  if (isNaN(d.getTime())) return 'unknown'
  const y = d.getFullYear()
  const m = String(d.getMonth() + 1).padStart(2, '0')
  const day = String(d.getDate()).padStart(2, '0')
  return `${y}-${m}-${day}`
}

/** 分组标题：今天 / 昨天 / 本地化日期。 */
function toDateLabel(key: string): string {
  if (key === 'unknown') return t.value('sessions.unknownDate')
  const d = new Date(key + 'T00:00:00')
  const now = new Date()
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate())
  const target = new Date(d.getFullYear(), d.getMonth(), d.getDate())
  const diff = Math.round((today.getTime() - target.getTime()) / 86400000)
  if (diff === 0) return t.value('sessions.today')
  if (diff === 1) return t.value('sessions.yesterday')
  if (language.value === 'en') {
    return d.toLocaleDateString('en-US', { year: 'numeric', month: 'short', day: 'numeric' })
  }
  return `${d.getFullYear()}年${d.getMonth() + 1}月${d.getDate()}日`
}

/** 各日期分组的折叠状态（key → 是否折叠，默认展开）。 */
const collapsedGroups = ref<Record<string, boolean>>({})

function toggleGroup(key: string) {
  collapsedGroups.value[key] = !collapsedGroups.value[key]
}

const groupedSessions = computed<SessionGroup[]>(() => {
  const buckets = new Map<string, Session[]>()
  for (const s of visibleSessions.value) {
    const key = toDateKey(s.updated_at)
    const arr = buckets.get(key)
    if (arr) arr.push(s)
    else buckets.set(key, [s])
  }
  const groups: SessionGroup[] = []
  for (const [key, sessions] of buckets) {
    // 组内同样按最后活跃时间降序
    sessions.sort((a, b) => String(b.updated_at).localeCompare(String(a.updated_at)))
    groups.push({ key, label: toDateLabel(key), sessions })
  }
  // 日期降序：近期日期靠上
  groups.sort((a, b) => b.key.localeCompare(a.key))
  return groups
})

const pluginMenuItems = computed(() => pluginLoaderStore.menuItems)

const currentMenuSession = computed(
  () => chatStore.sessions.find((s) => s.id === menu.value.sessionId) || null,
)

async function handleNewSession() {
  await chatStore.loadSessions()
  const emptySession = chatStore.sessions.find(
    (s) => !s.archived && s.message_count === 0,
  )
  if (emptySession) {
    await chatStore.selectSession(emptySession.id)
    showToast(t.value('sessions.existingEmpty'))
    return
  }
  await chatStore.createSession('New Session')
}

function openMenu(e: MouseEvent, sessionId: string) {
  e.stopPropagation()
  const x = Math.min(e.clientX, window.innerWidth - 280)
  const y = Math.min(e.clientY, window.innerHeight - 300)
  menu.value = { visible: true, x, y, sessionId }
}

function closeMenu() {
  menu.value.visible = false
}

// ── 会话操作实现 ──────────────────────────────────────

async function handleRename(id: string) {
  const session = chatStore.sessions.find((s) => s.id === id)
  const title = window.prompt(t.value('sessions.rename'), session?.title || '')
  if (title) await chatStore.renameSession(id, title)
  closeMenu()
}

async function handleArchive(id: string) {
  await chatStore.archiveSession(id)
  closeMenu()
}

async function handleDelete(id: string) {
  if (window.confirm(t.value('sessions.confirmDelete'))) {
    await chatStore.deleteSession(id)
  }
  closeMenu()
}

function safeFileName(title: string) {
  const cleaned = (title || 'session').replace(/[\\/:*?"<>|]/g, '_').trim()
  return cleaned.slice(0, 60) || 'session'
}

/** 导出：下载为 JSON 文件（含会话元数据 + 全部消息 + 任务清单）。 */
async function handleExport(id: string) {
  try {
    const payload = await apiClient.get<Record<string, unknown>>(`/sessions/${id}/export`)
    const session = chatStore.sessions.find((s) => s.id === id)
    const blob = new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `${safeFileName(session?.title || 'session')}.json`
    a.click()
    URL.revokeObjectURL(url)
    showToast(t.value('sessions.exported'))
  } catch (e) {
    window.alert(t.value('sessions.exportFail') + ': ' + e)
  }
  closeMenu()
}

/** 分叉：复制当前会话为一份新会话（历史完整），并切换过去。 */
async function handleFork(id: string) {
  try {
    const forked = await chatStore.forkSession(id, null)
    showToast(`${t.value('sessions.forked')}：${forked.title}`)
  } catch (e) {
    window.alert(t.value('sessions.forkFail') + ': ' + e)
  }
  closeMenu()
}

// ── 导入 ──────────────────────────────────────────────
const fileInput = ref<HTMLInputElement | null>(null)

function triggerImport() {
  fileInput.value?.click()
}

async function handleImportFile(e: Event) {
  const input = e.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) return
  try {
    const text = await file.text()
    const payload = JSON.parse(text)
    if (!payload || typeof payload !== 'object' || !Array.isArray(payload.messages)) {
      throw new Error(t.value('sessions.importInvalid'))
    }
    const title = payload?.session?.title
      ? `${payload.session.title}（导入）`
      : t.value('sessions.importedTitle')
    const created = await chatStore.importSession(payload, title)
    showToast(`${t.value('sessions.imported')}：${created.title}`)
  } catch (err) {
    window.alert(t.value('sessions.importFail') + ': ' + err)
  } finally {
    input.value = ''
  }
}

async function handleDeleteAll() {
  if (!window.confirm(t.value('sessions.confirmDeleteAll'))) return
  for (const s of chatStore.sessions) {
    await chatStore.deleteSession(s.id)
  }
}

onMounted(() => {
  chatStore.loadSessions()
  document.addEventListener('click', closeMenu)
  document.addEventListener('scroll', closeMenu, true)
})

onUnmounted(() => {
  document.removeEventListener('click', closeMenu)
  document.removeEventListener('scroll', closeMenu, true)
  if (toastTimer) clearTimeout(toastTimer)
})
</script>

<template>
  <div class="session-sidebar">
    <div class="sidebar-header">
      <div class="header-top">
        <div class="header-brand">
          <span class="brand-icon">💬</span>
          <h3>{{ t('sessions.title') }}</h3>
        </div>
      </div>

      <button class="btn-new" @click="handleNewSession">
        <span class="btn-icon-plus">+</span>
        <span>{{ t('sessions.new') }}</span>
      </button>

      <div class="header-tools">
        <button class="tool-btn" @click="triggerImport" :title="t('sessions.importHint')">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" /><polyline points="7 10 12 15 17 10" /><line x1="12" y1="15" x2="12" y2="3" />
          </svg>
          <span>{{ t('sessions.import') }}</span>
        </button>
        <button
          v-if="chatStore.sessions.length > 0"
          class="tool-btn danger"
          @click="handleDeleteAll"
          :title="t('sessions.deleteAll')"
        >
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <polyline points="3 6 5 6 21 6" /><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6" /><path d="M10 11v6M14 11v6" />
          </svg>
          <span>{{ t('sessions.deleteAll') }}</span>
        </button>
      </div>

      <p class="header-hint">{{ t('sessions.hint') }}</p>
    </div>

    <div class="archive-toggle">
      <div class="search-box">
        <svg class="search-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <circle cx="11" cy="11" r="8" /><line x1="21" y1="21" x2="16.65" y2="16.65" />
        </svg>
        <input
          v-model="searchQuery"
          class="search-input"
          type="text"
          :placeholder="t('sessions.searchPlaceholder')"
          @input="onSearchInput"
          @keydown.esc="clearSearch"
        />
        <button v-if="searchQuery" class="search-clear" @click="clearSearch">×</button>
      </div>
      <button class="toggle-pill" :class="{ active: showArchived }" @click="showArchived = !showArchived">
        <span class="toggle-dot"></span>
        <span class="toggle-text">{{ showArchived ? t('sessions.hideArchived') : t('sessions.showArchived') }}</span>
      </button>
    </div>

    <div class="session-list">
      <!-- 搜索结果 -->
      <template v-if="searchQuery.trim()">
        <div v-if="searching" class="search-hint">{{ t('sessions.searching') }}</div>
        <template v-else-if="searchResults.length">
          <div
            v-for="result in searchResults"
            :key="result.session.id"
            class="search-result"
            @click="openSearchResult(result)"
          >
            <div class="search-result-title">
              <span class="search-result-name">{{ result.session.title }}</span>
              <span v-if="result.title_hit" class="search-badge">{{ t('sessions.searchTitleHit') }}</span>
            </div>
            <div
              v-for="match in result.matches"
              :key="match.message_id"
              class="search-snippet"
            >
              <span class="search-role">{{ match.role }}:</span>
              <span>{{ match.snippet }}</span>
            </div>
            <div v-if="result.msg_hits > result.matches.length" class="search-more">
              {{ t('sessions.searchMore').replace('{0}', String(result.msg_hits)) }}
            </div>
          </div>
        </template>
        <div v-else class="empty-state">
          <div class="empty-icon">🔍</div>
          <p class="empty-text">{{ t('sessions.searchEmpty') }}</p>
        </div>
      </template>

      <template v-else>
      <template v-for="group in groupedSessions" :key="group.key">
        <button
          class="group-header"
          :class="{ collapsed: collapsedGroups[group.key] }"
          @click="toggleGroup(group.key)"
        >
          <svg
            class="group-chevron"
            :class="{ collapsed: collapsedGroups[group.key] }"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            stroke-width="2"
            stroke-linecap="round"
            stroke-linejoin="round"
          >
            <path d="M9 18l6-6-6-6" />
          </svg>
          <span class="group-label">{{ group.label }}</span>
          <span class="group-count">{{ group.sessions.length }}</span>
        </button>
        <transition name="group-fade">
          <div v-show="!collapsedGroups[group.key]" class="group-body">
            <div
              v-for="session in group.sessions"
              :key="session.id"
              :class="['session-item', { active: session.id === chatStore.currentSessionId, archived: session.archived }]"
              v-ripple="session.id === chatStore.currentSessionId ? 'rgba(255,255,255,0.2)' : 'rgba(99,102,241,0.15)'"
              @click="chatStore.selectSession(session.id)"
              @dblclick="handleRename(session.id)"
              @contextmenu.prevent="openMenu($event, session.id)"
            >
              <span class="session-title" :title="session.title">
                {{ session.title.length > 20 ? session.title.slice(0, 20) + '...' : session.title }}
                <span v-if="session.archived" class="archived-badge">{{ t('sessions.archived') }}</span>
              </span>
              <div class="session-actions">
                <button
                  class="action-btn menu-btn"
                  :title="t('sessions.menu')"
                  @click.stop="openMenu($event, session.id)"
                >
                  <svg viewBox="0 0 24 24" fill="currentColor"><circle cx="12" cy="5" r="1.6"/><circle cx="12" cy="12" r="1.6"/><circle cx="12" cy="19" r="1.6"/></svg>
                </button>
              </div>
            </div>
          </div>
        </transition>
      </template>

      <div v-if="groupedSessions.length === 0" class="empty-state">
        <div class="empty-icon">📝</div>
        <p class="empty-text">{{ t('sessions.empty') }}</p>
      </div>
      </template>

      <input
        ref="fileInput"
        type="file"
        accept="application/json,.json"
        class="hidden-file-input"
        @change="handleImportFile"
      />
    </div>

    <!-- 会话操作菜单 -->
    <teleport to="body">
      <div
        v-if="menu.visible"
        class="session-menu"
        :style="{ left: menu.x + 'px', top: menu.y + 'px' }"
        @click.stop
      >
        <div class="menu-header">
          <span class="menu-title">{{ currentMenuSession?.title || t('sessions.menu') }}</span>
          <span class="menu-sub">{{ t('sessions.menuSub') }}</span>
        </div>
        <button class="menu-item" @click="handleRename(menu.sessionId)">
          <svg class="menu-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9" /><path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4z" /></svg>
          <span class="menu-text">
            <span class="menu-label">{{ t('sessions.rename') }}</span>
            <span class="menu-desc">{{ t('sessions.renameHint') }}</span>
          </span>
        </button>
        <button class="menu-item" @click="handleExport(menu.sessionId)">
          <svg class="menu-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" /><polyline points="7 10 12 15 17 10" /><line x1="12" y1="15" x2="12" y2="3" /></svg>
          <span class="menu-text">
            <span class="menu-label">{{ t('sessions.export') }}</span>
            <span class="menu-desc">{{ t('sessions.exportHint') }}</span>
          </span>
        </button>
        <button class="menu-item" @click="handleFork(menu.sessionId)">
          <svg class="menu-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="6" y1="3" x2="6" y2="15" /><circle cx="18" cy="6" r="3" /><circle cx="6" cy="18" r="3" /><path d="M18 9a9 9 0 0 1-9 9" /></svg>
          <span class="menu-text">
            <span class="menu-label">{{ t('sessions.fork') }}</span>
            <span class="menu-desc">{{ t('sessions.forkHint') }}</span>
          </span>
        </button>
        <button class="menu-item" @click="handleArchive(menu.sessionId)">
          <svg class="menu-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="4" width="20" height="5" rx="1" /><path d="M4 9v10a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1V9" /><line x1="10" y1="13" x2="14" y2="13" /></svg>
          <span class="menu-text">
            <span class="menu-label">{{ t('sessions.archive') }}</span>
            <span class="menu-desc">{{ t('sessions.archiveHint') }}</span>
          </span>
        </button>
        <div class="menu-divider"></div>
        <button class="menu-item danger" @click="handleDelete(menu.sessionId)">
          <svg class="menu-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6" /><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6" /><path d="M10 11v6M14 11v6" /></svg>
          <span class="menu-text">
            <span class="menu-label">{{ t('sessions.delete') }}</span>
            <span class="menu-desc">{{ t('sessions.deleteHint') }}</span>
          </span>
        </button>
      </div>
    </teleport>

    <!-- 轻提示 -->
    <teleport to="body">
      <transition name="toast-fade">
        <div v-if="toastText" class="session-toast">{{ toastText }}</div>
      </transition>
    </teleport>

    <div v-if="pluginMenuItems.length > 0" class="plugin-nav">
      <div class="plugin-nav-header">{{ t('nav.hello') }}</div>
      <router-link
        v-for="item in pluginMenuItems"
        :key="item.id"
        :to="pluginLoaderStore.activeRoutes.find((v) => v.id === item.view_id)?.route || '#'"
        class="plugin-nav-item"
      >
        <span class="plugin-nav-icon">🔌</span>
        <span class="plugin-nav-label">{{ item.label }}</span>
      </router-link>
    </div>
  </div>
</template>

<style scoped>
.session-sidebar {
  width: var(--sidebar-width, 260px);
  display: flex;
  flex-direction: column;
  height: 100%;
  background: var(--bg-sidebar);
  border-right: 1px solid var(--border-color);
  overflow: hidden;
}

/* ── Header ── */
.sidebar-header {
  padding: var(--space-md) var(--space-md) var(--space-sm);
  background: var(--bg-sidebar);
  border-bottom: 1px solid var(--border-color);
  display: flex;
  flex-direction: column;
  gap: var(--space-sm);
}

.header-top {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.header-brand {
  display: flex;
  align-items: center;
  gap: var(--space-sm);
}

.brand-icon {
  font-size: 1.1rem;
  line-height: 1;
}

.sidebar-header h3 {
  margin: 0;
  font-size: var(--font-size-md);
  font-weight: 600;
  color: var(--color-text);
  letter-spacing: -0.01em;
}

.btn-new {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: var(--space-xs);
  padding: var(--space-sm) var(--space-md);
  background: var(--color-primary);
  border: 1px solid var(--color-primary-dark);
  border-radius: var(--radius-md);
  color: var(--color-text-inverse);
  font-size: var(--font-size-sm);
  font-weight: 500;
  cursor: pointer;
  transition: background var(--transition-base), transform var(--transition-base), box-shadow var(--transition-base);
}

.btn-new:hover {
  background: var(--color-primary-dark);
  transform: translateY(-1px);
  box-shadow: var(--shadow-md);
}

.btn-icon-plus {
  font-size: 1.1rem;
  line-height: 1;
  font-weight: 600;
}

.header-tools {
  display: flex;
  gap: var(--space-sm);
}

.tool-btn {
  flex: 1;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 5px;
  padding: var(--space-xs) var(--space-sm);
  background: transparent;
  border: 1px solid var(--border-color);
  border-radius: var(--radius-sm);
  color: var(--color-text-secondary);
  font-size: var(--font-size-xs);
  font-weight: 500;
  cursor: pointer;
  transition: var(--transition-base);
}

.tool-btn svg {
  width: 14px;
  height: 14px;
}

.tool-btn:hover {
  background: var(--bg-hover);
  border-color: var(--color-primary);
  color: var(--color-primary);
}

.tool-btn.danger:hover {
  background: var(--color-danger-light);
  border-color: var(--color-danger);
  color: var(--color-danger);
}

.header-hint {
  margin: 0;
  font-size: 11px;
  line-height: 1.5;
  color: var(--color-text-tertiary);
}

/* ── Archive toggle ── */
.archive-toggle {
  padding: var(--space-sm) var(--space-md);
  border-bottom: 1px solid var(--border-color);
}

.toggle-pill {
  display: inline-flex;
  align-items: center;
  gap: var(--space-sm);
  padding: 5px 14px 5px 5px;
  border-radius: var(--radius-full);
  background: var(--bg-hover);
  border: 1px solid var(--border-color);
  font-size: var(--font-size-xs);
  color: var(--color-text-secondary);
  cursor: pointer;
  transition: var(--transition-base);
}

.toggle-pill.active {
  background: var(--color-primary-light);
  color: var(--color-primary);
  border-color: var(--color-primary);
}

.toggle-dot {
  width: 14px;
  height: 14px;
  border-radius: 50%;
  background: var(--bg-slider);
  transition: background var(--transition-base);
  flex-shrink: 0;
}

.toggle-pill.active .toggle-dot {
  background: var(--color-primary);
}

.toggle-text {
  font-weight: 500;
}

/* ── Session list ── */
.session-list {
  flex: 1;
  overflow-y: auto;
  padding: var(--space-sm);
}

.session-item {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: var(--space-sm) var(--space-md);
  margin-bottom: 2px;
  cursor: pointer;
  border-radius: var(--radius-md);
  border-left: 3px solid transparent;
  transition: var(--transition-base);
  animation: slideInLeft var(--transition-base) ease both;
}

.session-item:hover {
  background: var(--bg-hover);
  transform: translateX(2px);
}

.session-item.active {
  background: var(--bg-active);
  border-left-color: var(--color-primary);
  box-shadow: var(--shadow-xs);
}

.session-item.archived {
  opacity: 0.55;
}

.session-title {
  font-size: var(--font-size-sm);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  flex: 1;
  color: var(--color-text);
  font-weight: 450;
}

.session-item.active .session-title {
  font-weight: 600;
  color: var(--color-primary);
}

.archived-badge {
  display: inline-block;
  font-size: 0.65rem;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.03em;
  color: var(--color-warning);
  background: var(--color-warning-light);
  padding: 1px 6px;
  border-radius: var(--radius-full);
  margin-left: var(--space-xs);
  vertical-align: middle;
}

.session-actions {
  display: flex;
  gap: 2px;
  opacity: 0;
  transition: opacity var(--transition-base);
}

.session-item:hover .session-actions,
.session-item.active .session-actions {
  opacity: 1;
}

.action-btn {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 26px;
  height: 26px;
  border: none;
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--color-text-secondary);
  cursor: pointer;
  transition: var(--transition-fast);
}

.action-btn svg {
  width: 16px;
  height: 16px;
}

.action-btn:hover {
  background: var(--bg-active);
  color: var(--color-primary);
}

/* ── 日期分组头 ── */
.group-header {
  display: flex;
  align-items: center;
  gap: var(--space-sm);
  width: 100%;
  padding: var(--space-xs) var(--space-sm);
  margin-top: var(--space-xs);
  background: transparent;
  border: none;
  cursor: pointer;
  color: var(--color-text-tertiary);
  font-size: 11px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  transition: var(--transition-base);
}

.group-header:hover {
  color: var(--color-text-secondary);
}

.group-chevron {
  width: 14px;
  height: 14px;
  flex-shrink: 0;
  transition: transform var(--transition-base);
}

.group-chevron.collapsed {
  transform: rotate(-90deg);
}

.group-label {
  flex: 1;
  text-align: left;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.group-count {
  font-size: 10px;
  line-height: 1;
  background: var(--bg-hover);
  color: var(--color-text-tertiary);
  padding: 2px 7px;
  border-radius: var(--radius-full);
  font-weight: 600;
  letter-spacing: 0;
}

.group-body {
  display: flex;
  flex-direction: column;
}

.group-fade-enter-active,
.group-fade-leave-active {
  transition: opacity var(--transition-fast);
}

.group-fade-enter-from,
.group-fade-leave-to {
  opacity: 0;
}

/* ── Empty state ── */
.empty-state {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: var(--space-xl) var(--space-md);
  text-align: center;
}

.empty-icon {
  font-size: 2.5rem;
  opacity: 0.4;
  margin-bottom: var(--space-sm);
}

.empty-text {
  font-size: var(--font-size-sm);
  color: var(--color-text-tertiary);
}

.hidden-file-input {
  display: none;
}

/* ── Plugin nav ── */
.plugin-nav {
  border-top: 1px solid var(--border-color);
  padding: var(--space-sm) 0;
}

.plugin-nav-header {
  padding: var(--space-xs) var(--space-md) var(--space-sm);
  font-size: var(--font-size-xs);
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  color: var(--color-text-tertiary);
}

.plugin-nav-item {
  display: flex;
  align-items: center;
  gap: var(--space-sm);
  padding: var(--space-sm) var(--space-md);
  color: var(--color-text-secondary);
  font-size: var(--font-size-sm);
  transition: var(--transition-base);
}

.plugin-nav-item:hover {
  background: var(--bg-hover);
  color: var(--color-text);
}

.plugin-nav-icon {
  font-size: 0.95rem;
  line-height: 1;
}

/* ── 会话操作菜单 ── */
.session-menu {
  position: fixed;
  z-index: 10000;
  width: 268px;
  padding: 6px;
  background: var(--bg-surface);
  border: 1px solid var(--border-strong);
  border-radius: var(--radius-md);
  box-shadow: var(--shadow-xl);
  animation: contextMenuIn 150ms cubic-bezier(0.4, 0, 0.2, 1);
}

@keyframes contextMenuIn {
  from { opacity: 0; transform: scale(0.96) translateY(-4px); }
  to { opacity: 1; transform: scale(1) translateY(0); }
}

.menu-header {
  padding: 6px 10px 8px;
  display: flex;
  flex-direction: column;
  gap: 2px;
  border-bottom: 1px solid var(--border-light);
  margin-bottom: 4px;
}

.menu-title {
  font-size: var(--font-size-sm);
  font-weight: 600;
  color: var(--color-text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.menu-sub {
  font-size: 11px;
  color: var(--color-text-tertiary);
}

.menu-item {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  width: 100%;
  padding: 8px 10px;
  border: none;
  background: none;
  text-align: left;
  border-radius: var(--radius-sm);
  cursor: pointer;
  transition: var(--transition-fast);
}

.menu-item:hover {
  background: var(--bg-hover);
}

.menu-item.danger:hover {
  background: var(--color-error-bg);
  color: var(--color-danger);
}

.menu-icon {
  width: 16px;
  height: 16px;
  flex-shrink: 0;
  margin-top: 2px;
  color: var(--color-text-secondary);
}

.menu-item:hover .menu-icon {
  color: var(--color-primary);
}

.menu-item.danger:hover .menu-icon {
  color: var(--color-danger);
}

.menu-text {
  display: flex;
  flex-direction: column;
  gap: 1px;
  min-width: 0;
}

.menu-label {
  font-size: var(--font-size-sm);
  font-weight: 500;
  color: var(--color-text);
}

.menu-desc {
  font-size: 11px;
  line-height: 1.45;
  color: var(--color-text-tertiary);
}

.menu-item.danger:hover .menu-label {
  color: var(--color-danger);
}

.menu-divider {
  height: 1px;
  background: var(--border-light);
  margin: 4px 0;
}

/* ── 轻提示 ── */
.session-toast {
  position: fixed;
  top: 24px;
  left: 50%;
  transform: translateX(-50%);
  z-index: 10001;
  padding: 10px 18px;
  background: var(--color-primary);
  color: var(--color-text-inverse);
  font-size: var(--font-size-sm);
  font-weight: 500;
  border-radius: var(--radius-full);
  box-shadow: var(--shadow-lg);
  max-width: 80vw;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.toast-fade-enter-active,
.toast-fade-leave-active {
  transition: opacity var(--transition-base), transform var(--transition-base);
}

.toast-fade-enter-from,
.toast-fade-leave-to {
  opacity: 0;
  transform: translateX(-50%) translateY(-8px);
}

/* ── 搜索（E9） ── */
.search-box {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 4px 8px;
  background: var(--bg-surface);
  border: 1px solid var(--border-color);
  border-radius: var(--radius-md);
  transition: border-color var(--transition-fast), box-shadow var(--transition-fast);
}

.search-box:focus-within {
  border-color: var(--color-primary);
  box-shadow: 0 0 0 2px var(--color-primary-light);
}

.search-icon {
  width: 14px;
  height: 14px;
  color: var(--color-text-tertiary);
  flex-shrink: 0;
}

.search-input {
  flex: 1;
  min-width: 0;
  border: none;
  outline: none;
  background: transparent;
  font-size: var(--font-size-sm);
  color: var(--color-text);
  padding: 3px 0;
}

.search-input::placeholder {
  color: var(--color-text-tertiary);
}

.search-clear {
  border: none;
  background: var(--bg-hover);
  color: var(--color-text-secondary);
  width: 18px;
  height: 18px;
  border-radius: 50%;
  font-size: 13px;
  line-height: 1;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
}

.search-hint {
  padding: var(--space-md);
  font-size: var(--font-size-sm);
  color: var(--color-text-tertiary);
  text-align: center;
}

.search-result {
  padding: 8px 10px;
  margin-bottom: 4px;
  border: 1px solid var(--border-light);
  border-radius: var(--radius-md);
  cursor: pointer;
  background: var(--bg-surface);
  transition: var(--transition-fast);
}

.search-result:hover {
  border-color: var(--color-primary);
  background: var(--bg-hover);
}

.search-result-title {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 4px;
}

.search-result-name {
  font-size: var(--font-size-sm);
  font-weight: 600;
  color: var(--color-text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.search-badge {
  font-size: 10px;
  font-weight: 600;
  color: var(--color-primary);
  background: var(--color-primary-light);
  padding: 1px 6px;
  border-radius: var(--radius-full);
  flex-shrink: 0;
}

.search-snippet {
  font-size: 11px;
  line-height: 1.5;
  color: var(--color-text-secondary);
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

.search-role {
  color: var(--color-text-tertiary);
  margin-right: 4px;
}

.search-more {
  font-size: 10px;
  color: var(--color-text-tertiary);
  margin-top: 3px;
}
</style>
