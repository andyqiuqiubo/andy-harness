/**
 * 回归：被用户停止的回答必须在内容下方明确标注「用户已停止回答」。
 */

import { mount } from '@vue/test-utils'
import { createPinia } from 'pinia'
import { describe, expect, it } from 'vitest'
import type { Message } from '../api/types'
import MessageItem from './MessageItem.vue'

function stoppedMessage(): Message {
  return {
    id: 'm1',
    session_id: 's1',
    role: 'assistant',
    content: '这是被停止前已生成的部分回答内容。',
    tokens: 0,
    created_at: '2026-10-12 10:00:00',
    stopped: true,
  }
}

function mountMessage(message: Message) {
  return mount(MessageItem, {
    props: { message },
    global: { plugins: [createPinia()] },
  })
}

describe('MessageItem 停止标注', () => {
  it('stopped 回答在气泡下方展示「用户已停止回答」', () => {
    const wrapper = mountMessage(stoppedMessage())
    const badge = wrapper.find('.stopped-badge')
    expect(badge.exists()).toBe(true)
    expect(badge.text()).toContain('用户已停止回答')
  })

  it('标注位于回答内容（气泡）之后，即内容下方', () => {
    const wrapper = mountMessage(stoppedMessage())
    const bubble = wrapper.find('.assistant-bubble')
    const badge = wrapper.find('.stopped-badge')
    expect(bubble.exists()).toBe(true)
    expect(badge.exists()).toBe(true)
    // DOM 顺序：bubble 在 badge 之前 → 视觉上标注位于内容下方
    expect(
      bubble.element.compareDocumentPosition(badge.element) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy()
  })

  it('未停止的回答不展示标注', () => {
    const wrapper = mountMessage({ ...stoppedMessage(), stopped: false })
    expect(wrapper.find('.stopped-badge').exists()).toBe(false)
  })

  it('被中断但正文为空（仅思考阶段被停止）仍展示「用户已停止回答」', () => {
    const wrapper = mountMessage({
      id: 'm2',
      session_id: 's1',
      role: 'assistant',
      content: '',
      reasoning: '让我先分析一下用户的问题…',
      tokens: 0,
      created_at: '2026-10-12 10:00:00',
      stopped: true,
    })
    const badge = wrapper.find('.stopped-badge')
    expect(badge.exists()).toBe(true)
    expect(badge.text()).toContain('用户已停止回答')
  })

  it('仅思考被中断的消息携带的思维链可折叠展示', () => {
    const wrapper = mountMessage({
      id: 'm3',
      session_id: 's1',
      role: 'assistant',
      content: '',
      reasoning: '这是被中断前的思考过程。',
      tokens: 0,
      created_at: '2026-10-12 10:00:00',
      stopped: true,
    })
    // 默认展开（无正文），存在思考过程折叠控件
    expect(wrapper.find('.reasoning-toggle').exists()).toBe(true)
    expect(wrapper.find('.assistant-reasoning').isVisible()).toBe(true)
  })
})
