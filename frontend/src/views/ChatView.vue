<script setup lang="ts">
import { ref, watch, onMounted, onUnmounted, nextTick } from 'vue'
import { useChatStore } from '../stores/chat'
import { useProviderStore } from '../stores/providers'
import { apiClient, wsConnectionStatus } from '../api/client'
import type { WSFrame } from '../api/types'
import { useLanguage } from '../composables/useLanguage'
import { useSettingsStore } from '../stores/settings'
import SessionSidebar from '../components/SessionSidebar.vue'
import MessageItem from '../components/MessageItem.vue'
import ModelSelector from '../components/ModelSelector.vue'
import MarkdownRenderer from '../components/MarkdownRenderer.vue'

const chatStore = useChatStore()
const providerStore = useProviderStore()
const settingsStore = useSettingsStore()
const { t } = useLanguage()

const inputText = ref('')
const selectedModel = ref(settingsStore.sessionSettings.model || 'deepseek-v4-flash')
const selectedProvider = ref('')
const messagesContainer = ref<HTMLElement | null>(null)

// 初始化 provider 选择：优先 deepseek
watch(() => providerStore.providers, (providers) => {
  if (providers.length > 0 && !selectedProvider.value) {
    // 优先选择 deepseek
    const deepseek = providers.find((p) => p.id === 'deepseek' || p.id.includes('deepseek'))
    const enabled = providers.filter((p) => p.enabled !== false)
    if (deepseek && deepseek.enabled !== false) {
      selectedProvider.value = deepseek.id
    } else {
      const target = enabled.length > 0 ? enabled[0] : providers[0]
      selectedProvider.value = target.id
    }
  }
}, { immediate: true })

// WS 消息处理
function onWSMessage(data: unknown) {
  chatStore.handleWSFrame(data as WSFrame)
}

// 滚动到底部
function scrollToBottom() {
  nextTick(() => {
    if (messagesContainer.value) {
      messagesContainer.value.scrollTop = messagesContainer.value.scrollHeight
    }
  })
}

// 发送消息
function handleSend() {
  if (!inputText.value.trim() || chatStore.isStreaming) return
  chatStore.sendMessage(inputText.value, selectedProvider.value, selectedModel.value || undefined)
  inputText.value = ''
  scrollToBottom()
}

// 停止生成
function handleStop() {
  chatStore.stopStreaming()
}

// 监听消息变化自动滚动
watch(() => [chatStore.messages.length, chatStore.streamingContent, chatStore.streamingReasoning, chatStore.toolEvents.length], () => {
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
})

