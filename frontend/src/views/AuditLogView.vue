<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { apiClient } from '../api/client'
import { formatLocalTime } from '../utils/datetime'
import { useLanguage } from '../composables/useLanguage'

const { t } = useLanguage()

interface AuditRow {
  id: number
  created_at: string
  user_id: string | null
  session_id: string | null
  tool_name: string
  risk: string
  action: string
  stage: string
  outcome: string | null
  reason: string | null
  policy_mode: string | null
  decided_by: string | null
}

interface AuditStats {
  by_action?: Record<string, number>
  by_risk?: Record<string, number>
  by_decided_by?: Record<string, number>
  top_tools?: { tool_name: string; count: number }[]
  timeline_24h?: { bucket: string; count: number }[]
}

const filters = ref({
  tool_name: '',
  risk: '',
  action: '',
  stage: '',
  decided_by: '',
})
const items = ref<AuditRow[]>([])
const total = ref(0)
const limit = ref(50)
const offset = ref(0)
const loading = ref(false)
const stats = ref<AuditStats | null>(null)
const error = ref('')

function buildQuery(): string {
  const p: string[] = []
  const f = filters.value
  if (f.tool_name) p.push(`tool_name=${encodeURIComponent(f.tool_name)}`)
  if (f.risk) p.push(`risk=${encodeURIComponent(f.risk)}`)
  if (f.action) p.push(`action=${encodeURIComponent(f.action)}`)
  if (f.stage) p.push(`stage=${encodeURIComponent(f.stage)}`)
  if (f.decided_by) p.push(`decided_by=${encodeURIComponent(f.decided_by)}`)
  p.push(`limit=${limit.value}`)
  p.push(`offset=${offset.value}`)
  return p.join('&')
}

async function loadStats(): Promise<void> {
  try {
    stats.value = await apiClient.get<AuditStats>('/permissions/audit/stats')
  } catch {
    stats.value = null
  }
}

async function load(): Promise<void> {
  loading.value = true
  error.value = ''
  try {
    const data = await apiClient.get<{ total: number; items: AuditRow[] }>(
      `/permissions/audit?${buildQuery()}`,
    )
    items.value = data.items
    total.value = data.total
  } catch (e) {
    items.value = []
    total.value = 0
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    loading.value = false
  }
}

function onSearch(): void {
  offset.value = 0
  void load()
}

function prevPage(): void {
  if (offset.value === 0) return
  offset.value = Math.max(0, offset.value - limit.value)
  void load()
}

function nextPage(): void {
  if (offset.value + limit.value >= total.value) return
  offset.value += limit.value
  void load()
}

onMounted(() => {
  void load()
  void loadStats()
})
</script>

