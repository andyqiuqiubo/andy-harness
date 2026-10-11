/**
 * 回归：会话页「厂商 + 模型」选择必须跨页面保留。
 *
 * 用户反馈的缺陷：在会话页选好厂商（如 OortCloud）与具体模型 → 跳到设置页 →
 * 回到会话页，选择被打回 deepseek。
 *
 * 原因：ChatView 没有 KeepAlive，切页即卸载重建，组件内 ref 归零；而旧实现里
 * provider 初值恒为 ''，重建后被「优先 deepseek」接管，模型也随之被校正成
 * deepseek 的模型。修复后从 localStorage 恢复「最后一次选择」。
 */

import { flushPromises, mount } from '@vue/test-utils'
import { createPinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { Provider } from '../api/types'
import ChatView from './ChatView.vue'
import { CHAT_MODEL_KEY, CHAT_PROVIDER_KEY } from '../utils/chatSelection'

const PROVIDERS: Provider[] = [
  {
    id: 'deepseek',
    name: 'DeepSeek',
    base_url: 'https://api.deepseek.com',
    models: ['deepseek-chat'],
    has_api_key: true,
    enabled: true,
  },
  {
    id: 'custom_oortcloud',
    name: 'OortCloud',
    base_url: 'https://example.com/bus/apaas-newapi/v1',
    models: ['glm-5', 'qwen3.8'],
    has_api_key: true,
    enabled: true,
  },
]

/** jsdom 无真实 WS，替换为不做任何事的最小实现（本测试不关心连接）。 */
class NoopWebSocket {
  static readonly CONNECTING = 0
  static readonly OPEN = 1
  static readonly CLOSING = 2
  static readonly CLOSED = 3
  readyState = NoopWebSocket.CONNECTING
  url: string
  onopen: (() => void) | null = null
  onclose: (() => void) | null = null
  onerror: (() => void) | null = null
  onmessage: (() => void) | null = null
  constructor(url: string) {
    this.url = url
  }
  send() {}
  close() {}
}

/** 当前用例的 provider 列表（fetch 桩按它返回，供不同用例替换）。 */
let currentProviders: Provider[] = PROVIDERS

function mountChat(providers: Provider[] = PROVIDERS) {
  currentProviders = providers
  const pinia = createPinia()
  return mount(ChatView, {
    global: {
      plugins: [pinia],
      // main.ts 里注册的全局指令，测试环境补一个空实现
      directives: { ripple: {} },
      // 只关心顶栏的厂商/模型选择，其余子组件与本用例无关
      stubs: {
        SessionSidebar: true,
        MessageItem: true,
        AttachmentImage: true,
        MarkdownRenderer: true,
        ProcessTrace: true,
        RouterLink: true,
      },
    },
  })
}

/** 读取下拉当前值（断言用）。 */
function selectValue(wrapper: ReturnType<typeof mountChat>, selector: string): string {
  const el = wrapper.find(selector).element as HTMLSelectElement
  return el.value
}

/** 读取下拉里的全部选项值（断言"不该出现"用）。 */
function optionValues(wrapper: ReturnType<typeof mountChat>, selector: string): string[] {
  return wrapper
    .findAll(`${selector} option`)
    .map((o) => (o.element as HTMLOptionElement).value)
}

describe('ChatView：厂商/模型选择跨页面保留', () => {
  beforeEach(() => {
    localStorage.clear()
    vi.stubGlobal('WebSocket', NoopWebSocket)
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string) => {
        const u = String(url)
        let body: unknown = {}
        if (u.includes('/providers')) body = currentProviders
        else if (u.includes('/todos')) body = { todos: [] }
        else if (u.includes('/sessions')) body = []
        else if (u.includes('/models')) body = []
        else if (u.includes('/settings')) body = {}
        else if (u.includes('/auth/status')) body = { enabled: false }
        return { ok: true, status: 200, json: async () => body } as Response
      }),
    )
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    localStorage.clear()
  })

  it('无历史选择时：保持既有体验，优先 deepseek', async () => {
    const w = mountChat()
    await flushPromises()
    expect(selectValue(w, '.provider-select')).toBe('deepseek')
    w.unmount()
  })

  it('记住最后一次选择：切页（卸载重建）后仍是上次选的厂商与模型', async () => {
    // ① 第一次进入会话页：用户选 OortCloud + glm-5
    const first = mountChat()
    await flushPromises()
    await first.find('.provider-select').setValue('custom_oortcloud')
    await flushPromises()
    await first.find('.model-selector').setValue('glm-5')
    await flushPromises()

    // 选择应已落盘（供重建时恢复）
    expect(localStorage.getItem(CHAT_PROVIDER_KEY)).toBe('custom_oortcloud')
    expect(localStorage.getItem(CHAT_MODEL_KEY)).toBe('glm-5')

    // ② 跳设置页 → ChatView 卸载
    first.unmount()

    // ③ 回到会话页 → 重建：必须是上次的选择，而不是 deepseek
    const second = mountChat()
    await flushPromises()
    expect(selectValue(second, '.provider-select')).toBe('custom_oortcloud')
    expect(selectValue(second, '.model-selector')).toBe('glm-5')
    second.unmount()
  })

  it('已选的厂商被删除时：回退到 deepseek，不留失效选择', async () => {
    localStorage.setItem(CHAT_PROVIDER_KEY, 'removed-vendor')
    localStorage.setItem(CHAT_MODEL_KEY, 'ghost-model')

    const w = mountChat()
    await flushPromises()
    expect(selectValue(w, '.provider-select')).toBe('deepseek')
    // 失效模型也应被校正为该 provider 的可用模型
    expect(selectValue(w, '.model-selector')).toBe('deepseek-chat')
    w.unmount()
  })

  it('已停用 / 未配 Key 的厂商不出现在选框里（用户反馈：停用了还存在）', async () => {
    // 复刻真实库现状：deepseek 已停用，OortCloud 可用
    const w = mountChat([
      { ...PROVIDERS[0]!, enabled: false }, // deepseek：已停用
      { ...PROVIDERS[1]! }, // custom_oortcloud：可用
    ])
    await flushPromises()

    expect(optionValues(w, '.provider-select')).toEqual(['custom_oortcloud'])
    // 自动落到唯一可用厂商，而不是停在停用的 deepseek
    expect(selectValue(w, '.provider-select')).toBe('custom_oortcloud')
    w.unmount()
  })

  it('未配 API Key 的厂商也不出现在选框里', async () => {
    const w = mountChat([
      { ...PROVIDERS[0]!, has_api_key: false }, // deepseek：缺 Key
      { ...PROVIDERS[1]! },
    ])
    await flushPromises()

    expect(optionValues(w, '.provider-select')).toEqual(['custom_oortcloud'])
    expect(selectValue(w, '.provider-select')).toBe('custom_oortcloud')
    w.unmount()
  })

  it('已从厂商配置中删除的模型不再出现在模型选框里', async () => {
    // OortCloud 原本有 glm-5 / qwen3.8，用户在设置页删掉了 qwen3.8
    const w = mountChat([{ ...PROVIDERS[1]!, models: ['glm-5'] }])
    await flushPromises()

    expect(optionValues(w, '.model-selector')).toEqual(['glm-5'])
    expect(selectValue(w, '.model-selector')).toBe('glm-5')
    w.unmount()
  })

  it('全部厂商都停用时：不显示选框，只提示「模型全部已停用」', async () => {
    const w = mountChat([{ ...PROVIDERS[0]!, enabled: false }])
    await flushPromises()

    expect(w.find('.provider-select').exists()).toBe(false)
    expect(w.text()).toContain('模型全部已停用')
    w.unmount()
  })
})
