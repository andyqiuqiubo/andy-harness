<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { apiClient } from '../api/client'
import { useLanguage } from '../composables/useLanguage'

const { t } = useLanguage()

interface ActionSpec {
  name: string
  description: string
}
interface ProviderDesc {
  provider_id: string
  provider_name: string
  supported_modes: string[]
  authenticated: boolean
  actions: ActionSpec[]
  cli_install_command?: string
  auth_instructions?: Record<string, string>
  channel_capabilities?: { name: string; description: string }[]
  channel_running?: boolean
  cli_available?: boolean | null
  cli_path?: string | null
  cli_auth?: { ready: boolean | null; summary: string } | null
}

interface ProviderUI {
  id: string
  name: string
  modes: string[]
  authenticated: boolean
  actions: ActionSpec[]
  cliInstall?: string
  authInstructions?: Record<string, string>
  channelCapabilities: { name: string; description: string }[]
  channelRunning: boolean
  cliAvailable: boolean | null
  cliPath: string | null
  cliAuth: { ready: boolean | null; summary: string } | null
  mode: string
  appId: string
  appSecret: string
  profile: string
  cliPathInput: string
  webhookUrl: string
  webhookSecret: string
  busy: boolean
  expanded: boolean
  selectedAction: string
  paramsText: string
  result: string
  error: string
  channelAppId: string
  channelAppSecret: string
  channelBusy: boolean
  channelDetail: string
}

const providers = ref<ProviderUI[]>([])
const loading = ref(false)
const loadError = ref('')
const copiedCmd = ref('')

async function loadProviders() {
  loading.value = true
  loadError.value = ''
  try {
    const data = await apiClient.get<{ providers: ProviderDesc[] }>('/integrations')
    providers.value = (data.providers || []).map((p) => ({
      id: p.provider_id,
      name: p.provider_name,
      modes: p.supported_modes,
      authenticated: p.authenticated,
      actions: p.actions,
      cliInstall: p.cli_install_command,
      authInstructions: p.auth_instructions,
      channelCapabilities: p.channel_capabilities || [],
      channelRunning: p.channel_running || false,
      cliAvailable: p.cli_available ?? null,
      cliPath: p.cli_path ?? null,
      cliAuth: p.cli_auth ?? null,
      // 默认落在「开放 API」：填凭证即可用；CLI 需另装二进制，不作默认
      mode: p.supported_modes.includes('api') ? 'api' : p.supported_modes[0] || 'cli',
      appId: '',
      appSecret: '',
      profile: '',
      cliPathInput: '',
      webhookUrl: '',
      webhookSecret: '',
      busy: false,
      expanded: false,
      selectedAction: p.actions[0]?.name || '',
      paramsText: '{}',
      result: '',
      error: '',
      channelAppId: '',
      channelAppSecret: '',
      channelBusy: false,
      channelDetail: '',
    }))
  } catch (e: unknown) {
    loadError.value = e instanceof Error ? e.message : String(e)
  } finally {
    loading.value = false
  }
}

function resetAfterChange(ui: ProviderUI) {
  ui.result = ''
  ui.error = ''
}

async function copyCmd(cmd: string) {
  try {
    await navigator.clipboard.writeText(cmd)
    copiedCmd.value = cmd
    setTimeout(() => {
      if (copiedCmd.value === cmd) copiedCmd.value = ''
    }, 2000)
  } catch {
    // 剪贴板不可用时忽略
  }
}

async function connect(ui: ProviderUI) {
  ui.busy = true
  ui.error = ''
  ui.result = ''
  try {
    const credentials: Record<string, string> = {}
    if (ui.appId) credentials.app_id = ui.appId
    if (ui.appSecret) credentials.app_secret = ui.appSecret
    if (ui.mode === 'cli') {
      if (ui.profile) credentials.profile = ui.profile
      // 非 PATH 安装（如装在 F:\tools\larkcli）时，显式指定二进制路径
      if (ui.cliPathInput.trim()) credentials.cli_path = ui.cliPathInput.trim()
    }
    if (ui.mode === 'api') {
      if (ui.webhookUrl) credentials.webhook_url = ui.webhookUrl
      if (ui.webhookSecret) credentials.webhook_secret = ui.webhookSecret
    }
    const res = await apiClient.post<{ ok: boolean; detail?: string; error?: string }>(
      `/integrations/${ui.id}/connect`,
      { mode: ui.mode, credentials },
    )
    if (res.ok) {
      ui.authenticated = true
      ui.result = JSON.stringify(res, null, 2)
      await refreshDetail(ui)
    } else {
      ui.error = res.error || '连接失败'
    }
  } catch (e: unknown) {
    ui.error = e instanceof Error ? e.message : String(e)
  } finally {
    ui.busy = false
  }
}

