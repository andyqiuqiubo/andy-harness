<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { useChatStore } from '../stores/chat'
import { usePluginLoaderStore } from '../stores/plugin-loader'
import { useLanguage } from '../composables/useLanguage'

const chatStore = useChatStore()
const pluginLoaderStore = usePluginLoaderStore()
const { t } = useLanguage()

// M16: Show archived toggle
const showArchived = ref(false)

// 右键上下文菜单
const contextMenu = ref<{ visible: boolean; x: number; y: number; sessionId: string }>({
  visible: false, x: 0, y: 0, sessionId: '',
})

// M16: Filter sessions by archived state
const visibleSessions = computed(() => {
  return chatStore.sessions.filter((s) => showArchived.value || !s.archived)
})

// Plugin menu items for sidebar injection
const pluginMenuItems = computed(() => pluginLoaderStore.menuItems)

async function handleNewSession() {
  await chatStore.createSession('New Session')
}

async function handleRename(id: string) {
  const session = chatStore.sessions.find(s => s.id === id)
  const title = window.prompt(t.value('sessions.rename'), session?.title || '')
  if (title) {
    await chatStore.renameSession(id, title)
  }
}

async function handleArchive(id: string) {
  await chatStore.archiveSession(id)
  contextMenu.value.visible = false
}

async function handleDelete(id: string) {
  if (window.confirm(t.value('sessions.confirmDelete'))) {
    await chatStore.deleteSession(id)
  }
  contextMenu.value.visible = false
}

async function handleDeleteAll() {
  if (window.confirm(t.value('sessions.confirmDeleteAll'))) {
    for (const s of chatStore.sessions) {
      await chatStore.deleteSession(s.id)
    }
  }
}

// 右键菜单
function handleContextMenu(e: MouseEvent, sessionId: string) {
  e.preventDefault()
  contextMenu.value = {
    visible: true,
    x: e.clientX,
    y: e.clientY,
    sessionId,
  }
}

function closeContextMenu() {
  contextMenu.value.visible = false
}

// 关闭右键菜单（点击其他区域）
onMounted(() => {
  chatStore.loadSessions()
  document.addEventListener('click', closeContextMenu)
})
</script>

<template>
  <div class="session-sidebar">
    <!-- Gradient header -->
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
      <button
        v-if="chatStore.sessions.length > 0"
        class="btn-clear"
        @click="handleDeleteAll"
        :title="t('sessions.deleteAll')"
      >
        <span class="btn-clear-icon">🗑</span>
        <span>{{ t('sessions.deleteAll') }}</span>
      </button>
    </div>

    <!-- Archive toggle as a modern pill switch -->
    <div class="archive-toggle">
      <button
        class="toggle-pill"
        :class="{ active: showArchived }"
        @click="showArchived = !showArchived"
      >
        <span class="toggle-dot"></span>
        <span class="toggle-text">{{ showArchived ? t('sessions.hideArchived') : t('sessions.showArchived') }}</span>
      </button>
    </div>

    <!-- Session list -->
    <div class="session-list">
      <div
        v-for="session in visibleSessions"
        :key="session.id"
        :class="['session-item', { active: session.id === chatStore.currentSessionId, archived: session.archived }]"
        v-ripple="session.id === chatStore.currentSessionId ? 'rgba(255,255,255,0.2)' : 'rgba(99,102,241,0.15)'"
        @click="chatStore.selectSession(session.id)"
        @dblclick="handleRename(session.id)"
        @contextmenu="handleContextMenu($event, session.id)"
      >
        <span class="session-title" :title="session.title">
          {{ session.title.length > 20 ? session.title.slice(0, 20) + '...' : session.title }}
          <span v-if="session.archived" class="archived-badge">{{ t('sessions.archived') }}</span>
        </span>
        <div class="session-actions">
          <button class="action-btn" @click.stop="handleRename(session.id)" :title="t('sessions.rename')">✏</button>
          <button class="action-btn" @click.stop="handleArchive(session.id)" :title="t('sessions.archive')">📦</button>
          <button class="action-btn action-delete" @click.stop="handleDelete(session.id)" :title="t('sessions.delete')">✕</button>
        </div>
      </div>

      <!-- Empty state -->
      <div v-if="visibleSessions.length === 0" class="empty-state">
        <div class="empty-icon">📝</div>
        <p class="empty-text">{{ t('sessions.empty') }}</p>
      </div>
    </div>

    <!-- 右键上下文菜单 -->
    <teleport to="body">
      <div
        v-if="contextMenu.visible"
        class="context-menu"
        :style="{ left: contextMenu.x + 'px', top: contextMenu.y + 'px' }"
        @click.stop
      >
        <button class="ctx-item" @click="handleRename(contextMenu.sessionId); contextMenu.visible = false">
          <span class="ctx-icon">✏</span> {{ t('sessions.rename') }}
        </button>
        <button class="ctx-item" @click="handleArchive(contextMenu.sessionId)">
          <span class="ctx-icon">📦</span> {{ t('sessions.archive') }}
        </button>
        <div class="ctx-divider"></div>
        <button class="ctx-item ctx-danger" @click="handleDelete(contextMenu.sessionId)">
          <span class="ctx-icon">✕</span> {{ t('sessions.delete') }}
        </button>
      </div>
    </teleport>

    <!-- Plugin nav section -->
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

