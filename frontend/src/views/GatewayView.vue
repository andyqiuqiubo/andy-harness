<script setup lang="ts">
/**
 * LLM 网关管理台（/gateway）—— andy-gateway 管理面前端。
 *
 * 数据全部来自 /api/gateway/*（require_admin）：通道与模型级可用性、API Key
 * 签发/吊销/轮换、租户、用量流水、审计、配置版本回滚。
 *
 * 约定同 InsightsView：useLanguage 的 t 是 ComputedRef（模板 t()、script t.value()）；
 * 样式一律用 src/style.css 设计令牌；紧凑风格。
 */

import { computed, onMounted, ref } from 'vue'
import { apiClient } from '../api/client'
import { useLanguage } from '../composables/useLanguage'

const { t } = useLanguage()

type TabKey = 'channels' | 'keys' | 'tenants' | 'usage' | 'audit' | 'versions'

const TABS: { key: TabKey; label: string }[] = [
  { key: 'channels', label: '通道' },
  { key: 'keys', label: 'API Key' },
  { key: 'tenants', label: '租户' },
  { key: 'usage', label: '用量' },
  { key: 'audit', label: '审计' },
  { key: 'versions', label: '配置版本' },
]

interface ChannelRow {
  id: string
  name: string
  adapter: string
  base_url: string
  auth: string
  stream_only: boolean
  requires_tools: boolean
  weight: number
  strategy: string
  models: string[]
  available: boolean
}
interface ModelRow {
  id: string
  channel_id: string
  model: string
  available: boolean
  cooldown_remaining: number
}
interface KeyRow {
  id: string
  tenant_id: string
  name: string
  prefix: string
  scopes: string[]
  rpm: number
  tpm: number
  expires_at: string | null
  revoked_at: string | null
  last_used_at: string | null
  created_at: string
}
interface TenantRow {
  id: string
  name: string
  status: string
  allowed_models: string[]
  allowed_channels: string[]
  created_at: string
}
interface UsageRow {
  id: number
  ts: string
  tenant_id: string
  channel_id: string
  model: string
  status: number
  prompt_tokens: number
  completion_tokens: number
  total_tokens: number
  duration_ms: number
  ttfb_ms: number
  attempt: number
  error_code: string
}
interface AuditRow {
  id: number
  ts: string
  actor: string
  action: string
  target_type: string
  target_id: string
  detail_json: string
}
interface VersionRow {
  version: number
  checksum: string
  actor: string
  source: string
  applied_at: string
}

const tab = ref<TabKey>('channels')
const loading = ref(false)
const loadError = ref('')
const notice = ref('')

const channels = ref<ChannelRow[]>([])
const models = ref<ModelRow[]>([])
const keys = ref<KeyRow[]>([])
const tenants = ref<TenantRow[]>([])
const usage = ref<UsageRow[]>([])
const audit = ref<AuditRow[]>([])
const versions = ref<VersionRow[]>([])

// ── 签发 Key 表单 ──
const keyForm = ref({ tenant_id: 'default', name: '', scopes: ['chat', 'models'], rpm: 0, tpm: 0 })
const issuedPlain = ref('') // 明文仅展示一次
const issuedName = ref('')

// ── 创建租户表单 ──
const tenantForm = ref({ id: '', name: '', allowed_models: '*', allowed_channels: '*' })

const ALL_SCOPES = ['chat', 'models', 'admin']

const modelsByChannel = computed(() => {
  const map = new Map<string, ModelRow[]>()
  for (const m of models.value) {
    const list = map.get(m.channel_id) || []
    list.push(m)
    map.set(m.channel_id, list)
  }
  return map
})

