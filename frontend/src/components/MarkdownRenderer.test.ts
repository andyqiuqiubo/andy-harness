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
