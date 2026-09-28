<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import type { ProcessStep } from '../api/types'

const props = defineProps<{
  steps: ProcessStep[]
  /** 实时模式：默认展开、内容增长自动跟随滚动、终答开始时自动收缩 */
  live?: boolean
  /** live 模式下的收缩信号：最终答案开始输出时置 true，过程框自动收起（用户仍可手动点开） */
  collapseTrigger?: boolean
}>()

// 展开状态：实时模式默认展开（正在执行），历史模式默认收缩（只展示答案）
const open = ref(!!props.live)
const boxEl = ref<HTMLElement | null>(null)

// 终答开始输出 → 自动收缩（仅 live 模式；只在信号跳变时触发，
// 用户随后手动展开不会被再次强制收起，除非下一轮工具后又输出终答）
watch(
  () => props.collapseTrigger,
  (v) => {
    if (v && props.live) open.value = false
  },
)

const reasoningChars = computed(() =>
  props.steps
    .filter((s) => s.kind === 'reasoning')
    .reduce((n, s) => n + (s.text?.length ?? 0), 0),
)
const toolCount = computed(() => props.steps.filter((s) => s.kind === 'tool').length)

// live 模式：过程内容增长时自动跟随滚动到底部（始终看到最新一行）
watch(
  () => props.steps,
  async () => {
    if (props.live && open.value) {
      await nextTick()
      const el = boxEl.value
      if (el) el.scrollTop = el.scrollHeight
    }
  },
  { deep: true },
)

/** 按工具名推断来源徽标：MCP / Skill / 内置工具。 */
function toolBadge(name: unknown): string {
  const n = String(name ?? '')
  if (n.startsWith('mcp__')) return 'MCP'
  if (n === 'use_skill' || n.startsWith('skill_')) return 'Skill'
  return '工具'
}

/** 单行预览文本（压缩空白 + 截断）。 */
function preview(text: unknown, n = 100): string {
  const t = String(text ?? '')
    .replace(/\s+/g, ' ')
    .trim()
  return t.length > n ? t.slice(0, n) + '…' : t
}

function argsText(args: unknown): string {
  try {
    return JSON.stringify(args ?? {})
  } catch {
    return String(args ?? '')
  }
}
</script>

<template>
  <div :class="['process-trace', { live: props.live, open }]">
    <button type="button" class="process-header" @click="open = !open">
      <svg class="process-chevron" width="14" height="14" viewBox="0 0 24 24" fill="none">
        <path d="M9 18l6-6-6-6" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" />
      </svg>
      <span class="process-title">
        {{ props.live && !props.collapseTrigger ? '执行中' : '执行过程' }}
      </span>
      <svg
        v-if="props.live && !props.collapseTrigger"
        class="process-spinner"
        width="13" height="13" viewBox="0 0 24 24" fill="none"
      >
        <circle cx="12" cy="12" r="10" stroke="currentColor" stroke-width="2.5" stroke-dasharray="40" stroke-dashoffset="26" stroke-linecap="round">
          <animateTransform attributeName="transform" type="rotate" from="0 12 12" to="360 12 12" dur="0.9s" repeatCount="indefinite" />
        </circle>
      </svg>
      <span class="process-meta">
        <template v-if="reasoningChars > 0">思考 {{ reasoningChars }} 字</template>
        <template v-if="reasoningChars > 0 && toolCount > 0"> · </template>
        <template v-if="toolCount > 0">{{ toolCount }} 次工具调用</template>
        <template v-if="reasoningChars === 0 && toolCount === 0">暂无过程记录</template>
      </span>
    </button>

    <div v-show="open" ref="boxEl" class="process-box">
      <template v-for="(s, i) in props.steps" :key="i">
        <!-- 思维链 -->
        <div v-if="s.kind === 'reasoning'" class="proc-reasoning">{{ s.text }}</div>
        <!-- 中间说明文字（模型的阶段性说明，如"先加载该技能"） -->
        <div v-else-if="s.kind === 'text'" class="proc-text">{{ s.text }}</div>
        <!-- 工具调用（MCP / Skill / 内置），可展开查看参数与结果 -->
        <details v-else-if="s.tool" class="proc-tool">
          <summary class="proc-tool-summary">
            <span :class="['proc-tool-badge', toolBadge(s.tool.tool_name).toLowerCase()]">
              {{ toolBadge(s.tool.tool_name) }}
            </span>
            <span class="proc-tool-name">{{ s.tool.tool_name }}</span>
            <span v-if="s.tool.error" class="proc-tool-status err">失败</span>
            <span v-else-if="s.tool.result" class="proc-tool-status ok">完成</span>
          </summary>
          <div class="proc-tool-detail">
            <div v-if="s.tool.result" class="proc-row">
              <span class="proc-label">结果</span>
              <code class="proc-code">{{ preview(s.tool.result, 200) }}</code>
            </div>
            <div v-if="s.tool.error" class="proc-row">
              <span class="proc-label proc-label-err">错误</span>
              <code class="proc-code proc-code-err">{{ s.tool.error }}</code>
            </div>
            <div class="proc-row">
              <span class="proc-label">参数</span>
              <code class="proc-code">{{ argsText(s.tool.args) }}</code>
            </div>
          </div>
        </details>
      </template>
    </div>
  </div>