async function load() {
  loading.value = true
  loadError.value = ''
  try {
    if (tab.value === 'channels') {
      const [ch, mo] = await Promise.all([
        apiClient.get<ChannelRow[]>('/gateway/channels'),
        apiClient.get<ModelRow[]>('/gateway/models'),
      ])
      channels.value = ch || []
      models.value = mo || []
    } else if (tab.value === 'keys') {
      keys.value = (await apiClient.get<KeyRow[]>('/gateway/keys')) || []
    } else if (tab.value === 'tenants') {
      tenants.value = (await apiClient.get<TenantRow[]>('/gateway/tenants')) || []
    } else if (tab.value === 'usage') {
      usage.value = (await apiClient.get<UsageRow[]>('/gateway/usage?limit=100')) || []
    } else if (tab.value === 'audit') {
      audit.value = (await apiClient.get<AuditRow[]>('/gateway/audit?limit=100')) || []
    } else {
      versions.value = (await apiClient.get<VersionRow[]>('/gateway/config/versions?limit=20')) || []
    }
  } catch (e) {
    loadError.value = e instanceof Error ? e.message : String(e)
  } finally {
    loading.value = false
  }
}

function switchTab(next: TabKey) {
  if (tab.value === next) return
  tab.value = next
  notice.value = ''
  issuedPlain.value = ''
  void load()
}

async function issueKey() {
  notice.value = ''
  try {
    const res = await apiClient.post<{ id: string; key: string; prefix: string; tenant_id: string }>(
      '/gateway/keys',
      {
        tenant_id: keyForm.value.tenant_id,
        name: keyForm.value.name || 'api-key',
        scopes: keyForm.value.scopes,
        rpm: Number(keyForm.value.rpm) || 0,
        tpm: Number(keyForm.value.tpm) || 0,
      },
    )
    issuedPlain.value = res.key
    issuedName.value = res.prefix
    keyForm.value.name = ''
    await load()
  } catch (e) {
    notice.value = e instanceof Error ? e.message : String(e)
  }
}

async function revokeKey(id: string) {
  if (!confirm(t.value('gateway.keys.revokeConfirm'))) return
  try {
    await apiClient.post(`/gateway/keys/${encodeURIComponent(id)}/revoke`)
    await load()
  } catch (e) {
    notice.value = e instanceof Error ? e.message : String(e)
  }
}

async function rotateKey(id: string) {
  if (!confirm(t.value('gateway.keys.rotateConfirm'))) return
  try {
    const res = await apiClient.post<{ key: string; prefix: string; old_key_grace_hours: number }>(
      `/gateway/keys/${encodeURIComponent(id)}/rotate`,
    )
    issuedPlain.value = res.key
    issuedName.value = res.prefix
    await load()
  } catch (e) {
    notice.value = e instanceof Error ? e.message : String(e)
  }
}

async function createTenant() {
  notice.value = ''
  try {
    await apiClient.post('/gateway/tenants', {
      id: tenantForm.value.id,
      name: tenantForm.value.name || tenantForm.value.id,
      allowed_models: tenantForm.value.allowed_models
        .split(',')
        .map((s) => s.trim())
        .filter(Boolean),
      allowed_channels: tenantForm.value.allowed_channels
        .split(',')
        .map((s) => s.trim())
        .filter(Boolean),
    })
    tenantForm.value.id = ''
    tenantForm.value.name = ''
    await load()
  } catch (e) {
    notice.value = e instanceof Error ? e.message : String(e)
  }
}

async function rollback(version: number) {
  if (!confirm(`${t.value('gateway.versions.rollbackConfirm')} #${version}`)) return
  try {
    await apiClient.post('/gateway/config/rollback', { version })
    notice.value = `${t.value('gateway.versions.rolledBack')} #${version}`
  } catch (e) {
    notice.value = e instanceof Error ? e.message : String(e)
  }
}

async function copyPlain() {
  try {
    await navigator.clipboard.writeText(issuedPlain.value)
    notice.value = t.value('gateway.keys.copied')
  } catch {
    notice.value = t.value('gateway.keys.copyFailed')
  }
}

function fmtTime(v: string | null | undefined): string {
  if (!v) return '-'
  return v.replace('T', ' ').slice(0, 19)
}

function fmtMs(v: number): string {
  if (!v) return '-'
  return v >= 1000 ? (v / 1000).toFixed(1) + 's' : v + 'ms'
}

function statusClass(status: number): string {
  if (status >= 200 && status < 300) return 'ok'
  if (status === 429) return 'warn'
  return 'err'
}

onMounted(load)
</script>

