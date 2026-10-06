<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue'
import type { Message, TokenUsage } from '../api/types'
import { formatTokens } from '../utils/format'
import { fetchAttachmentBlobUrl } from '../api/client'
import MarkdownRenderer from './MarkdownRenderer.vue'
import { useLanguage } from '../composables/useLanguage'

const props = defineProps<{ message: Message }>()
const { t } = useLanguage()

const emit = defineEmits<{
  (e: 'delete-turn', message: Message): void
  (e: 'follow-up', message: Message): void
  (e: 'fork', message: Message): void
  (e: 'retry', message: Message): void
}>()

// 复制反馈：短暂显示「复制成功」提示（3 秒）
const copied = ref(false)
let copyTimer: ReturnType<typeof setTimeout> | null = null

// P2-4：图片附件加载失败追踪（用于显示降级占位，而非浏览器默认破图）
const failedAttachments = ref<string[]>([])
function markFailed(id: string) {
  if (!failedAttachments.value.includes(id)) failedAttachments.value.push(id)
}
function markLoaded(id: string) {
  failedAttachments.value = failedAttachments.value.filter((x) => x !== id)
}

// P0-3：图片附件以 blob: 渲染，使 E12 鉴权模式下也能正确带 Bearer 取图。
// imageSrc 以 att.id 为键缓存已解析地址（blob: 或回退直链）。
const imageSrc = ref<Record<string, string>>({})
const imageAttachments = computed(() =>
  (props.message.attachments ?? []).filter((a) => a.kind === 'image'),
)
const objectUrls = new Set<string>()

async function resolveImage(id: string, sessionId: string) {
  if (imageSrc.value[id]) return
  const url = await fetchAttachmentBlobUrl(sessionId, id)
  if (url.startsWith('blob:')) objectUrls.add(url)
  imageSrc.value = { ...imageSrc.value, [id]: url }
}

// 附件列表变化（含首帧）时解析所有图片地址
watch(
  imageAttachments,
  (list) => {
    for (const att of list) resolveImage(att.id, props.message.session_id)
  },
  { immediate: true },
)

// 组件卸载时释放 blob: URL，避免内存泄漏
onUnmounted(() => {
  for (const url of objectUrls) URL.revokeObjectURL(url)
  objectUrls.clear()
})

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
  if (copyTimer) clearTimeout(copyTimer)
  copyTimer = window.setTimeout(() => {
    copied.value = false
  }, 3000)
}