</template>

<style scoped>
.process-trace {
  border: 1px solid var(--border-color);
  border-radius: var(--radius-md);
  background: var(--bg-tool, var(--bg-surface));
  overflow: hidden;
  font-size: var(--font-size-sm);
}

.process-header {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  padding: 7px 12px;
  border: none;
  background: transparent;
  color: var(--color-text-secondary);
  cursor: pointer;
  user-select: none;
  transition: background var(--transition-base);
  text-align: left;
}

.process-header:hover {
  background: var(--bg-hover);
}

.process-chevron {
  flex-shrink: 0;
  color: var(--color-text-tertiary);
  transition: transform var(--transition-base);
}

.process-trace.open .process-chevron {
  transform: rotate(90deg);
}

.process-title {
  font-weight: 600;
  color: var(--color-text);
  white-space: nowrap;
}

.process-spinner {
  flex-shrink: 0;
  color: var(--color-primary);
}

.process-meta {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: var(--font-size-xs);
  color: var(--color-text-tertiary);
}

/* 过程内容框：高度随内容增长，封顶后出现纵向滚动条 */
.process-box {
  max-height: 260px;
  overflow-y: auto;
  padding: 8px 12px;
  border-top: 1px solid var(--border-color);
  display: flex;
  flex-direction: column;
  gap: 6px;
  scrollbar-width: thin;
}

.process-box::-webkit-scrollbar {
  width: 6px;
}

.process-box::-webkit-scrollbar-thumb {
  background: var(--border-color);
  border-radius: 3px;
}

/* 思维链：弱化样式 */
.proc-reasoning {
  color: var(--color-text-secondary);
  font-style: italic;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-word;
  border-left: 2px solid var(--border-color);
  padding-left: 8px;
}

/* 中间说明文字 */
.proc-text {
  color: var(--color-text-secondary);
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-word;
  border-left: 2px solid var(--color-primary-light, var(--border-color));
  padding-left: 8px;
}

/* 工具调用条目 */
.proc-tool-summary {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 4px 6px;
  border-radius: var(--radius-sm);
  cursor: pointer;
  list-style: none;
  user-select: none;
  transition: background var(--transition-base);
}

.proc-tool-summary::-webkit-details-marker {
  display: none;
}

.proc-tool-summary:hover {
  background: var(--bg-hover);
}

.proc-tool-badge {
  flex-shrink: 0;
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 0.03em;
  padding: 1px 6px;
  border-radius: var(--radius-full);
  background: var(--bg-code);
  color: var(--color-text-secondary);
}

.proc-tool-badge.mcp {
  background: var(--color-primary-light, var(--bg-code));
  color: var(--color-primary);
}

.proc-tool-badge.skill {
  background: var(--color-success-light, var(--bg-code));
  color: var(--color-success);
}

.proc-tool-name {
  flex: 1;
  min-width: 0;
  font-family: var(--font-mono);
  font-size: var(--font-size-xs);
  color: var(--color-text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.proc-tool-status {
  flex-shrink: 0;
  font-size: 10px;
  padding: 1px 6px;
  border-radius: var(--radius-full);
}

.proc-tool-status.ok {
  color: var(--color-success);
  background: var(--color-success-light, var(--bg-code));
}

.proc-tool-status.err {
  color: var(--color-danger);
  background: var(--color-error-bg, var(--bg-code));
}

.proc-tool-detail {
  margin: 2px 0 4px;
  padding: 6px 8px;
  border-left: 2px solid var(--border-color);
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.proc-row {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.proc-label {
  font-size: 10px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  color: var(--color-text-tertiary);
}

.proc-label-err {
  color: var(--color-danger);
}

.proc-code {
  font-family: var(--font-mono);
  font-size: var(--font-size-xs);
  background: var(--bg-code);
  padding: 4px 8px;
  border-radius: var(--radius-sm);
  white-space: pre-wrap;
  word-break: break-all;
  color: var(--color-text);
  line-height: 1.5;
  max-height: 120px;
  overflow-y: auto;
}

.proc-code-err {
  color: var(--color-danger);
}

@media (max-width: 768px) {
  .process-box {
    max-height: 200px;
  }
}
</style>