<template>
  <div class="gateway-view">
    <header class="gateway-header">
      <router-link to="/chat" class="gateway-back">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="15 18 9 12 15 6" /></svg>
        {{ t('gateway.back') }}
      </router-link>
      <h2 class="gateway-title">{{ t('gateway.title') }}</h2>
      <button class="gateway-refresh" :disabled="loading" @click="load">{{ t('gateway.refresh') }}</button>
    </header>

    <nav class="gateway-tabs">
      <button
        v-for="item in TABS"
        :key="item.key"
        :class="['gateway-tab', { active: tab === item.key }]"
        @click="switchTab(item.key)"
      >
        {{ t(`gateway.tab.${item.key}`) }}
      </button>
    </nav>

    <div v-if="loading" class="gateway-hint">{{ t('gateway.loading') }}</div>
    <div v-else-if="loadError" class="gateway-error">{{ loadError }}</div>
    <div v-else-if="notice" class="gateway-notice">{{ notice }}</div>

    <!-- ── 通道 ── -->
    <template v-if="!loading && !loadError && tab === 'channels'">
      <div v-if="channels.length === 0" class="gateway-hint">{{ t('gateway.channels.empty') }}</div>
      <div v-for="ch in channels" :key="ch.id" class="gateway-card">
        <div class="gateway-card-head">
          <span class="gateway-card-title">{{ ch.name || ch.id }}</span>
          <span class="gateway-badge" :class="ch.available ? 'ok' : 'err'">
            {{ ch.available ? t('gateway.available') : t('gateway.cooling') }}
          </span>
        </div>
        <div class="gateway-meta">
          <span>{{ ch.id }}</span><span>{{ ch.adapter }}</span><span>{{ ch.auth }}</span>
          <span v-if="ch.stream_only">{{ t('gateway.streamOnly') }}</span>
          <span v-if="ch.requires_tools">{{ t('gateway.requiresTools') }}</span>
          <span>{{ t('gateway.weight') }} {{ ch.weight }}</span>
        </div>
        <div class="gateway-models">
          <span
            v-for="m in (modelsByChannel.get(ch.id) || [])"
            :key="m.id"
            class="gateway-model"
            :class="{ off: !m.available }"
            :title="m.available ? '' : `${t('gateway.cooldownLeft')} ${m.cooldown_remaining}s`"
          >
            {{ m.model }}
          </span>
        </div>
      </div>
    </template>

    <!-- ── API Key ── -->
    <template v-if="!loading && !loadError && tab === 'keys'">
      <div v-if="issuedPlain" class="gateway-plain">
        <div class="gateway-plain-label">{{ t('gateway.keys.plainOnce') }}（{{ issuedName }}…）</div>
        <code class="gateway-plain-key">{{ issuedPlain }}</code>
        <button class="gateway-btn" @click="copyPlain">{{ t('gateway.keys.copy') }}</button>
      </div>
      <form class="gateway-form" @submit.prevent="issueKey">
        <input v-model="keyForm.name" :placeholder="t('gateway.keys.namePh')" />
        <input v-model="keyForm.tenant_id" :placeholder="t('gateway.keys.tenantPh')" />
        <label v-for="s in ALL_SCOPES" :key="s" class="gateway-check">
          <input v-model="keyForm.scopes" type="checkbox" :value="s" />
          <span>{{ s }}</span>
        </label>
        <input v-model.number="keyForm.rpm" type="number" min="0" :placeholder="t('gateway.keys.rpmPh')" />
        <input v-model.number="keyForm.tpm" type="number" min="0" :placeholder="t('gateway.keys.tpmPh')" />
        <button class="gateway-btn primary" type="submit">{{ t('gateway.keys.issue') }}</button>
      </form>
      <table class="gateway-table">
        <thead>
          <tr>
            <th>{{ t('gateway.keys.colPrefix') }}</th><th>{{ t('gateway.keys.colTenant') }}</th>
            <th>{{ t('gateway.keys.colScopes') }}</th><th>RPM</th><th>TPM</th>
            <th>{{ t('gateway.keys.colLastUsed') }}</th><th>{{ t('gateway.keys.colStatus') }}</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="k in keys" :key="k.id">
            <td><code>{{ k.prefix }}…</code></td>
            <td>{{ k.tenant_id }}</td>
            <td>{{ k.scopes.join(', ') }}</td>
            <td>{{ k.rpm || '-' }}</td>
            <td>{{ k.tpm || '-' }}</td>
            <td>{{ fmtTime(k.last_used_at) }}</td>
            <td>
              <span class="gateway-badge" :class="k.revoked_at ? 'err' : 'ok'">
                {{ k.revoked_at ? t('gateway.keys.revoked') : t('gateway.keys.active') }}
              </span>
            </td>
            <td class="gateway-ops">
              <button v-if="!k.revoked_at" class="gateway-btn small" @click="rotateKey(k.id)">
                {{ t('gateway.keys.rotate') }}
              </button>
              <button v-if="!k.revoked_at" class="gateway-btn small danger" @click="revokeKey(k.id)">
                {{ t('gateway.keys.revoke') }}
              </button>
            </td>
          </tr>
        </tbody>
      </table>
    </template>

    <!-- ── 租户 ── -->
    <template v-if="!loading && !loadError && tab === 'tenants'">
      <form class="gateway-form" @submit.prevent="createTenant">
        <input v-model="tenantForm.id" required :placeholder="t('gateway.tenants.idPh')" />
        <input v-model="tenantForm.name" :placeholder="t('gateway.tenants.namePh')" />
        <input v-model="tenantForm.allowed_models" :placeholder="t('gateway.tenants.modelsPh')" />
        <input v-model="tenantForm.allowed_channels" :placeholder="t('gateway.tenants.channelsPh')" />
        <button class="gateway-btn primary" type="submit">{{ t('gateway.tenants.create') }}</button>
      </form>
      <table class="gateway-table">
        <thead>
          <tr>
            <th>ID</th><th>{{ t('gateway.tenants.colName') }}</th>
            <th>{{ t('gateway.tenants.colModels') }}</th>
            <th>{{ t('gateway.tenants.colChannels') }}</th>
            <th>{{ t('gateway.tenants.colStatus') }}</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="tn in tenants" :key="tn.id">
            <td><code>{{ tn.id }}</code></td>
            <td>{{ tn.name }}</td>
            <td>{{ tn.allowed_models.join(', ') }}</td>
            <td>{{ tn.allowed_channels.join(', ') }}</td>
            <td>
              <span class="gateway-badge" :class="tn.status === 'active' ? 'ok' : 'err'">{{ tn.status }}</span>
            </td>
          </tr>
        </tbody>
      </table>
    </template>

    <!-- ── 用量 ── -->
    <template v-if="!loading && !loadError && tab === 'usage'">
      <div v-if="usage.length === 0" class="gateway-hint">{{ t('gateway.usage.empty') }}</div>
      <table v-else class="gateway-table">
        <thead>
          <tr>
            <th>{{ t('gateway.usage.colTime') }}</th><th>{{ t('gateway.usage.colChannel') }}</th>
            <th>{{ t('gateway.usage.colModel') }}</th><th>{{ t('gateway.usage.colStatus') }}</th>
            <th>{{ t('gateway.usage.colTokens') }}</th><th>{{ t('gateway.usage.colTtfb') }}</th>
            <th>{{ t('gateway.usage.colDuration') }}</th><th>{{ t('gateway.usage.colError') }}</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="u in usage" :key="u.id">
            <td>{{ fmtTime(u.ts) }}</td>
            <td>{{ u.channel_id }}</td>
            <td>{{ u.model }}</td>
            <td><span class="gateway-badge" :class="statusClass(u.status)">{{ u.status }}</span></td>
            <td>{{ u.total_tokens }} ({{ u.prompt_tokens }}+{{ u.completion_tokens }})</td>
            <td>{{ fmtMs(u.ttfb_ms) }}</td>
            <td>{{ fmtMs(u.duration_ms) }}</td>
            <td>{{ u.error_code || '-' }}</td>
          </tr>
        </tbody>
      </table>
    </template>

    <!-- ── 审计 ── -->
    <template v-if="!loading && !loadError && tab === 'audit'">
      <div v-if="audit.length === 0" class="gateway-hint">{{ t('gateway.audit.empty') }}</div>
      <table v-else class="gateway-table">
        <thead>
          <tr>
            <th>{{ t('gateway.audit.colTime') }}</th><th>{{ t('gateway.audit.colActor') }}</th>
            <th>{{ t('gateway.audit.colAction') }}</th><th>{{ t('gateway.audit.colTarget') }}</th>
            <th>{{ t('gateway.audit.colDetail') }}</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="a in audit" :key="a.id">
            <td>{{ fmtTime(a.ts) }}</td>
            <td>{{ a.actor }}</td>
            <td><code>{{ a.action }}</code></td>
            <td>{{ a.target_type }}/{{ a.target_id }}</td>
            <td class="gateway-detail">{{ a.detail_json }}</td>
          </tr>
        </tbody>
      </table>
    </template>

    <!-- ── 配置版本 ── -->
    <template v-if="!loading && !loadError && tab === 'versions'">
      <div v-if="versions.length === 0" class="gateway-hint">{{ t('gateway.versions.empty') }}</div>
      <table v-else class="gateway-table">
        <thead>
          <tr>
            <th>{{ t('gateway.versions.colVersion') }}</th><th>{{ t('gateway.versions.colSource') }}</th>
            <th>{{ t('gateway.versions.colActor') }}</th><th>{{ t('gateway.versions.colTime') }}</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="v in versions" :key="v.version">
            <td>#{{ v.version }}</td>
            <td>{{ v.source }}</td>
            <td>{{ v.actor }}</td>
            <td>{{ fmtTime(v.applied_at) }}</td>
            <td class="gateway-ops">
              <button class="gateway-btn small" @click="rollback(v.version)">
                {{ t('gateway.versions.rollback') }}
              </button>
            </td>
          </tr>
        </tbody>
      </table>
    </template>
  </div>
