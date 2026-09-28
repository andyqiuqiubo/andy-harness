<script setup lang="ts">
import { computed, ref } from 'vue'
import type { Message, TokenUsage } from '../api/types'
import MarkdownRenderer from './MarkdownRenderer.vue'

const props = defineProps<{ message: Message }>()

const emit = defineEmits<{
  (e: 'delete-turn', message: Message): void
  (e: 'follow-up', message: Message): void
  (e: 'fork', message: Message): void
}>()

// 复制反馈：短暂显示「已复制」
const copied = ref(false)

async function copyContent() {
  const text = props.message.content || ''
  if (!text) return
  try {
    await navigator.clipboard.writeText(text)
  } catch {
    // 回退：非安全上下文 / 无剪贴板权限时用临时 textarea
    const ta = document.createElement('textarea')
    ta.value = text
    ta.style.position = 'fixed'
    ta.style.opacity = '0'
    document.body.appendChild(ta)
    ta.select()
    try {
      document.execCommand('copy')
    } finally {
      ta.remove()
    }
  }
  copied.value = true
  window.setTimeout(() => {
    copied.value = false
  }, 1200)
}

// 是否在消息流中可见：
// - tool 角色消息仅用于模型 API 上下文，其结果已在 assistant 的
//   tool_calls 卡片中展示，不单独渲染（避免孤立的工具头像行）
// - assistant 消息既无正文也无工具调用时不渲染（避免空白气泡/空行）
const isVisible = computed(() => {
  const m = props.message
  if (m.role === 'tool') return false
  if (m.role === 'assistant' && !m.content && !(m.tool_calls && m.tool_calls.length)) {
    return false
  }
  return true
})

// 输入 token 中命中缓存的量（兼容 DeepSeek / OpenAI 两种字段名）
function usageCacheHit(u: TokenUsage | undefined): number {
  if (!u?.prompt_tokens_details) return 0
  const d = u.prompt_tokens_details
  return d.prompt_cache_hit_tokens ?? d.cached_tokens ?? 0
}

// 输入 token 中未命中缓存的量
function usageCacheMiss(u: TokenUsage | undefined): number {
  if (!u?.prompt_tokens_details) return 0
  const d = u.prompt_tokens_details
  return d.prompt_cache_miss_tokens ?? (u.prompt_tokens - usageCacheHit(u))
}
</script>

