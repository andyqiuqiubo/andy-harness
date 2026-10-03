import { flushPromises } from '@vue/test-utils'
import { mount } from '@vue/test-utils'
import { createPinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import SessionSidebar from './SessionSidebar.vue'

const SEARCH_PAYLOAD = [
  {
    session: {
      id: 's1',
      title: 'Docker 讨论',
      message_count: 2,
      archived: false,
      created_at: '2026-09-30 10:00:00',
      updated_at: '2026-09-30 10:00:00',
    },
    title_hit: true,
    msg_hits: 1,
    matches: [
      {
        message_id: 'm1',
        role: 'user',
        snippet: '关于 Docker 的讨论内容',
        created_at: '2026-09-30 10:00:00',
      },
    ],
  },
]

function mountSidebar() {
  return mount(SessionSidebar, {
    global: {
      plugins: [createPinia()],
      directives: { ripple: () => {} },
      stubs: { RouterLink: true },
    },
  })
}

describe('SessionSidebar 搜索（E9）', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url.includes('/sessions/search')) {
        return {
          ok: true,
          status: 200,
          json: async () => SEARCH_PAYLOAD,
        } as Response
      }
      // 会话列表 / 其他请求：空数组
      return { ok: true, status: 200, json: async () => [] } as Response
    })
    vi.stubGlobal('fetch', fetchMock)
  })

  afterEach(() => {
    vi.useRealTimers()
    vi.unstubAllGlobals()
  })

  it('渲染搜索框', () => {
    const wrapper = mountSidebar()
    expect(wrapper.find('.search-input').exists()).toBe(true)
  })

  it('输入后（防抖 300ms）显示搜索结果与片段', async () => {
    const wrapper = mountSidebar()
    await wrapper.find('.search-input').setValue('Docker')
    await vi.advanceTimersByTime(300)
    await flushPromises()

    expect(wrapper.find('.search-result').exists()).toBe(true)
    expect(wrapper.find('.search-result-name').text()).toBe('Docker 讨论')
    expect(wrapper.find('.search-snippet').text()).toContain('Docker')
  })

  it('点 × 清空搜索并恢复正常列表', async () => {
    const wrapper = mountSidebar()
    await wrapper.find('.search-input').setValue('Docker')
    await vi.advanceTimersByTime(300)
    await flushPromises()
    expect(wrapper.find('.search-result').exists()).toBe(true)

    await wrapper.find('.search-clear').trigger('click')
    expect(wrapper.find('.search-result').exists()).toBe(false)
    expect((wrapper.find('.search-input').element as HTMLInputElement).value).toBe('')
  })

  it('无结果时显示空状态', async () => {
    // fetch 对搜索也返回空
    vi.stubGlobal('fetch', vi.fn(async () => ({
      ok: true,
      status: 200,
      json: async () => [],
    })))
    const wrapper = mountSidebar()
    await wrapper.find('.search-input').setValue('不存在的词')
    await vi.advanceTimersByTime(300)
    await flushPromises()

    expect(wrapper.find('.search-result').exists()).toBe(false)
    expect(wrapper.find('.search-hint').exists()).toBe(false)
  })
})