/* ── Sidebar header ── */
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

/* ── New session button ── */
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

.btn-new:active {
  transform: translateY(0);
}

.btn-icon-plus {
  font-size: 1.1rem;
  line-height: 1;
  font-weight: 600;
}

/* ── Clear all button (ghost/danger) ── */
.btn-clear {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: var(--space-xs);
  padding: var(--space-xs) var(--space-sm);
  background: transparent;
  border: 1px solid rgba(239, 68, 68, 0.4);
  border-radius: var(--radius-sm);
  color: var(--color-danger);
  font-size: var(--font-size-xs);
  font-weight: 500;
  cursor: pointer;
  transition: background var(--transition-base), border-color var(--transition-base), color var(--transition-base);
}

.btn-clear:hover {
  background: var(--color-danger);
  border-color: var(--color-danger);
  color: #fff;
}

.btn-clear-icon {
  font-size: 0.8rem;
}

/* ── Archive toggle (pill switch) ── */
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
  transition: background var(--transition-base), color var(--transition-base), border-color var(--transition-base);
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
  padding: var(--space-sm) var(--space-sm);
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
  transition: background var(--transition-base), transform var(--transition-base), border-color var(--transition-base), box-shadow var(--transition-base);
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

/* ── Session action buttons ── */
.session-actions {
  display: flex;
  gap: 2px;
  opacity: 0;
  transform: translateX(8px);
  transition: opacity var(--transition-base), transform var(--transition-base);
  pointer-events: none;
}

.session-item:hover .session-actions {
  opacity: 1;
  transform: translateX(0);
  pointer-events: auto;
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
  font-size: 0.8rem;
  cursor: pointer;
  transition: background var(--transition-fast), color var(--transition-fast);
}

.action-btn:hover {
  background: var(--bg-hover);
  color: var(--color-text);
}

.action-delete {
  color: var(--color-danger);
}

.action-delete:hover {
  background: var(--color-danger-light);
  color: var(--color-danger-hover);
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

/* ── Plugin nav section ── */
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
  transition: background var(--transition-base), color var(--transition-base);
}

.plugin-nav-item:hover {
  background: var(--bg-hover);
  color: var(--color-text);
}

.plugin-nav-icon {
  font-size: 0.95rem;
  line-height: 1;
}

.plugin-nav-label {
  font-weight: 450;
}

/* ── 右键上下文菜单 ── */
.context-menu {
  position: fixed;
  z-index: 10000;
  min-width: 160px;
  padding: 4px;
  background: var(--bg-surface);
  border: 1px solid var(--border-color);
  border-radius: var(--radius-md);
  box-shadow: var(--shadow-xl);
  animation: contextMenuIn 150ms cubic-bezier(0.4, 0, 0.2, 1);
}

@keyframes contextMenuIn {
  from { opacity: 0; transform: scale(0.95) translateY(-4px); }
  to { opacity: 1; transform: scale(1) translateY(0); }
}

.ctx-item {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  padding: 8px 12px;
  border: none;
  background: none;
  color: var(--color-text);
  font-size: var(--font-size-sm);
  border-radius: var(--radius-sm);
  cursor: pointer;
  transition: background var(--transition-fast), color var(--transition-fast);
}

.ctx-item:hover {
  background: var(--bg-hover);
}

.ctx-item.ctx-danger:hover {
  background: var(--color-error-bg);
  color: var(--color-danger);
}

.ctx-icon {
  font-size: 0.9rem;
  width: 16px;
  text-align: center;
}

.ctx-divider {
  height: 1px;
  background: var(--border-color);
  margin: 4px 0;
}
</style>
