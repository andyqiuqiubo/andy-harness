<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { apiClient } from '../api/client'
import { useLanguage } from '../composables/useLanguage'

const { t } = useLanguage()

type RangeKey = '7d' | '30d' | '90d'
const RANGES: { key: RangeKey; label: string }[] = [
  { key: '7d', label: '7D' },
  { key: '30d', label: '30D' },
  { key: '90d', label: '90D' },
]

interface DayPoint {
  date: string
  count?: number
  prompt?: number
  completion?: number
  total?: number
}
interface ToolStat {
  name: string
  calls: number
  errors: number
  avg_ms: number
}
interface Overview {
  range_days: number
  generated_at: string
  totals: {
    messages: number
    sessions: number
    tool_calls: number
    tool_errors: number
    tool_success_rate: number | null
    tokens_total: number
    model_tokens_total: number
    dangerous_ops: number
    active_days: number
  }
  messages_by_day: DayPoint[]
  sessions_by_day: DayPoint[]
  tokens_by_day: DayPoint[]
  top_tools: ToolStat[]
}
interface PluginMetric {
  metric_id: string
  metric_name: string
  data: Record<string, unknown>
}

const range = ref<RangeKey>('7d')
const overview = ref<Overview | null>(null)
const pluginMetrics = ref<PluginMetric[]>([])
const loading = ref(false)
const loadError = ref('')

async function load() {
  loading.value = true
  loadError.value = ''
  try {
    const [ov, pm] = await Promise.all([
      apiClient.get<Overview>(`/insights/overview?range=${range.value}`),
      apiClient.get<PluginMetric[]>(`/insights/plugins?range=${range.value}`).catch(() => [] as PluginMetric[]),
    ])
    overview.value = ov
    pluginMetrics.value = pm || []
  } catch (e) {
    loadError.value = e instanceof Error ? e.message : String(e)
  } finally {
    loading.value = false
  }
}

function switchRange(r: RangeKey) {
  if (range.value === r) return
  range.value = r
  void load()
}

onMounted(load)

// ── 图表数据（零依赖 SVG 柱状图） ─────────────────
const CHART_H = 120
const CHART_W = 640

function shortDate(d: string): string {
  return d.slice(5) // MM-DD
}

const messagesChart = computed(() => {
  const rows = overview.value?.messages_by_day || []
  return buildBars(rows.map((r) => ({ label: shortDate(r.date), value: r.count || 0 })))
})

const tokensChart = computed(() => {
  const rows = overview.value?.tokens_by_day || []
  return buildBars(rows.map((r) => ({ label: shortDate(r.date), value: r.total || 0 })))
})

function buildBars(items: { label: string; value: number }[]) {
  const max = Math.max(1, ...items.map((i) => i.value))
  const n = Math.max(1, items.length)
  // 横轴留出标签空间：每根柱子占位均分
  const slot = CHART_W / n
  const barW = Math.min(28, Math.max(4, slot * 0.6))
  return {
    max,
    bars: items.map((it, i) => ({
      ...it,
      x: Math.round(i * slot + (slot - barW) / 2),
      w: Math.round(barW),
      h: Math.round((it.value / max) * (CHART_H - 24)),
      y: CHART_H - 24 - Math.round((it.value / max) * (CHART_H - 24)),
    })),
  }
}

function fmtNum(n: number | null | undefined): string {
  if (n === null || n === undefined) return '-'
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(1) + 'M'
  if (n >= 1_000) return (n / 1_000).toFixed(1) + 'k'
  return String(n)
}

function fmtPct(v: number | null | undefined): string {
  if (v === null || v === undefined) return '-'
  return Math.round(v * 100) + '%'
}

const summaryCards = computed(() => {
  const tt = overview.value?.totals
  if (!tt) return []
  return [
    { key: 'sessions', label: t.value('insights.cardSessions'), value: fmtNum(tt.sessions) },
    { key: 'messages', label: t.value('insights.cardMessages'), value: fmtNum(tt.messages) },
    { key: 'tokens', label: t.value('insights.cardTokens'), value: fmtNum(tt.model_tokens_total) },
    { key: 'toolCalls', label: t.value('insights.cardToolCalls'), value: fmtNum(tt.tool_calls) },
    {
      key: 'success',
      label: t.value('insights.cardSuccessRate'),
      value: fmtPct(tt.tool_success_rate),
    },
    { key: 'dangerous', label: t.value('insights.cardDangerous'), value: fmtNum(tt.dangerous_ops) },
  ]
})

