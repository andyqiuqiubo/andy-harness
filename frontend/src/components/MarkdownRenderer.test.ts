import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import MarkdownRenderer from './MarkdownRenderer.vue'

describe('MarkdownRenderer', () => {
  it('把 markdown 渲染为 HTML', () => {
    const wrapper = mount(MarkdownRenderer, {
      props: { content: '# 标题\n\n一段 **加粗** 文本。' },
    })
    const body = wrapper.find('.markdown-body')
    expect(body.find('h1').text()).toBe('标题')
    expect(body.find('strong').text()).toBe('加粗')
  })

  it('渲染代码块并加 hljs 样式', () => {
    const wrapper = mount(MarkdownRenderer, {
      props: { content: '```python\nprint(1)\n```' },
    })
    const pre = wrapper.find('pre.hljs')
    expect(pre.exists()).toBe(true)
    expect(pre.find('code').text()).toContain('print(1)')
  })

  it('渲染链接（linkify）', () => {
    const wrapper = mount(MarkdownRenderer, {
      props: { content: '访问 https://example.com 即可' },
    })
    const a = wrapper.find('a')
    expect(a.exists()).toBe(true)
    expect(a.attributes('href')).toBe('https://example.com')
  })

  it('空内容不报错', () => {
    const wrapper = mount(MarkdownRenderer, { props: { content: '' } })
    expect(wrapper.find('.markdown-body').exists()).toBe(true)
  })
})

/**
 * SEC-06 修复验证：MarkdownRenderer 必须把 HTML 当纯文本转义，
 * 绝不允许原始 HTML 进入 v-html 注入面。
 */
describe('MarkdownRenderer XSS 防护', () => {
  it('转义 script 标签，不产生可执行脚本', () => {
    const wrapper = mount(MarkdownRenderer, {
      props: { content: '<script>window.__xss__=1</script>' },
    })
    const el = wrapper.element as HTMLElement
    // 不得存在真实 script 元素
    expect(el.querySelectorAll('script')).toHaveLength(0)
    // 必须以转义文本形式出现（&lt;script&gt;）
    expect(el.innerHTML).toContain('&lt;script&gt;')
  })

  it('转义 img onerror 事件处理器', () => {
    const wrapper = mount(MarkdownRenderer, {
      props: { content: '<img src=x onerror="window.__xss2__=1">' },
    })
    const el = wrapper.element as HTMLElement
    // 不得产生真实 img 元素（onerror 无从触发）
    expect(el.querySelectorAll('img')).toHaveLength(0)
    expect(el.innerHTML).toContain('&lt;img')
  })

  it('转义 iframe / svg onload 等危险标签', () => {
    const wrapper = mount(MarkdownRenderer, {
      props: { content: '<iframe src="https://evil.test"></iframe><svg onload="alert(1)"></svg>' },
    })
    const el = wrapper.element as HTMLElement
    expect(el.querySelectorAll('iframe')).toHaveLength(0)
    expect(el.querySelectorAll('svg')).toHaveLength(0)
    expect(el.innerHTML).toContain('&lt;iframe')
  })

  it('javascript: 协议链接被 markdown-it 默认规则拦截', () => {
    const wrapper = mount(MarkdownRenderer, {
      props: { content: '[click me](javascript:window.__xss3__=1)' },
    })
    const el = wrapper.element as HTMLElement
    // 不得生成可点击的 <a href="javascript:...">
    expect(el.querySelectorAll('a')).toHaveLength(0)
    expect(el.innerHTML).not.toMatch(/<a[^>]+javascript:/i)
  })

  it('代码块中的 HTML 被转义，不破坏 DOM', () => {
    const wrapper = mount(MarkdownRenderer, {
      props: { content: '```html\n<div onclick="evil()">x</div>\n```' },
    })
    const el = wrapper.element as HTMLElement
    expect(el.querySelectorAll('div')).toHaveLength(0)
    expect(el.querySelector('pre code')?.innerHTML).toContain('&lt;')
  })

  it('外链补充 target=_blank 与 rel=noopener noreferrer', () => {
    const wrapper = mount(MarkdownRenderer, {
      props: { content: '[site](https://example.com)' },
    })
    const a = wrapper.element.querySelector('a')
    expect(a).not.toBeNull()
    expect(a?.getAttribute('target')).toBe('_blank')
    expect(a?.getAttribute('rel')).toContain('noopener')
    expect(a?.getAttribute('rel')).toContain('noreferrer')
  })
})