// 是否在消息流中可见：
// - tool 角色消息仅用于模型 API 上下文，其结果已在「执行过程」过程框中
//   展示（ChatView.turns 按版本归并），不单独渲染（避免孤立的工具头像行）
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
          <button class="icon-btn" :title="copied ? t('chat.copied') : t('chat.copyQuestion')" @click="copyContent">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <rect x="9" y="9" width="13" height="13" rx="2" /><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" />
            </svg>
          </button>
          <button class="icon-btn danger" :title="t('chat.deleteTurnBtn')" @click="emit('delete-turn', message)">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <polyline points="3 6 5 6 21 6" /><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6" /><path d="M10 11v6M14 11v6" /><path d="M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2" />
            </svg>
          </button>
        </div>
        <div class="message-bubble user-bubble">
          <div v-if="message.attachments && message.attachments.length" class="msg-attachments">
          <img
            v-for="att in imageAttachments"
            :key="att.id"
            class="msg-attach-img"
            :src="imageSrc[att.id] || ''"
            :alt="att.filename"
            :title="att.filename"
            loading="lazy"
            @error="markFailed(att.id)"
            @load="markLoaded(att.id)"
          />
            <!-- P2-4：加载中占位 + 加载失败降级，避免布局抖动与无提示破图 -->
            <div
              v-for="att in message.attachments.filter(a => a.kind === 'image')"
              v-show="failedAttachments.includes(att.id)"
              :key="`fb-${att.id}`"
              class="msg-attach-img msg-attach-fallback"
              :title="att.filename"
            >
              <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <rect x="3" y="3" width="18" height="18" rx="2" /><circle cx="8.5" cy="8.5" r="1.5" /><polyline points="21 15 16 10 5 21" />
              </svg>
              <span class="msg-attach-fallback-text">无法预览</span>
            </div>
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

        <!-- 回答的操作条：复制 / 重新回答 / 追问 / 分叉（仅 assistant 回答） -->
        <div class="msg-actions-wrap" v-if="message.role === 'assistant' && message.content">
          <div class="msg-actions">
            <button class="icon-btn" :title="copied ? t('chat.copied') : t('chat.copyAnswer')" @click="copyContent">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <rect x="9" y="9" width="13" height="13" rx="2" /><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" />
              </svg>
            </button>
            <!-- 重新回答：保留历史答案版本，仅针对同一提问再生成一条新答案 -->
            <button class="icon-btn" :title="t('chat.retryTitle')" @click="emit('retry', message)">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <polyline points="23 4 23 10 17 10" /><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10" />
              </svg>
            </button>
            <button class="icon-btn" :title="t('chat.followUpBtn')" @click="emit('follow-up', message)">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <polyline points="9 17 4 12 9 7" /><path d="M20 18v-2a4 4 0 0 0-4-4H4" />
              </svg>
            </button>
            <button class="icon-btn" :title="t('chat.forkBtn')" @click="emit('fork', message)">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <line x1="6" y1="3" x2="6" y2="15" /><circle cx="18" cy="6" r="3" /><circle cx="6" cy="18" r="3" /><path d="M18 9a9 9 0 0 1-9 9" />
              </svg>
            </button>
          </div>
          <!-- 复制成功提示（3 秒后自动消失） -->
          <transition name="copy-toast-fade">
            <span v-if="copied" class="copy-toast">{{ t('chat.copied') }}</span>
          </transition>
        </div>

        <!-- 本次对话的 token 用量（DeepSeek 官方 usage），每条回答各自保留 -->
        <div v-if="message.role === 'assistant' && message.usage" class="message-usage">
          <span class="usage-label">本次用量</span>
          <span class="usage-item">输入 {{ formatTokens(message.usage.prompt_tokens) }}</span>
          <span v-if="usageCacheHit(message.usage) > 0" class="usage-item usage-muted">(缓存 {{ formatTokens(usageCacheHit(message.usage)) }} · 未命中 {{ formatTokens(usageCacheMiss(message.usage)) }})</span>
          <span class="usage-item">· 输出 {{ formatTokens(message.usage.completion_tokens) }}</span>
          <span class="usage-item usage-total">· 合计 {{ formatTokens(message.usage.total_tokens) }} tokens</span>
        </div>

        <!-- Tool calls 已统一归并进「执行过程」过程框（ChatView.turns 按版本归并），
             此处不再内联渲染，避免同一工具调用重复展示两处。 -->
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

/* 操作条 + 复制成功提示的容器（相对定位，供 toast 绝对定位） */
.msg-actions-wrap {
  position: relative;
  display: inline-flex;
  align-items: center;
}

/* 复制成功提示：浮于操作条上方，3 秒后淡出；不受 .msg-actions 悬浮显隐影响 */
.copy-toast {
  position: absolute;
  bottom: calc(100% + 6px);
  left: 0;
  background: var(--color-success, #16a34a);
  color: #fff;
  font-size: var(--font-size-xs);
  line-height: 1;
  padding: 5px 10px;
  border-radius: var(--radius-md);
  white-space: nowrap;
  pointer-events: none;
  box-shadow: var(--shadow-sm);
  z-index: 5;
}

.copy-toast-fade-enter-active,
.copy-toast-fade-leave-active {
  transition: opacity var(--transition-fast), transform var(--transition-fast);
}
.copy-toast-fade-enter-from,
.copy-toast-fade-leave-to {
  opacity: 0;
  transform: translateY(4px);
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
  /* P2-4：固定骨架尺寸，避免图片加载前后气泡高度抖动 */
  width: 160px;
  height: 120px;
  max-width: 180px;
  max-height: 180px;
  border-radius: var(--radius-sm);
  object-fit: cover;
  border: 1px solid var(--border-color);
  background: var(--bg-surface);
}

/* 图片加载失败时的降级占位（不显示浏览器默认破图） */
.msg-attach-fallback {
  display: inline-flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 4px;
  color: var(--color-text-muted, #888);
  font-size: var(--font-size-xs);
}

.msg-attach-fallback-text {
  line-height: 1;
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

/* Tool call 卡片样式已随内联渲染一并移除：工具调用统一在
   ChatView 的「执行过程」过程框中展示（ProcessTrace）。 */

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
