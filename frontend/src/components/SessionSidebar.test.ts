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

describe('SessionSidebar 会话菜单视口防截断（缺陷 1）', () => {
  const SESSIONS = Array.from({ length: 6 }, (_, i) => ({
    id: `s${i + 1}`,
    title: `会话 ${i + 1}`,
    message_count: i,
    archived: false,
    created_at: '2026-10-12 10:00:00',
    updated_at: '2026-10-12 10:00:00',
  }))

  beforeEach(() => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input)
        if (url.includes('/sessions/search')) {
          return { ok: true, status: 200, json: async () => [] } as Response
        }
        if (url.match(/\/api\/sessions\/[^/]+\/messages/)) {
          return { ok: true, status: 200, json: async () => [] } as Response
        }
        if (url.includes('/context-snapshot')) {
          return { ok: true, status: 200, json: async () => null } as Response
        }
        if (url.endsWith('/api/sessions')) {
          return { ok: true, status: 200, json: async () => SESSIONS } as Response
        }
        return { ok: true, status: 200, json: async () => [] } as Response
      }),
    )
    // 视口 1024×700；菜单实测尺寸钉为 268×360（jsdom 无布局，需显式给出量级）
    Object.defineProperty(window, 'innerWidth', { configurable: true, writable: true, value: 1024 })
    Object.defineProperty(window, 'innerHeight', { configurable: true, writable: true, value: 700 })
    Object.defineProperty(HTMLElement.prototype, 'offsetWidth', {
      configurable: true,
      get: () => 268,
    })
    Object.defineProperty(HTMLElement.prototype, 'offsetHeight', {
      configurable: true,
      get: () => 360,
    })
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    // 清理 teleport 到 body 的菜单残留，避免跨用例互相干扰
    document.querySelectorAll('.session-menu').forEach((n) => n.remove())
    Reflect.deleteProperty(HTMLElement.prototype, 'offsetWidth')
    Reflect.deleteProperty(HTMLElement.prototype, 'offsetHeight')
    Reflect.deleteProperty(window, 'innerWidth')
    Reflect.deleteProperty(window, 'innerHeight')
  })

  it('点击贴近视口右下角的 ⋮：菜单完整落入视口，删除选项可见', async () => {
    const wrapper = mountSidebar()
    await flushPromises()
    const items = wrapper.findAll('.session-item')
    expect(items.length).toBe(6)

    // 列表最后一项的 ⋮（视口中最靠下的位置），点击点贴近右下角
    const lastItem = items[items.length - 1]
    await lastItem.find('.menu-btn').trigger('click', { clientX: 1000, clientY: 690 })
    await flushPromises()

    const menu = document.querySelector('.session-menu') as HTMLElement | null
    expect(menu).toBeTruthy()
    const left = parseFloat(menu!.style.left)
    const top = parseFloat(menu!.style.top)
    // 右缘 / 下缘均不越界（8px 安全距）
    expect(left + 268).toBeLessThanOrEqual(1024)
    expect(top + 360).toBeLessThanOrEqual(700)
    expect(left).toBeGreaterThanOrEqual(8)
    expect(top).toBeGreaterThanOrEqual(8)

    // 「删除」选项存在且带危险样式（完整展示、可交互）
    const danger = menu!.querySelector('.menu-item.danger') as HTMLElement | null
    expect(danger).toBeTruthy()
    expect(danger!.textContent).toContain('删除')
    wrapper.unmount()
  })

  it('点击贴近视口左上角：菜单不出现负坐标', async () => {
    const wrapper = mountSidebar()
    await flushPromises()
    const firstBtn = wrapper.findAll('.session-item')[0].find('.menu-btn')
    await firstBtn.trigger('click', { clientX: 4, clientY: 4 })
    await flushPromises()

    const menu = document.querySelector('.session-menu') as HTMLElement | null
    expect(menu).toBeTruthy()
    expect(parseFloat(menu!.style.left)).toBeGreaterThanOrEqual(8)
    expect(parseFloat(menu!.style.top)).toBeGreaterThanOrEqual(8)
    wrapper.unmount()
  })
})