function maxToolCalls(tools: ToolStat[]): number {
  return Math.max(1, ...tools.map((x) => x.calls))
}
</script>

<template>
  <div class="insights-view">
    <header class="insights-header">
      <router-link to="/chat" class="insights-back">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="15 18 9 12 15 6" /></svg>
        {{ t('insights.back') }}
      </router-link>
      <h2 class="insights-title">{{ t('insights.title') }}</h2>
      <div class="range-switch">
        <button
          v-for="r in RANGES"
          :key="r.key"
          :class="['range-btn', { active: range === r.key }]"
          @click="switchRange(r.key)"
        >
          {{ r.label }}
        </button>
      </div>
    </header>

    <div v-if="loading" class="empty-hint">{{ t('insights.loading') }}</div>
    <div v-else-if="loadError" class="insights-error">{{ loadError }}</div>

    <template v-else-if="overview">
      <!-- 汇总卡片 -->
      <div class="summary-grid">
        <div v-for="c in summaryCards" :key="c.key" class="summary-card card">
          <span class="summary-value">{{ c.value }}</span>
          <span class="summary-label">{{ c.label }}</span>
        </div>
      </div>

      <!-- 按日图表 -->
      <div class="chart-grid">
        <div class="card chart-card">
          <h3 class="chart-title">{{ t('insights.chartMessages') }}</h3>
          <svg :viewBox="`0 0 ${640} ${120}`" class="chart-svg" preserveAspectRatio="none">
            <g v-for="b in messagesChart.bars" :key="b.label">
              <rect :x="b.x" :y="b.h > 0 ? b.y : CHART_H - 25" :width="b.w" :height="Math.max(b.h, b.value ? 0 : 1)" rx="3"
                :class="['chart-bar', { zero: !b.value }]" />
              <text :x="b.x + b.w / 2" :y="CHART_H - 8" class="chart-xlabel">{{ b.label }}</text>
            </g>
          </svg>
        </div>
        <div class="card chart-card">
          <h3 class="chart-title">{{ t('insights.chartTokens') }}</h3>
          <svg :viewBox="`0 0 ${640} ${120}`" class="chart-svg" preserveAspectRatio="none">
            <g v-for="b in tokensChart.bars" :key="b.label">
              <rect :x="b.x" :y="b.h > 0 ? b.y : CHART_H - 25" :width="b.w" :height="Math.max(b.h, b.value ? 0 : 1)" rx="3"
                :class="['chart-bar chart-bar-token', { zero: !b.value }]" />
              <text :x="b.x + b.w / 2" :y="CHART_H - 8" class="chart-xlabel">{{ b.label }}</text>
            </g>
          </svg>
        </div>
      </div>

      <!-- 工具 Top10 -->
      <div class="card tools-card">
        <h3 class="chart-title">{{ t('insights.topTools') }}</h3>
        <div v-if="overview.top_tools.length" class="tools-table-wrap">
          <table class="tools-table">
          <thead>
            <tr>
              <th>{{ t('insights.toolName') }}</th>
              <th class="num">{{ t('insights.toolCalls') }}</th>
              <th class="num">{{ t('insights.toolErrors') }}</th>
              <th class="num">{{ t('insights.toolAvgMs') }}</th>
              <th class="bar-col"></th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="tool in overview.top_tools" :key="tool.name">
              <td><code class="tool-name">{{ tool.name }}</code></td>
              <td class="num">{{ tool.calls }}</td>
              <td class="num" :class="{ 'num-err': tool.errors > 0 }">{{ tool.errors }}</td>
              <td class="num">{{ tool.avg_ms }}ms</td>
              <td class="bar-col">
                <div class="tool-bar">
                  <div class="tool-bar-fill" :style="{ width: Math.round((tool.calls / maxToolCalls(overview.top_tools)) * 100) + '%' }"></div>
                </div>
              </td>
            </tr>
          </tbody>
          </table>
        </div>
        <p v-else class="empty-hint">{{ t('insights.noToolData') }}</p>
      </div>

      <!-- 插件贡献指标卡 -->
      <template v-if="pluginMetrics.length">
        <h3 class="section-sub">{{ t('insights.pluginMetrics') }}</h3>
        <div class="summary-grid">
          <div v-for="m in pluginMetrics" :key="m.metric_id" class="summary-card card">
            <span class="summary-value">{{ m.data.value ?? '-' }}<span v-if="m.data.unit" class="summary-unit">{{ m.data.unit }}</span></span>
            <span class="summary-label">{{ m.metric_name }}</span>
          </div>
        </div>
      </template>
    </template>
  </div>