</template>

<style scoped>
.gateway-view {
  max-width: 1080px;
  margin: 0 auto;
  padding: var(--space-md);
  display: flex;
  flex-direction: column;
  gap: var(--space-md);
}

.gateway-header {
  display: flex;
  align-items: center;
  gap: var(--space-md);
}

.gateway-back {
  display: inline-flex;
  align-items: center;
  gap: var(--space-xs);
  color: var(--color-text-secondary);
  text-decoration: none;
  font-size: var(--font-size-sm);
}

.gateway-back:hover {
  color: var(--color-primary);
}

.gateway-title {
  flex: 1;
  margin: 0;
  font-size: var(--font-size-lg);
  color: var(--color-text);
}

.gateway-refresh {
  padding: var(--space-xs) var(--space-md);
  background: var(--bg-input);
  border: 1px solid var(--border-color);
  border-radius: var(--radius-sm);
  color: var(--color-text-secondary);
  font-size: var(--font-size-xs);
  cursor: pointer;
}

.gateway-refresh:hover {
  color: var(--color-primary);
  border-color: var(--color-primary);
}

.gateway-tabs {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-xs);
  border-bottom: 1px solid var(--border-color);
  padding-bottom: var(--space-xs);
}

.gateway-tab {
  padding: var(--space-xs) var(--space-md);
  background: transparent;
  border: none;
  border-radius: var(--radius-sm);
  color: var(--color-text-secondary);
  font-size: var(--font-size-sm);
  cursor: pointer;
}