async function disconnect(ui: ProviderUI) {
  ui.busy = true
  ui.error = ''
  try {
    await apiClient.post<{ ok: boolean }>(`/integrations/${ui.id}/disconnect`, {})
    ui.authenticated = false
    ui.result = ''
  } catch (e: unknown) {
    ui.error = e instanceof Error ? e.message : String(e)
  } finally {
    ui.busy = false
  }
}

async function refreshDetail(ui: ProviderUI) {
  try {
    const detail = await apiClient.get<ProviderDesc>(`/integrations/${ui.id}`)
    ui.authenticated = detail.authenticated
    ui.actions = detail.actions
    ui.cliInstall = detail.cli_install_command || ui.cliInstall
    ui.authInstructions = detail.auth_instructions || ui.authInstructions
    ui.cliAvailable = detail.cli_available ?? ui.cliAvailable
    ui.cliPath = detail.cli_path ?? ui.cliPath
    ui.cliAuth = detail.cli_auth ?? ui.cliAuth
    ui.channelCapabilities = detail.channel_capabilities || ui.channelCapabilities
    ui.channelRunning = detail.channel_running || false
    if (!ui.actions.find((a) => a.name === ui.selectedAction)) {
      ui.selectedAction = ui.actions[0]?.name || ''
    }
  } catch {
    // 忽略刷新失败，保留现有展示
  }
}

async function callAction(ui: ProviderUI) {
  ui.error = ''
  ui.result = ''
  let params: Record<string, unknown>
  try {
    params = ui.paramsText.trim() ? JSON.parse(ui.paramsText) : {}
  } catch {
    ui.error = t.value('integrations.paramsInvalid')
    return
  }
  ui.busy = true
  try {
    const res = await apiClient.post<{ ok: boolean; data?: unknown; error?: string }>(
      `/integrations/${ui.id}/call`,
      { action: ui.selectedAction, params },
    )
    ui.result = JSON.stringify(res, null, 2)
    if (!res.ok) ui.error = res.error || '调用失败'
  } catch (e: unknown) {
    ui.error = e instanceof Error ? e.message : String(e)
  } finally {
    ui.busy = false
  }
}

async function startChannel(ui: ProviderUI) {
  ui.channelBusy = true
  ui.channelDetail = ''
  ui.error = ''
  try {
    const res = await apiClient.post<{ ok: boolean; detail?: string; error?: string }>(
      `/integrations/${ui.id}/channel/start`,
      { credentials: { app_id: ui.channelAppId, app_secret: ui.channelAppSecret } },
    )
    ui.channelDetail = res.detail || res.error || ''
    if (res.ok) ui.channelRunning = true
    else ui.error = res.error || '启动失败'
    await refreshDetail(ui)
  } catch (e: unknown) {
    ui.error = e instanceof Error ? e.message : String(e)
  } finally {
    ui.channelBusy = false
  }
}

async function stopChannel(ui: ProviderUI) {
  ui.channelBusy = true
  ui.error = ''
  try {
    const res = await apiClient.post<{ ok: boolean; detail?: string; error?: string }>(
      `/integrations/${ui.id}/channel/stop`,
      {},
    )
    ui.channelDetail = res.detail || res.error || ''
    if (res.ok) ui.channelRunning = false
    await refreshDetail(ui)
  } catch (e: unknown) {
    ui.error = e instanceof Error ? e.message : String(e)
  } finally {
    ui.channelBusy = false
  }
}

async function refreshChannelStatus(ui: ProviderUI) {
  try {
    const res = await apiClient.post<{ running: boolean; detail: string }>(
      `/integrations/${ui.id}/channel/status`,
      {},
    )
    ui.channelRunning = res.running
    ui.channelDetail = res.detail || ''
  } catch {
    // 忽略
  }
}

