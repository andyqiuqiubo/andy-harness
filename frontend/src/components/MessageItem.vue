<script setup lang="ts">
import type { Message } from '../api/types'
import MarkdownRenderer from './MarkdownRenderer.vue'

defineProps<{ message: Message }>()
</script>

<template>
  <div :class="['message-row', message.role]">
    <!-- User messages: avatar on right, content aligned right -->
    <template v-if="message.role === 'user'">
      <div class="message-body user-body">
        <div class="message-bubble user-bubble">
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
        <div class="message-bubble assistant-bubble" v-if="message.role === 'assistant' || message.role === 'system'">
          <MarkdownRenderer v-if="message.content" :content="message.content" />
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
/* ── Message row layout ── */
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