<template>
  <div class="audit-view">
    <h2 class="section-title">{{ t('audit.title') }}</h2>
    <p class="audit-desc">
      {{ t('audit.desc') }}
    </p>

    <div class="audit-stats" v-if="stats">
      <div class="audit-stat">
        <span class="audit-stat-label">{{ t('audit.stat.allow') }}</span>
        <span class="audit-stat-val">{{ stats.by_action?.allow ?? 0 }}</span>
      </div>
      <div class="audit-stat">
        <span class="audit-stat-label">{{ t('audit.stat.confirm') }}</span>
        <span class="audit-stat-val">{{ stats.by_action?.confirm ?? 0 }}</span>
      </div>
      <div class="audit-stat">
        <span class="audit-stat-label">{{ t('audit.stat.deny') }}</span>
        <span class="audit-stat-val">{{ stats.by_action?.deny ?? 0 }}</span>
      </div>
      <div class="audit-stat">
        <span class="audit-stat-label">{{ t('audit.stat.policy') }}</span>
        <span class="audit-stat-val">{{ stats.by_decided_by?.policy ?? 0 }}</span>
      </div>
      <div class="audit-stat">
        <span class="audit-stat-label">{{ t('audit.stat.override') }}</span>
        <span class="audit-stat-val">{{ stats.by_decided_by?.override ?? 0 }}</span>
      </div>
      <div class="audit-stat">
        <span class="audit-stat-label">{{ t('audit.stat.human') }}</span>
        <span class="audit-stat-val">{{ stats.by_decided_by?.human ?? 0 }}</span>
      </div>
    </div>

    <div class="audit-filters">
      <input class="form-input" v-model="filters.tool_name" :placeholder="t('audit.filter.toolName')" />
      <select class="form-input" v-model="filters.risk">
        <option value="">{{ t('audit.filter.all') }}</option>
        <option value="read">{{ t('audit.risk.read') }}</option>
        <option value="write">{{ t('audit.risk.write') }}</option>
        <option value="dangerous">{{ t('audit.risk.dangerous') }}</option>
      </select>
      <select class="form-input" v-model="filters.action">
        <option value="">{{ t('audit.filter.all') }}</option>
        <option value="allow">{{ t('audit.action.allow') }}</option>
        <option value="confirm">{{ t('audit.action.confirm') }}</option>
        <option value="deny">{{ t('audit.action.deny') }}</option>
      </select>
      <select class="form-input" v-model="filters.stage">
        <option value="">{{ t('audit.filter.all') }}</option>
        <option value="decision">{{ t('audit.stage.decision') }}</option>
        <option value="resolved">{{ t('audit.stage.resolved') }}</option>
      </select>
      <select class="form-input" v-model="filters.decided_by">
        <option value="">{{ t('audit.filter.all') }}</option>
        <option value="policy">{{ t('audit.decider.policy') }}</option>
        <option value="override">{{ t('audit.decider.override') }}</option>
        <option value="human">{{ t('audit.decider.human') }}</option>
      </select>
      <button class="btn-primary btn-sm" @click="onSearch">{{ t('audit.filter.search') }}</button>
    </div>

    <p v-if="error" class="audit-error">{{ error }}</p>

    <div class="audit-table-wrap">
      <table class="audit-table">
        <thead>
          <tr>
            <th>{{ t('audit.col.time') }}</th>
            <th>{{ t('audit.col.user') }}</th>
            <th>{{ t('audit.col.tool') }}</th>
            <th>{{ t('audit.col.risk') }}</th>
            <th>{{ t('audit.col.action') }}</th>
            <th>{{ t('audit.col.stage') }}</th>
            <th>{{ t('audit.col.outcome') }}</th>
            <th>{{ t('audit.col.decider') }}</th>
            <th>{{ t('audit.col.reason') }}</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in items" :key="row.id">
            <td class="audit-time">{{ formatLocalTime(row.created_at) }}</td>
            <td>{{ row.user_id || t('audit.system') }}</td>
            <td class="audit-tool">{{ row.tool_name }}</td>
            <td><span :class="['audit-badge', 'risk-' + row.risk]">{{ row.risk ? t('audit.risk.' + row.risk) : '—' }}</span></td>
            <td>{{ row.action ? t('audit.action.' + row.action) : '—' }}</td>
            <td>{{ row.stage ? t('audit.stage.' + row.stage) : '—' }}</td>
            <td>{{ row.outcome ? t('audit.outcome.' + row.outcome) : t('audit.outcome.none') }}</td>
            <td>{{ row.decided_by ? t('audit.decider.' + row.decided_by) : '—' }}</td>
            <td class="audit-reason" :title="row.reason || ''">{{ row.reason }}</td>
          </tr>
          <tr v-if="!items.length">
            <td colspan="9" class="empty-hint">{{ t('audit.empty') }}</td>
          </tr>
        </tbody>
      </table>
    </div>

    <div class="audit-pager">
      <button class="btn-ghost btn-sm" :disabled="offset === 0 || loading" @click="prevPage">
        {{ t('audit.prev') }}
      </button>
      <span class="audit-page-info">
        {{ t('audit.pageInfo').replace('{0}', String(total)).replace('{1}', String(Math.floor(offset / limit) + 1)) }}
      </span>
      <button
        class="btn-ghost btn-sm"
        :disabled="offset + limit >= total || loading"
        @click="nextPage"
      >
        {{ t('audit.next') }}
      </button>
    </div>
  </div>
</template>

<style scoped>
.audit-view {
  display: flex;
  flex-direction: column;
  gap: var(--space-md);
}

.audit-desc {
  color: var(--color-text-secondary);
  font-size: var(--font-size-sm);
  margin: 0;
}

.audit-stats {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-sm);
}

.audit-stat {
  min-width: 96px;
  padding: var(--space-sm) var(--space-md);
  border-radius: var(--radius-md);
  background: var(--bg-form);
  border: 1px solid var(--border-color);
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 2px;
}

.audit-stat-label {
  font-size: var(--font-size-xs);
  color: var(--color-text-tertiary);
}

.audit-stat-val {
  font-size: var(--font-size-lg);
  font-weight: 700;
  color: var(--color-text);
}

.audit-filters {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-sm);
  align-items: center;
}

.audit-filters .form-input {
  width: auto;
  min-width: 96px;
}

.audit-error {
  color: var(--color-danger);
  font-size: var(--font-size-sm);
}

.audit-table-wrap {
  overflow-x: auto;
  border-radius: var(--radius-md);
  border: 1px solid var(--border-color);
  background: var(--bg-surface);
}

.audit-table {
  width: 100%;
  border-collapse: collapse;
  font-size: var(--font-size-sm);
}

.audit-table th,
.audit-table td {
  padding: var(--space-sm) var(--space-md);
  text-align: left;
  white-space: nowrap;
  border-bottom: 1px solid var(--border-light);
  color: var(--color-text);
}

.audit-table th {
  background: var(--bg-form);
  color: var(--color-text-secondary);
  font-weight: 600;
}

.audit-table tbody tr:hover {
  background: var(--bg-hover);
}

.audit-time {
  color: var(--color-text-secondary);
}

.audit-tool {
  font-family: var(--font-mono, monospace);
  color: var(--color-tag);
  font-weight: 600;
}

.audit-reason {
  max-width: 280px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--color-text-secondary);
}

.audit-badge {
  padding: 1px 8px;
  border-radius: var(--radius-full);
  font-size: var(--font-size-xs);
  font-weight: 600;
}

.risk-read {
  background: var(--color-primary-light);
  color: var(--color-primary);
}

.risk-write {
  background: rgba(212, 136, 0, 0.12);
  color: var(--color-warning);
}

.risk-dangerous {
  background: var(--color-error-bg);
  color: var(--color-danger);
}

.audit-pager {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: var(--space-md);
}

.audit-page-info {
  font-size: var(--font-size-sm);
  color: var(--color-text-secondary);
}
</style>