onUnmounted(() => {
  if (unregisterWS) {
    unregisterWS()
    unregisterWS = null
  }
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
          <h2>{{ chatStore.currentSession?.title || 'andy-harness' }}</h2>
        </div>
        <div class="header-controls">
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
          <div class="conn-status" :class="`status-${wsConnectionStatus}`">
            <span class="conn-dot"></span>
            <span class="conn-text">{{ statusText(wsConnectionStatus) }}</span>
          </div>
          <router-link to="/settings" class="btn-settings" title="设置">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <circle cx="12" cy="12" r="3" />
              <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z" />
            </svg>
          </router-link>
        </div>
      </header>

      <!-- Messages -->
      <div ref="messagesContainer" class="messages-container">
        <div class="messages-inner">
          <MessageItem v-for="msg in chatStore.messages" :key="msg.id" :message="msg" />

          <!-- 流式渲染中 -->
          <div v-if="chatStore.streamingContent || chatStore.streamingReasoning.length || chatStore.toolEvents.length" class="streaming-message">
            <div class="message-avatar assistant-avatar">
              <svg width="20" height="20" viewBox="0 0 24 24" fill="none">
                <path d="M12 2L2 7l10 5 10-5-10-5z" stroke="currentColor" stroke-width="2" stroke-linejoin="round"/>
                <path d="M2 17l10 5 10-5M2 12l10 5 10-5" stroke="currentColor" stroke-width="2" stroke-linejoin="round"/>
              </svg>
            </div>
            <div class="streaming-body">
              <!-- 思维链：逐步展示，完成后折叠 -->
              <details
                v-if="chatStore.streamingReasoning.length > 0"
                class="reasoning-block"
                :open="!chatStore.reasoningDone"
              >
                <summary class="reasoning-summary">
                  <svg class="reasoning-icon" width="14" height="14" viewBox="0 0 24 24" fill="none">
                    <path d="M9.66 7H17a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h2.66l1-2h2l1 2z" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
                  </svg>
                  <span class="reasoning-label">{{ chatStore.reasoningDone ? '思维过程（点击展开）' : '思考中...' }}</span>
                  <svg v-if="!chatStore.reasoningDone" class="reasoning-spinner" width="14" height="14" viewBox="0 0 24 24" fill="none">
                    <circle cx="12" cy="12" r="10" stroke="currentColor" stroke-width="2" stroke-dasharray="40" stroke-dashoffset="20" stroke-linecap="round">
                      <animateTransform attributeName="transform" type="rotate" from="0 12 12" to="360 12 12" dur="1s" repeatCount="indefinite"/>
                    </circle>
                  </svg>
                </summary>
                <div class="reasoning-text">{{ chatStore.streamingReasoning }}</div>
              </details>

              <!-- 正文回复 -->
              <div v-if="chatStore.streamingContent" class="streaming-content">
                <MarkdownRenderer :content="chatStore.streamingContent" /><span class="streaming-cursor"></span>
              </div>

              <!-- Tool events -->
              <div v-for="(tc, i) in chatStore.toolEvents" :key="`tool-${i}`" class="tool-card">
                <details>
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
                    <div class="tool-result">
                      <span class="tool-label">结果:</span>
                      <code class="tool-result-code">{{ tc.result }}</code>
                    </div>
                    <div v-if="tc.error" class="tool-error">
                      <span class="tool-label">错误:</span>
                      <code class="tool-result-code">{{ tc.error }}</code>
                    </div>
                  </div>
                </details>
              </div>
            </div>
          </div>

          <!-- 上下文快照 -->
          <div v-if="chatStore.contextSnapshot" class="context-snapshot">
            <details>
              <summary class="snapshot-summary">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none">
                  <path d="M3 3v18h18" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>
                  <path d="M7 14l4-4 4 4 5-5" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
                </svg>
                <span>上下文快照 (tokens: {{ chatStore.contextSnapshot.token_count }}/{{ chatStore.contextSnapshot.budget }})</span>
              </summary>
            </details>
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

      <!-- Input area -->
      <div class="input-area">
        <div class="input-wrapper">
          <textarea
            v-model="inputText"
            class="input-textarea"
            :placeholder="t('chat.placeholder')"
            @keydown.enter.exact.prevent="handleSend"
            :disabled="chatStore.isStreaming"
            rows="3"
          />
          <button
            v-if="!chatStore.isStreaming"
            class="btn-send"
            v-ripple
            @click="handleSend"
            :disabled="!inputText.trim() || !chatStore.currentSessionId"
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
.btn-settings {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 32px;
  height: 32px;
  border-radius: var(--radius-md);
  color: var(--color-text-secondary);
  transition: var(--transition-base);
  flex-shrink: 0;
}

.btn-settings:hover {
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
}

.messages-inner {
  max-width: var(--content-max-width);
  margin: 0 auto;
  width: 100%;
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

/* ── 思维链展示 ── */
.reasoning-block {
  margin-bottom: var(--space-sm);
  border: 1px solid var(--border-color);
  border-radius: var(--radius-md);
  background: var(--bg-form);
  overflow: hidden;
  transition: all var(--transition-base);
}

.reasoning-block[open] {
  border-color: var(--color-primary-light);
}

.reasoning-summary {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 8px 12px;
  cursor: pointer;
  font-size: var(--font-size-sm);
  color: var(--color-text-secondary);
  user-select: none;
  list-style: none;
  transition: background var(--transition-fast);
}

.reasoning-summary::-webkit-details-marker {
  display: none;
}

.reasoning-summary:hover {
  background: var(--bg-hover);
}

.reasoning-icon {
  flex-shrink: 0;
  color: var(--color-primary);
}

.reasoning-label {
  font-weight: 500;
}

.reasoning-spinner {
  color: var(--color-primary);
  margin-left: auto;
}

.reasoning-text {
  padding: 8px 12px 10px;
  max-height: 300px;
  overflow-y: auto;
  font-size: var(--font-size-xs);
  color: var(--color-text-secondary);
  line-height: 1.7;
  font-family: var(--font-mono);
  border-left: 2px solid var(--color-primary-light);
  margin: 4px 8px 4px 12px;
  white-space: pre-wrap;
  word-break: break-word;
  animation: reasoningFadeIn 400ms ease both;
  opacity: 0.85;
}

@keyframes reasoningFadeIn {
  from { opacity: 0; }
  to { opacity: 0.85; }
}

.streaming-content {
  white-space: pre-wrap;
  line-height: 1.7;
  font-size: var(--font-size-base);
  color: var(--color-text);
  margin-top: var(--space-sm);
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

/* ── Tool cards (in streaming) ── */
.tool-card {
  margin: var(--space-sm) 0;
}

.tool-card details {
  border: 1px solid var(--border-color);
  border-radius: var(--radius-md);
  background: var(--bg-tool);
  overflow: hidden;
  transition: border-color var(--transition-base), box-shadow var(--transition-base);
}

.tool-card details:hover {
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

details[open] .tool-chevron {
  transform: rotate(90deg);
}

.tool-detail {
  padding: var(--space-sm) var(--space-md);
  border-top: 1px solid var(--border-color);
  font-size: var(--font-size-sm);
}

.tool-result,
.tool-error {
  display: flex;
  flex-direction: column;
  gap: 4px;
  margin-bottom: 6px;
}

.tool-label {
  font-weight: 600;
  color: var(--color-text-secondary);
  font-size: var(--font-size-xs);
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

.tool-error .tool-label {
  color: var(--color-danger);
}

.tool-error .tool-result-code {
  background: var(--color-error-bg);
  color: var(--color-danger);
}

/* ── Context snapshot ── */
.context-snapshot {
  margin: var(--space-md) 0;
  animation: slideUp var(--transition-base) ease-out;
}

.context-snapshot details {
  border: 1px solid var(--border-color);
  border-radius: var(--radius-md);
  background: var(--bg-snapshot);
  overflow: hidden;
}

.snapshot-summary {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  cursor: pointer;
  list-style: none;
  font-size: var(--font-size-xs);
  color: var(--color-text-secondary);
  user-select: none;
  font-weight: 500;
}

.snapshot-summary::-webkit-details-marker {
  display: none;
}

.snapshot-summary svg {
  color: var(--color-success);
  flex-shrink: 0;
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

/* ── Input area ── */
.input-area {
  padding: var(--space-md) var(--space-md) var(--space-lg);
  border-top: 1px solid var(--border-color);
  background: var(--bg-surface);
  flex-shrink: 0;
}

.input-wrapper {
  max-width: var(--content-max-width);
  margin: 0 auto;
  display: flex;
  align-items: flex-end;
  gap: var(--space-sm);
  background: var(--bg-input);
  border: 1px solid var(--border-color);
  border-radius: var(--radius-xl);
  padding: 8px 8px 8px 16px;
  box-shadow: var(--shadow-sm);
  transition: border-color var(--transition-base), box-shadow var(--transition-base);
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
  padding: 6px 0;
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
}
</style>