<template>
  <div v-if="isVisible" :class="['message-row', message.role]">
    <!-- User messages: avatar on right, content aligned right -->
    <template v-if="message.role === 'user'">
      <div class="message-body user-body">
        <div class="msg-actions">
          <button class="icon-btn" :title="copied ? '已复制' : '复制提问'" @click="copyContent">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <rect x="9" y="9" width="13" height="13" rx="2" /><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" />
            </svg>
          </button>
          <button class="icon-btn danger" title="删除本轮问答（物理删除）" @click="emit('delete-turn', message)">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <polyline points="3 6 5 6 21 6" /><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6" /><path d="M10 11v6M14 11v6" /><path d="M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2" />
            </svg>
          </button>
        </div>
        <div class="message-bubble user-bubble">
          <div v-if="message.attachments && message.attachments.length" class="msg-attachments">
            <img
              v-for="att in message.attachments.filter(a => a.kind === 'image')"
              :key="att.id"
              class="msg-attach-img"
              :src="`/api/sessions/${message.session_id}/attachments/${att.id}`"
              :alt="att.filename"
              :title="att.filename"
            />
            <div
              v-for="att in message.attachments.filter(a => a.kind === 'document')"
              :key="att.id"
              class="msg-attach-doc"
              :title="att.filename"
            >
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/>
              </svg>
              <span class="msg-attach-doc-name">{{ att.filename }}</span>
            </div>
          </div>
          <MarkdownRenderer v-if="message.content" :content="message.content" />
        </div>
        <div class="message-avatar user-avatar">U</div>
      </div>
    </template>

    <!-- Assistant / system / tool messages: avatar on left, content aligned left -->
    <template v-else>
      <div class="message-avatar assistant-avatar" v-if="message.role === 'assistant'">
        <svg width="20" height="20" viewBox="0 0 24 24" fill="none">
          <path d="M12 2L2 7l10 5 10-5-10-5z" stroke="currentColor" stroke-width="2" stroke-linejoin="round"/>
          <path d="M2 17l10 5 10-5M2 12l10 5 10-5" stroke="currentColor" stroke-width="2" stroke-linejoin="round"/>
        </svg>
      </div>
      <div class="message-avatar system-avatar" v-else-if="message.role === 'system'">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none">
          <circle cx="12" cy="12" r="10" stroke="currentColor" stroke-width="2"/>
          <path d="M12 8v4M12 16h.01" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>
        </svg>
      </div>
      <div class="message-avatar tool-avatar" v-else>
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none">
          <path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
        </svg>
      </div>
      <div class="message-body assistant-body">
        <!-- 仅当正文非空时渲染气泡（空正文的纯工具调用轮次只显示工具卡片） -->
        <div
          class="message-bubble assistant-bubble"
          v-if="(message.role === 'assistant' || message.role === 'system') && message.content"
        >
          <MarkdownRenderer :content="message.content" />
        </div>

        <!-- 回答的操作条：复制 / 追问（仅 assistant 回答） -->
        <div class="msg-actions" v-if="message.role === 'assistant' && message.content">
          <button class="icon-btn" :title="copied ? '已复制' : '复制回答'" @click="copyContent">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <rect x="9" y="9" width="13" height="13" rx="2" /><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" />
            </svg>
          </button>
          <button class="icon-btn" title="针对该回答继续追问" @click="emit('follow-up', message)">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <polyline points="9 17 4 12 9 7" /><path d="M20 18v-2a4 4 0 0 0-4-4H4" />
            </svg>
          </button>
          <button class="icon-btn" title="从该回答处分叉出新会话" @click="emit('fork', message)">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <line x1="6" y1="3" x2="6" y2="15" /><circle cx="18" cy="6" r="3" /><circle cx="6" cy="18" r="3" /><path d="M18 9a9 9 0 0 1-9 9" />
            </svg>
          </button>
        </div>

        <!-- 本次对话的 token 用量（DeepSeek 官方 usage），每条回答各自保留 -->
        <div v-if="message.role === 'assistant' && message.usage" class="message-usage">
          <span class="usage-label">本次用量</span>
          <span class="usage-item">输入 {{ message.usage.prompt_tokens }}</span>
          <span v-if="usageCacheHit(message.usage) > 0" class="usage-item usage-muted">(缓存 {{ usageCacheHit(message.usage) }} · 未命中 {{ usageCacheMiss(message.usage) }})</span>
          <span class="usage-item">· 输出 {{ message.usage.completion_tokens }}</span>
          <span class="usage-item usage-total">· 合计 {{ message.usage.total_tokens }} tokens</span>
        </div>

        <!-- Tool calls -->
        <div v-if="message.tool_calls && message.tool_calls.length" class="tool-calls">
          <details v-for="(tc, i) in message.tool_calls" :key="i" class="tool-card">
            <summary class="tool-summary">
              <svg class="tool-icon" width="16" height="16" viewBox="0 0 24 24" fill="none">
                <path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
              </svg>
              <span class="tool-name">{{ tc.tool_name }}</span>
              <svg class="tool-chevron" width="14" height="14" viewBox="0 0 24 24" fill="none">
                <path d="M9 18l6-6-6-6" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
              </svg>
            </summary>
            <div class="tool-detail">
              <div class="tool-arg-row">
                <span class="tool-label">参数:</span>
                <code class="tool-result-code">{{ JSON.stringify(tc.args) }}</code>
              </div>
              <div class="tool-arg-row">
                <span class="tool-label">结果:</span>
                <code class="tool-result-code">{{ tc.result }}</code>
              </div>
              <div v-if="tc.error" class="tool-arg-row tool-error-row">
                <span class="tool-label">错误:</span>
                <code class="tool-result-code">{{ tc.error }}</code>
              </div>
            </div>
          </details>
        </div>
      </div>
    </template>
  </div>
</template>

<style scoped>
/* ── 消息操作条（复制 / 删除本轮 / 追问），悬浮或聚焦时显示 ── */
.msg-actions {
  display: flex;
  align-items: center;
  gap: 2px;
  flex-shrink: 0;
  opacity: 0;
  transition: opacity var(--transition-fast);
}

.message-row:hover .msg-actions,
.msg-actions:focus-within {
  opacity: 1;
}

.icon-btn.danger:hover {
  background: var(--color-danger-light);
  color: var(--color-danger);
}

/* ── 消息行布局 ── */
.message-row {
  display: flex;
  margin: var(--space-md) 0;
  animation: slideUp var(--transition-base) ease-out;
}

/* User messages: right aligned */
.message-row.user {
  justify-content: flex-end;
}

.user-body {
  display: flex;
  align-items: flex-start;
  gap: var(--space-sm);
  max-width: 85%;
}

.user-bubble {
  background: var(--bg-user);
  padding: 10px 16px;
  border-radius: var(--radius-lg) var(--radius-lg) var(--radius-sm) var(--radius-lg);
  color: var(--color-text);
  font-size: var(--font-size-base);
  line-height: 1.6;
  box-shadow: var(--shadow-sm);
  word-break: break-word;
}

/* ── 用户消息附件 ── */
.msg-attachments {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-xs);
  margin-bottom: var(--space-sm);
}

.msg-attach-img {
  max-width: 180px;
  max-height: 180px;
  border-radius: var(--radius-sm);
  object-fit: cover;
  border: 1px solid var(--border-color);
}

.msg-attach-doc {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 4px 10px;
  background: var(--bg-surface);
  border: 1px solid var(--border-color);
  border-radius: var(--radius-md);
  font-size: var(--font-size-xs);
  color: var(--color-text);
  max-width: 220px;
}