.gateway-tab:hover {
  color: var(--color-text);
  background: var(--bg-hover);
}

.gateway-tab.active {
  color: var(--color-primary);
  background: var(--bg-active);
}

.gateway-hint,
.gateway-notice {
  padding: var(--space-sm) var(--space-md);
  font-size: var(--font-size-sm);
  color: var(--color-text-secondary);
}

.gateway-notice {
  background: var(--bg-snapshot);
  border-radius: var(--radius-sm);
  color: var(--color-text);
}

.gateway-error {
  padding: var(--space-sm) var(--space-md);
  font-size: var(--font-size-sm);
  color: var(--color-danger);
  background: var(--bg-error);
  border-radius: var(--radius-sm);
}

.gateway-card {
  background: var(--bg-surface);
  border: 1px solid var(--border-color);
  border-radius: var(--radius-md);
  padding: var(--space-md);
  display: flex;
  flex-direction: column;
  gap: var(--space-sm);
}

.gateway-card-head {
  display: flex;
  align-items: center;
  gap: var(--space-sm);
}

.gateway-card-title {
  flex: 1;
  font-size: var(--font-size-md);
  color: var(--color-text);
}

.gateway-badge {
  padding: 2px var(--space-sm);
  border-radius: var(--radius-full);
  font-size: var(--font-size-xs);
}