function selectAction(ui: ProviderUI, name: string) {
  ui.selectedAction = name
  if (name === 'send_message') {
    if (ui.mode === 'cli') {
      ui.paramsText = JSON.stringify({ chat_id: '', text: 'hello' }, null, 2)
    } else {
      ui.paramsText = JSON.stringify(
        { receive_id: '', receive_id_type: 'chat_id', msg_type: 'text', content: 'hello' },
        null,
        2,
      )
    }
  } else if (name === 'get_user') {
    ui.paramsText = JSON.stringify({ user_id: '', user_id_type: 'open_id' }, null, 2)
  } else if (name === 'create_doc') {
    ui.paramsText = JSON.stringify({ title: 'andy-harness', content: 'andy' }, null, 2)
  } else if (name === 'write_doc') {
    ui.paramsText = JSON.stringify({ document_id: '', content: 'andy' }, null, 2)
  } else if (name === 'send_webhook') {
    ui.paramsText = JSON.stringify({ text: 'hello from andy-harness' }, null, 2)
  } else if (name === 'raw') {
    ui.paramsText = JSON.stringify(
      { args: ['im', 'send-message', '--chat-id', 'oc_xxx', '--text', 'hi'] },
      null,
      2,
    )
  } else {
    ui.paramsText = '{}'
  }
}

function modeLabel(m: string): string {
  if (m === 'api') return t.value('integrations.modeApi')
  if (m === 'cli') return t.value('integrations.modeCli')
  if (m === 'channel') return t.value('integrations.modeChannel')
  return m
}

/** 页签展示顺序：开放 API 优先（填凭证即用），CLI 需另装二进制放后面。 */
const MODE_DISPLAY_ORDER = ['api', 'channel', 'cli']
function displayModes(ui: ProviderUI): string[] {
  return [...ui.modes].sort((a, b) => {
    const ia = MODE_DISPLAY_ORDER.indexOf(a)
    const ib = MODE_DISPLAY_ORDER.indexOf(b)
    return (ia === -1 ? 99 : ia) - (ib === -1 ? 99 : ib)
  })
}

onMounted(loadProviders)
</script>