.msg-attach-doc-name {
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  max-width: 180px;
}

/* Assistant / system / tool messages: left aligned */
.message-row.assistant,
.message-row.system,
.message-row.tool {
  justify-content: flex-start;
}

.assistant-body {
  display: flex;
  flex-direction: column;
  gap: var(--space-sm);
  flex: 1;
  min-width: 0;
  max-width: calc(100% - 48px);
}

.assistant-bubble {
  background: var(--bg-assistant);
  padding: 12px 16px;
  border-radius: var(--radius-lg) var(--radius-lg) var(--radius-lg) var(--radius-sm);
  border: 1px solid var(--border-color);
  color: var(--color-text);
  font-size: var(--font-size-base);
  line-height: 1.7;
  box-shadow: var(--shadow-sm);
  word-break: break-word;
}

/* 本次对话的 token 用量 */
.message-usage {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  align-self: flex-start;
  font-family: var(--font-mono);
  font-size: var(--font-size-xs);
  color: var(--color-text-secondary);
  background: var(--bg-code);
  border: 1px solid var(--border-color);
  border-radius: var(--radius-full);
  padding: 3px 10px;
  white-space: nowrap;
}

.usage-label {
  color: var(--color-primary);
  font-weight: 600;
}

.usage-item {
  white-space: nowrap;
}

.usage-total {
  color: var(--color-text);
  font-weight: 600;
}

.usage-muted {
  color: var(--color-text-tertiary);
}

/* System messages */
.message-row.system .assistant-bubble {
  background: var(--bg-success);
  border-color: var(--color-success-light);
  font-size: var(--font-size-sm);
  color: var(--color-text-secondary);
  font-style: italic;
}

/* ── Avatars ── */
.message-avatar {
  width: 36px;
  height: 36px;
  border-radius: var(--radius-full);
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  font-size: var(--font-size-sm);
  font-weight: 700;
  box-shadow: var(--shadow-sm);
}

.user-avatar {
  background: linear-gradient(135deg, var(--color-primary), var(--color-primary-dark));
  color: var(--color-text-inverse);
}

.assistant-avatar {
  background: linear-gradient(135deg, var(--color-primary), var(--color-primary-dark));
  color: var(--color-text-inverse);
}

.system-avatar {
  background: var(--color-success-light);
  color: var(--color-success);
}

.tool-avatar {
  background: var(--color-warning-light);
  color: var(--color-warning);
}

/* ── Tool call cards ── */
.tool-calls {
  display: flex;
  flex-direction: column;
  gap: var(--space-sm);
}

.tool-card {
  border: 1px solid var(--border-color);
  border-radius: var(--radius-md);
  background: var(--bg-tool);
  overflow: hidden;
  transition: border-color var(--transition-base), box-shadow var(--transition-base);
}

.tool-card:hover {
  border-color: var(--color-warning);
  box-shadow: var(--shadow-sm);
}

.tool-summary {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  cursor: pointer;
  list-style: none;
  font-size: var(--font-size-sm);
  font-weight: 500;
  color: var(--color-text);
  user-select: none;
  transition: background var(--transition-base);
}

.tool-summary::-webkit-details-marker {
  display: none;
}

.tool-summary:hover {
  background: var(--bg-hover);
}

.tool-icon {
  color: var(--color-warning);
  flex-shrink: 0;
}

.tool-name {
  flex: 1;
  font-family: var(--font-mono);
  font-size: var(--font-size-xs);
}

.tool-chevron {
  color: var(--color-text-tertiary);
  transition: transform var(--transition-base);
}

.tool-card[open] .tool-chevron {
  transform: rotate(90deg);
}

.tool-detail {
  padding: var(--space-sm) var(--space-md);
  border-top: 1px solid var(--border-color);
  display: flex;
  flex-direction: column;
  gap: var(--space-sm);
}

.tool-arg-row {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.tool-label {
  font-weight: 600;
  color: var(--color-text-secondary);
  font-size: var(--font-size-xs);
  text-transform: uppercase;
  letter-spacing: 0.04em;
}

.tool-result-code {
  font-family: var(--font-mono);
  font-size: var(--font-size-xs);
  background: var(--bg-code);
  padding: 6px 10px;
  border-radius: var(--radius-sm);
  white-space: pre-wrap;
  word-break: break-all;
  color: var(--color-text);
  line-height: 1.5;
}

.tool-error-row .tool-label {
  color: var(--color-danger);
}

.tool-error-row .tool-result-code {
  background: var(--color-error-bg);
  color: var(--color-danger);
}

/* ── Responsive ── */
@media (max-width: 768px) {
  .user-body {
    max-width: 92%;
  }

  .assistant-body {
    max-width: calc(100% - 42px);
  }

  .message-avatar {
    width: 30px;
    height: 30px;
    font-size: var(--font-size-xs);
  }

  .user-bubble,
  .assistant-bubble {
    font-size: var(--font-size-sm);
    padding: 8px 12px;
  }
}
</style>