.gateway-badge.ok {
  color: var(--color-success);
  background: var(--bg-success);
}

.gateway-badge.warn {
  color: var(--color-warning);
  background: var(--bg-tool);
}

.gateway-badge.err {
  color: var(--color-danger);
  background: var(--bg-error);
}

.gateway-meta {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-md);
  font-size: var(--font-size-xs);
  color: var(--color-text-tertiary);
}

.gateway-models {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-xs);
}

.gateway-model {
  padding: 2px var(--space-sm);
  background: var(--bg-tag);
  border-radius: var(--radius-xs);
  font-family: var(--font-mono);
  font-size: var(--font-size-xs);
  color: var(--color-text-secondary);
}

.gateway-model.off {
  background: var(--bg-error);
  color: var(--color-danger);
  text-decoration: line-through;
}

.gateway-form {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--space-sm);
  padding: var(--space-md);
  background: var(--bg-surface);
  border: 1px solid var(--border-color);
  border-radius: var(--radius-md);
}

.gateway-form input[type='text'],
.gateway-form input:not([type]) {
  padding: var(--space-xs) var(--space-sm);
  background: var(--bg-input);
  border: 1px solid var(--border-color);
  border-radius: var(--radius-sm);
  color: var(--color-text);
  font-size: var(--font-size-sm);
  min-width: 140px;
}

.gateway-check {
  display: inline-flex;
  align-items: center;
  gap: var(--space-xs);
  font-size: var(--font-size-xs);
  color: var(--color-text-secondary);
}

.gateway-btn {
  padding: var(--space-xs) var(--space-md);
  background: var(--bg-input);
  border: 1px solid var(--border-color);
  border-radius: var(--radius-sm);
  color: var(--color-text);
  font-size: var(--font-size-xs);
  cursor: pointer;
}

.gateway-btn:hover {
  border-color: var(--color-primary);
  color: var(--color-primary);
}

.gateway-btn.primary {
  background: var(--color-primary);
  border-color: var(--color-primary);
  color: var(--color-text-inverse);
}

.gateway-btn.small {
  padding: 2px var(--space-sm);
}

.gateway-btn.danger:hover {
  border-color: var(--color-danger);
  color: var(--color-danger);
}

.gateway-plain {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--space-sm);
  padding: var(--space-md);
  background: var(--bg-snapshot);
  border: 1px solid var(--color-primary);
  border-radius: var(--radius-md);
}

.gateway-plain-label {
  font-size: var(--font-size-xs);
  color: var(--color-text-secondary);
}

.gateway-plain-key {
  flex: 1;
  min-width: 260px;
  padding: var(--space-xs) var(--space-sm);
  background: var(--bg-code-block);
  border-radius: var(--radius-xs);
  font-family: var(--font-mono);
  font-size: var(--font-size-xs);
  color: var(--color-primary);
  word-break: break-all;
}

.gateway-table {
  width: 100%;
  border-collapse: collapse;
  font-size: var(--font-size-xs);
}

.gateway-table th {
  text-align: left;
  padding: var(--space-xs) var(--space-sm);
  color: var(--color-text-tertiary);
  font-weight: 500;
  border-bottom: 1px solid var(--border-color);
  white-space: nowrap;
}

.gateway-table td {
  padding: var(--space-xs) var(--space-sm);
  color: var(--color-text-secondary);
  border-bottom: 1px solid var(--border-color);
  vertical-align: top;
}

.gateway-table code {
  font-family: var(--font-mono);
  color: var(--color-text);
}

.gateway-ops {
  display: flex;
  gap: var(--space-xs);
  white-space: nowrap;
}

.gateway-detail {
  max-width: 320px;
  word-break: break-all;
  font-family: var(--font-mono);
}
</style>