<template>
  <div class="integrations-panel">
    <div class="section-header">
      <h3 class="section-subtitle">{{ t('integrations.title') }}</h3>
      <button class="btn-ghost btn-sm" :disabled="loading" @click="loadProviders">
        {{ t('integrations.refresh') }}
      </button>
    </div>
    <p class="section-desc">{{ t('integrations.desc') }}</p>

    <div v-if="loading" class="empty-hint">{{ t('integrations.loading') }}</div>
    <div v-else-if="loadError" class="audit-error">{{ loadError }}</div>
    <div v-else-if="providers.length === 0" class="empty-hint">{{ t('integrations.empty') }}</div>

    <div v-for="ui in providers" :key="ui.id" class="integration-card card">
      <div class="integration-head">
        <div class="integration-title">
          <span class="integration-name">{{ ui.name }}</span>
          <span class="badge-pill" :class="ui.authenticated || ui.channelRunning ? 'badge-ok' : 'badge-disabled'">
            {{ ui.authenticated || ui.channelRunning ? t('integrations.connected') : t('integrations.notConnected') }}
          </span>
        </div>
        <button class="btn-ghost btn-xs" @click="ui.expanded = !ui.expanded">
          {{ ui.expanded ? t('integrations.collapse') : t('integrations.expand') }}
        </button>
      </div>

      <div v-if="ui.expanded" class="integration-body">
        <!-- 接入方式 Tab -->
        <div class="form-row">
          <label class="form-label">{{ t('integrations.mode') }}</label>
          <div class="mode-tabs">
            <button
              v-for="m in displayModes(ui)"
              :key="m"
              :class="['mode-tab', { active: ui.mode === m }]"
              @click="(ui.mode = m), resetAfterChange(ui)"
            >
              {{ modeLabel(m) }}
              <span v-if="m === 'cli' && ui.cliAvailable === false" class="mode-tag mode-tag-warn">
                {{ t('integrations.cliNotInstalled') }}
              </span>
            </button>
          </div>
        </div>

        <!-- CLI 模式：安装 / 授权命令 + 应用身份（可选） -->
        <template v-if="ui.mode === 'cli'">
          <p v-if="ui.cliAvailable === false" class="integration-hint hint-warn">
            {{ t('integrations.cliNotInstalledHint') }}
          </p>
          <p v-else-if="ui.cliPath" class="integration-hint">
            {{ t('integrations.cliDetected') }}
            <code class="cmd-text cmd-text-inline">{{ ui.cliPath }}</code>
          </p>
          <p v-if="ui.cliAuth && ui.cliAuth.ready === false" class="integration-hint hint-warn">
            {{ t('integrations.cliAuthNotReady') }}
            <template v-if="ui.cliAuth.summary">（{{ ui.cliAuth.summary }}）</template>
          </p>
          <p v-else-if="ui.cliAuth && ui.cliAuth.ready" class="integration-hint">
            {{ t('integrations.cliAuthReady') }}
            <template v-if="ui.cliAuth.summary">（{{ ui.cliAuth.summary }}）</template>
          </p>
          <div v-if="ui.cliInstall" class="cmd-block">
            <span class="cmd-label">{{ t('integrations.installCmd') }}</span>
            <code class="cmd-text">{{ ui.cliInstall }}</code>
            <button class="btn-ghost btn-xs" @click="copyCmd(ui.cliInstall!)">
              {{ copiedCmd === ui.cliInstall ? t('integrations.copied') : t('integrations.copy') }}
            </button>
          </div>
          <div class="cmd-block">
            <span class="cmd-label">{{ t('integrations.loginCmd') }}</span>
            <code class="cmd-text">lark-cli auth login</code>
            <button class="btn-ghost btn-xs" @click="copyCmd('lark-cli auth login')">
              {{ copiedCmd === 'lark-cli auth login' ? t('integrations.copied') : t('integrations.copy') }}
            </button>
          </div>
          <p class="integration-hint">
            {{ ui.authInstructions?.cli || t('integrations.cliHint') }}
          </p>
          <details class="adv-block">
            <summary>{{ t('integrations.advIdentity') }}</summary>
            <div class="form-row">
              <label class="form-label">{{ t('integrations.appId') }}</label>
              <input v-model="ui.appId" class="form-input" :placeholder="t('integrations.appIdPh')" />
            </div>
            <div class="form-row">
              <label class="form-label">{{ t('integrations.appSecret') }}</label>
              <input
                v-model="ui.appSecret"
                type="password"
                class="form-input"
                :placeholder="t('integrations.appSecretPh')"
              />
            </div>
            <p class="integration-hint">{{ t('integrations.cliAppIdentityHint') }}</p>
            <div class="form-row">
              <label class="form-label">{{ t('integrations.profile') }}</label>
              <input v-model="ui.profile" class="form-input" :placeholder="t('integrations.profilePh')" />
            </div>
            <div class="form-row">
              <label class="form-label">{{ t('integrations.cliPath') }}</label>
              <input
                v-model="ui.cliPathInput"
                class="form-input"
                :placeholder="t('integrations.cliPathPh')"
              />
            </div>
            <p class="integration-hint">{{ t('integrations.cliPathHint') }}</p>
          </details>
          <div class="integration-actions">
            <button
              v-if="!ui.authenticated"
              class="btn-primary btn-sm"
              :disabled="ui.busy"
              @click="connect(ui)"
            >
              {{ ui.busy ? t('integrations.connecting') : t('integrations.connect') }}
            </button>
            <button v-else class="btn-ghost btn-sm" :disabled="ui.busy" @click="disconnect(ui)">
              {{ t('integrations.disconnect') }}
            </button>
          </div>
        </template>

        <!-- Channel 模式：入站，让 Agent 在飞书里被对话调用 -->
        <template v-else-if="ui.mode === 'channel'">
          <p class="integration-hint">{{ t('integrations.channelDesc') }}</p>
          <ul v-if="ui.channelCapabilities.length" class="channel-caps">
            <li v-for="cap in ui.channelCapabilities" :key="cap.name">
              <strong>{{ cap.name }}</strong>：{{ cap.description }}
            </li>
          </ul>
          <div class="form-row">
            <label class="form-label">{{ t('integrations.appId') }}</label>
            <input v-model="ui.channelAppId" class="form-input" :placeholder="t('integrations.appIdPh')" />
          </div>
          <div class="form-row">
            <label class="form-label">{{ t('integrations.appSecret') }}</label>
            <input
              v-model="ui.channelAppSecret"
              type="password"
              class="form-input"
              :placeholder="t('integrations.appSecretPh')"
            />
          </div>
          <p class="integration-hint">{{ t('integrations.channelAppHint') }}</p>
          <div class="integration-actions">
            <button
              v-if="!ui.channelRunning"
              class="btn-primary btn-sm"
              :disabled="ui.channelBusy"
              @click="startChannel(ui)"
            >
              {{ ui.channelBusy ? t('integrations.channelStarting') : t('integrations.channelStart') }}
            </button>
            <button
              v-else
              class="btn-ghost btn-sm"
              :disabled="ui.channelBusy"
              @click="stopChannel(ui)"
            >
              {{ ui.channelBusy ? t('integrations.channelStopping') : t('integrations.channelStop') }}
            </button>
            <button class="btn-ghost btn-xs" :disabled="ui.channelBusy" @click="refreshChannelStatus(ui)">
              {{ t('integrations.refresh') }}
            </button>
          </div>
          <p class="channel-status" :class="ui.channelRunning ? 'on' : 'off'">
            {{ ui.channelRunning ? t('integrations.channelRunning') : t('integrations.channelStopped') }}
          </p>
          <p v-if="ui.channelDetail" class="integration-hint">{{ ui.channelDetail }}</p>
        </template>

        <!-- API 模式（兜底）：凭证必填 -->
        <template v-else>
          <div class="form-row">
            <label class="form-label">{{ t('integrations.appId') }}</label>
            <input v-model="ui.appId" class="form-input" :placeholder="t('integrations.appIdPh')" />
          </div>
          <div class="form-row">
            <label class="form-label">{{ t('integrations.appSecret') }}</label>
            <input
              v-model="ui.appSecret"
              type="password"
              class="form-input"
              :placeholder="t('integrations.appSecretPh')"
            />
          </div>
          <div class="form-row">
            <label class="form-label">{{ t('integrations.webhookUrl') }}</label>
            <input v-model="ui.webhookUrl" class="form-input" :placeholder="t('integrations.webhookUrlPh')" />
          </div>
          <div class="form-row">
            <label class="form-label">{{ t('integrations.webhookSecretPh') }}</label>
            <input
              v-model="ui.webhookSecret"
              type="password"
              class="form-input"
              :placeholder="t('integrations.webhookSecretPh')"
            />
          </div>
          <p class="integration-hint">
            {{ ui.authInstructions?.api || t('integrations.apiWebhookHint') }}
          </p>
          <div class="integration-actions">
            <button
              v-if="!ui.authenticated"
              class="btn-primary btn-sm"
              :disabled="ui.busy"
              @click="connect(ui)"
            >
              {{ ui.busy ? t('integrations.connecting') : t('integrations.connect') }}
            </button>
            <button v-else class="btn-ghost btn-sm" :disabled="ui.busy" @click="disconnect(ui)">
              {{ t('integrations.disconnect') }}
            </button>
          </div>
        </template>

        <!-- 已连接（cli/api）：动作调用 -->
        <div v-if="ui.authenticated && (ui.mode === 'cli' || ui.mode === 'api')" class="integration-invoke">
          <div class="form-row">
            <label class="form-label">{{ t('integrations.action') }}</label>
            <select
              class="form-input"
              :value="ui.selectedAction"
              @change="selectAction(ui, ($event.target as HTMLSelectElement).value)"
            >
              <option v-for="a in ui.actions" :key="a.name" :value="a.name">{{ a.name }}</option>
            </select>
          </div>
          <p class="integration-action-desc">
            {{ ui.actions.find((a) => a.name === ui.selectedAction)?.description }}
          </p>
          <div class="form-row">
            <label class="form-label">{{ t('integrations.params') }}</label>
            <textarea
              v-model="ui.paramsText"
              class="form-input integration-params"
              rows="5"
              :placeholder="t('integrations.paramsPh')"
            ></textarea>
          </div>
          <div class="integration-actions">
            <button class="btn-primary btn-sm" :disabled="ui.busy" @click="callAction(ui)">
              {{ ui.busy ? t('integrations.invoking') : t('integrations.invoke') }}
            </button>
          </div>
        </div>

        <p v-if="ui.error" class="audit-error">{{ ui.error }}</p>
        <pre v-if="ui.result" class="integration-result">{{ ui.result }}</pre>
      </div>
    </div>
  </div>