</template>

<style scoped>
.insights-view {
  min-height: 100vh;
  background: var(--bg-main);
  padding: var(--space-lg);
  max-width: 1080px;
  margin: 0 auto;
}

.insights-header {
  display: flex;
  align-items: center;
  gap: var(--space-md);
  margin-bottom: var(--space-lg);
}

.insights-back {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: var(--font-size-sm);
  color: var(--color-text-secondary);
  text-decoration: none;
}

.insights-back:hover {
  color: var(--color-primary);
}

.insights-title {
  margin: 0;
  font-size: var(--font-size-lg);
  font-weight: 700;
  color: var(--color-text);
  flex: 1;
}

.range-switch {
  display: flex;
  gap: 4px;
}

.range-btn {
  padding: 5px 14px;
  border: 1px solid var(--border-color);
  border-radius: var(--radius-full);
  background: var(--bg-input);
  color: var(--color-text-secondary);
  font-size: var(--font-size-xs);
  font-weight: 600;
  cursor: pointer;
  transition: var(--transition-base);
}

.range-btn.active {
  border-color: var(--color-primary);
  color: var(--color-primary);
  background: var(--color-primary-light);
}

.summary-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
  gap: var(--space-sm);
  margin-bottom: var(--space-lg);
}

.summary-card {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: var(--space-md);
  text-align: left;
}

.summary-value {
  font-size: 1.5rem;
  font-weight: 700;
  color: var(--color-primary);
  font-variant-numeric: tabular-nums;
}

.summary-unit {
  font-size: 0.75rem;
  font-weight: 500;
  margin-left: 4px;
  color: var(--color-text-secondary);
}

.summary-label {
  font-size: var(--font-size-xs);
  color: var(--color-text-secondary);
}

.chart-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
  gap: var(--space-md);
  margin-bottom: var(--space-lg);
}

.chart-card {
  padding: var(--space-md);
}

.chart-title {
  margin: 0 0 var(--space-sm);
  font-size: var(--font-size-sm);
  font-weight: 600;
  color: var(--color-text);
}

.chart-svg {
  width: 100%;
  height: auto;
  display: block;
}

.chart-bar {
  fill: var(--color-primary);
  opacity: 0.85;
}

.chart-bar-token {
  fill: var(--color-success, #16a34a);
}

.chart-bar.zero {
  opacity: 0.15;
}

.chart-xlabel {
  font-size: 9px;
  fill: var(--color-text-tertiary);
  text-anchor: middle;
}

.tools-card {
  padding: var(--space-md);
}

/* 行数随 Top10 增长：限制最大高度并出现纵向滚动条 */
.tools-table-wrap {
  max-height: 320px;
  overflow-y: auto;
}

.tools-table {
  width: 100%;
  border-collapse: collapse;
  font-size: var(--font-size-sm);
}

.tools-table th {
  text-align: left;
  font-size: var(--font-size-xs);
  font-weight: 600;
  color: var(--color-text-tertiary);
  padding: 6px 8px;
  border-bottom: 1px solid var(--border-light);
}

.tools-table td {
  padding: 7px 8px;
  border-bottom: 1px solid var(--border-light);
  color: var(--color-text);
}

.tools-table .num {
  text-align: right;
  font-variant-numeric: tabular-nums;
}

.tools-table .num-err {
  color: var(--color-danger);
  font-weight: 600;
}

.bar-col {
  width: 34%;
}

.tool-bar {
  height: 8px;
  border-radius: var(--radius-full);
  background: var(--bg-input);
  overflow: hidden;
}

.tool-bar-fill {
  height: 100%;
  border-radius: var(--radius-full);
  background: linear-gradient(90deg, var(--color-primary), var(--color-primary-dark));
}

.tool-name {
  font-family: var(--font-mono, monospace);
  font-size: var(--font-size-xs);
  background: var(--bg-tag);
  padding: 2px 8px;
  border-radius: var(--radius-sm);
}

.section-sub {
  margin: 0 0 var(--space-sm);
  font-size: var(--font-size-sm);
  font-weight: 600;
  color: var(--color-text);
}

.insights-error {
  padding: var(--space-md);
  border: 1px solid var(--border-error, var(--color-danger));
  border-radius: var(--radius-md);
  color: var(--color-danger);
  font-size: var(--font-size-sm);
}

.empty-hint {
  color: var(--color-text-tertiary);
  font-size: var(--font-size-sm);
  padding: var(--space-md) 0;
}
</style>
