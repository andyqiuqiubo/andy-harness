<script setup lang="ts">
import { computed } from 'vue'
import MarkdownIt from 'markdown-it'
import hljs from 'highlight.js'

const props = defineProps<{ content: string }>()

const md = new MarkdownIt({
  html: true,
  linkify: true,
  highlight(str: string, lang: string): string {
    if (lang && hljs.getLanguage(lang)) {
      try {
        return `<pre class="hljs"><code>${hljs.highlight(str, { language: lang }).value}</code></pre>`
      } catch {
        // fall through
      }
    }
    return `<pre class="hljs"><code>${md.utils.escapeHtml(str)}</code></pre>`
  },
})

const html = computed(() => md.render(props.content || ''))
</script>

<template>
  <div class="markdown-body" v-html="html"></div>
</template>

<style scoped>
.markdown-body {
  font-size: var(--font-size-base);
  line-height: 1.7;
  color: var(--color-text);
}

/* ── Headings ── */
.markdown-body :deep(h1),
.markdown-body :deep(h2),
.markdown-body :deep(h3),
.markdown-body :deep(h4),
.markdown-body :deep(h5),
.markdown-body :deep(h6) {
  font-weight: 600;
  line-height: 1.3;
  margin: 1.2em 0 0.6em;
  color: var(--color-text);
}

.markdown-body :deep(h1) {
  font-size: var(--font-size-xl);
  padding-bottom: 0.3em;
  border-bottom: 1px solid var(--border-color);
}

.markdown-body :deep(h2) {
  font-size: var(--font-size-lg);
  padding-bottom: 0.3em;
  border-bottom: 1px solid var(--border-color);
}

.markdown-body :deep(h3) {
  font-size: var(--font-size-md);
}

.markdown-body :deep(h4) {
  font-size: var(--font-size-base);
}

/* ── Paragraphs ── */
.markdown-body :deep(p) {
  margin: 0.6em 0;
}

/* ── Links ── */
.markdown-body :deep(a) {
  color: var(--color-primary);
  text-decoration: none;
  transition: color var(--transition-base);
}

.markdown-body :deep(a:hover) {
  color: var(--color-primary-hover);
  text-decoration: underline;
}

/* ── Inline code ── */
.markdown-body :deep(code) {
  background: var(--bg-code);
  padding: 0.15em 0.4em;
  border-radius: var(--radius-xs);
  font-size: 0.875em;
  font-family: var(--font-mono);
  color: var(--color-text);
}

/* ── Code blocks ── */
.markdown-body :deep(pre.hljs) {
  border-radius: var(--radius-md);
  padding: 14px 16px;
  overflow-x: auto;
  margin: 0.8em 0;
  background: var(--bg-code-block);
  border: 1px solid var(--border-color);
  box-shadow: var(--shadow-xs);
}

.markdown-body :deep(pre.hljs code) {
  background: none;
  padding: 0;
  border-radius: 0;
  font-size: var(--font-size-sm);
  font-family: var(--font-mono);
  line-height: 1.6;
}

/* ── Lists ── */
.markdown-body :deep(ul),
.markdown-body :deep(ol) {
  padding-left: 1.5em;
  margin: 0.6em 0;
}

.markdown-body :deep(li) {
  margin: 0.3em 0;
}

.markdown-body :deep(li > ul),
.markdown-body :deep(li > ol) {
  margin: 0.3em 0;
}

/* ── Blockquotes ── */
.markdown-body :deep(blockquote) {
  border-left: 3px solid var(--color-primary);
  padding: 4px 16px;
  margin: 0.8em 0;
  background: var(--color-primary-light);
  color: var(--color-text-secondary);
  border-radius: 0 var(--radius-sm) var(--radius-sm) 0;
}

.markdown-body :deep(blockquote p) {
  margin: 0.3em 0;
}

/* ── Tables ── */
.markdown-body :deep(table) {
  border-collapse: collapse;
  width: 100%;
  margin: 0.8em 0;
  font-size: var(--font-size-sm);
}

.markdown-body :deep(th),
.markdown-body :deep(td) {
  border: 1px solid var(--border-color);
  padding: 8px 12px;
  text-align: left;
}

.markdown-body :deep(th) {
  background: var(--bg-code-block);
  font-weight: 600;
  color: var(--color-text);
}

.markdown-body :deep(tr:nth-child(even) td) {
  background: var(--bg-hover);
}

/* ── Horizontal rule ── */
.markdown-body :deep(hr) {
  border: none;
  border-top: 1px solid var(--border-color);
  margin: 1.2em 0;
}

/* ── Images ── */
.markdown-body :deep(img) {
  max-width: 100%;
  border-radius: var(--radius-md);
  margin: 0.6em 0;
}

/* ── Strong / emphasis ── */
.markdown-body :deep(strong) {
  font-weight: 600;
  color: var(--color-text);
}

.markdown-body :deep(em) {
  font-style: italic;
}

/* ── Responsive ── */
@media (max-width: 768px) {
  .markdown-body {
    font-size: var(--font-size-sm);
  }

  .markdown-body :deep(pre.hljs) {
    padding: 10px 12px;
  }

  .markdown-body :deep(th),
  .markdown-body :deep(td) {
    padding: 6px 8px;
  }
}
</style>