</template>

<style scoped>
.integrations-panel {
  margin-top: var(--space-lg);
  padding-top: var(--space-lg);
  border-top: 1px solid var(--border-light);
}
.section-subtitle {
  font-size: 1.15rem;
  margin: 0;
  color: var(--color-text);
}
.section-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: var(--space-xs);
}
.section-desc {
  color: var(--color-text-tertiary);
  font-size: 0.85rem;
  margin: 0 0 var(--space-sm);
}
.integration-card {
  padding: var(--space-md);
  margin-bottom: var(--space-md);
}
.integration-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.integration-title {
  display: flex;
  align-items: center;
  gap: var(--space-sm);
}
.integration-name {
  font-weight: 600;
  font-size: 1rem;
  color: var(--color-text);
}
.integration-body {
  margin-top: var(--space-md);
}
.mode-tabs {
  display: flex;
  gap: var(--space-xs);
  flex-wrap: wrap;
}
.mode-tab {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 4px 14px;
  border: 1px solid var(--border-color);
  border-radius: 999px;
  background: var(--bg-input);
  color: var(--color-text-secondary);
  cursor: pointer;
  font-size: 0.85rem;
}
.mode-tab.active {
  border-color: var(--color-primary);
  color: var(--color-primary);
  background: var(--color-primary-light);
}
.mode-tag {
  font-size: 0.65rem;
  padding: 1px 6px;
  border-radius: 999px;
  background: var(--color-primary);
  color: #fff;
}
.mode-tag-warn {
  background: var(--color-warning);
}
.hint-warn {
  color: var(--color-warning);
}
.integration-hint {
  color: var(--color-text-tertiary);
  font-size: 0.8rem;
  margin: var(--space-xs) 0 var(--space-sm);
  line-height: 1.5;
}
/* 行内展示（如已检测到的 CLI 绝对路径）：不撑满、可换行 */
.cmd-text-inline {
  flex: 0 1 auto;
  font-size: 0.75rem;
  word-break: break-all;
}
.cmd-block {
  display: flex;
  align-items: center;
  gap: var(--space-xs);
  margin: var(--space-xs) 0;
  flex-wrap: wrap;
}
.cmd-label {
  font-size: 0.8rem;
  color: var(--color-text-secondary);
  white-space: nowrap;
}
.cmd-text {
  flex: 1 1 auto;
  min-width: 0;
  padding: 4px 8px;
  background: var(--bg-input);
  border: 1px solid var(--border-light);
  border-radius: var(--radius-sm);
  color: var(--color-text);
  font-family: var(--font-mono, monospace);
  font-size: 0.78rem;
  white-space: nowrap;
  overflow: auto;
}
.channel-caps {
  margin: var(--space-xs) 0;
  padding-left: var(--space-lg);
  color: var(--color-text-secondary);
  font-size: 0.82rem;
  line-height: 1.6;
}
.channel-status {
  font-size: 0.82rem;
  margin: var(--space-xs) 0;
  font-weight: 600;
}
.channel-status.on {
  color: var(--color-success, #2e9e5b);
}
.channel-status.off {
  color: var(--color-text-tertiary);
}
.adv-block {
  margin: var(--space-sm) 0;
  padding: var(--space-sm) var(--space-md);
  border: 1px dashed var(--border-light);
  border-radius: var(--radius-sm);
}
.adv-block > summary {
  cursor: pointer;
  color: var(--color-text-secondary);
  font-size: 0.82rem;
  user-select: none;
}
.adv-block > summary:hover {
  color: var(--color-primary);
}
.integration-actions {
  display: flex;
  gap: var(--space-sm);
  margin: var(--space-sm) 0;
  align-items: center;
}
.integration-invoke {
  margin-top: var(--space-md);
  padding-top: var(--space-md);
  border-top: 1px dashed var(--border-light);
}
.integration-action-desc {
  color: var(--color-text-tertiary);
  font-size: 0.8rem;
  margin: 0 0 var(--space-xs);
}
.integration-params {
  font-family: var(--font-mono, monospace);
  resize: vertical;
}
.integration-result {
  margin-top: var(--space-sm);
  padding: var(--space-sm);
  background: var(--bg-surface);
  border: 1px solid var(--border-light);
  border-radius: var(--radius-sm);
  color: var(--color-text-secondary);
  font-size: 0.8rem;
  white-space: pre-wrap;
  word-break: break-all;
  max-height: 320px;
  overflow: auto;
}
.audit-error {
  color: var(--color-danger);
  font-size: 0.85rem;
  margin: var(--space-xs) 0 0;
}
</style>
