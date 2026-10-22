/**
 * GatewayView 测试：网关管理台的数据渲染与交互。
 *
 * 覆盖：
 * - 通道分区：通道卡片 + 模型级可用性徽章（冷却模型保留可见）
 * - Key 分区：列表渲染 + 签发表单调 POST /gateway/keys
 * - 用量分区：状态徽章与 token 展示
 */

import { flushPromises, mount } from '@vue/test-utils'
import { createPinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import GatewayView from './GatewayView.vue'

const CHANNELS = [
  {
    id: 'workbuddy',
    name: 'WorkBuddy 国内版',
    adapter: 'openai-like',
    base_url: 'https://copilot.tencent.com/v2',
    auth: 'workbuddy-oauth',
    stream_only: true,
    requires_tools: false,
    weight: 3,
    strategy: 'weighted',
    models: ['hy3', 'step-5-preview'],
    available: true,
  },
  {
    id: 'zen',
    name: 'OpenCode Zen Free',
    adapter: 'openai-like',
    base_url: 'https://opencode.ai/inference/v1',
    auth: 'public',
    stream_only: true,
    requires_tools: true,
    weight: 1,
    strategy: 'weighted',
    models: ['big-pickle'],
    available: false,
  },
]

const MODELS = [
  { id: 'workbuddy/hy3', channel_id: 'workbuddy', model: 'hy3', available: true, cooldown_remaining: 0 },
  {
    id: 'workbuddy/step-5-preview',
    channel_id: 'workbuddy',
    model: 'step-5-preview',
    available: false,
    cooldown_remaining: 42,
  },
  { id: 'zen/big-pickle', channel_id: 'zen', model: 'big-pickle', available: false, cooldown_remaining: 17 },
]

const KEYS = [
  {
    id: 'k1',
    tenant_id: 'default',
    name: 'bootstrap',
    prefix: 'agw-ab12',
    scopes: ['chat', 'models', 'admin'],
    rpm: 0,
    tpm: 0,
    expires_at: null,
    revoked_at: null,
    last_used_at: '2026-10-22T10:00:00',
    created_at: '2026-10-22T09:00:00',
  },
]

const USAGE = [
  {
    id: 1,
    ts: '2026-10-22T10:00:00',
    tenant_id: 'default',
    channel_id: 'workbuddy',
    model: 'hy3',
    status: 200,
    prompt_tokens: 10,
    completion_tokens: 5,
    total_tokens: 15,
    duration_ms: 2400,
    ttfb_ms: 800,
    attempt: 1,
    error_code: '',
  },
]

function mountGateway() {
  const pinia = createPinia()
  return mount(GatewayView, {
    global: {
      plugins: [pinia],
      // 测试环境不挂路由器，stub 掉返回链接
      stubs: { RouterLink: true },
    },
  })
}

describe('GatewayView', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
  })

  it('renders channels with per-model availability', async () => {
    const client = (await import('../api/client')).apiClient
    const get = vi.fn(async (url: string) => {
      if (url.startsWith('/gateway/channels')) return CHANNELS
      if (url.startsWith('/gateway/models')) return MODELS
      return []
    })
    vi.spyOn(client, 'get').mockImplementation(get as never)

    const wrapper = mountGateway()
    await flushPromises()

    // 两个通道卡片
    expect(wrapper.text()).toContain('WorkBuddy 国内版')
    expect(wrapper.text()).toContain('OpenCode Zen Free')
    // 冷却中的通道有徽章
    expect(wrapper.text()).toContain('冷却中')
    // 模型逐个渲染；冷却模型保留可见（line-through 由 off class 表达）
    expect(wrapper.text()).toContain('hy3')
    expect(wrapper.text()).toContain('step-5-preview')
    const off = wrapper.findAll('.gateway-model.off')
    expect(off.length).toBe(2)
  })

  it('lists keys and issues a new one via POST', async () => {
    const client = (await import('../api/client')).apiClient
    const get = vi.fn(async (url: string) => {
      if (url.startsWith('/gateway/keys')) return KEYS
      return []
    })
    const post = vi.fn(async () => ({ id: 'k2', key: 'agw-newplain', prefix: 'agw-cd34', tenant_id: 'default' }))
    vi.spyOn(client, 'get').mockImplementation(get as never)
    vi.spyOn(client, 'post').mockImplementation(post as never)

    const wrapper = mountGateway()
    await flushPromises()

    // 切到 API Key 分区
    const tabs = wrapper.findAll('.gateway-tab')
    await tabs[1].trigger('click')
    await flushPromises()

    expect(wrapper.text()).toContain('agw-ab12')
    // 提交签发表单
    await wrapper.find('form.gateway-form').trigger('submit.prevent')
    await flushPromises()
    expect(post).toHaveBeenCalledWith('/gateway/keys', expect.objectContaining({ tenant_id: 'default' }))
    // 明文仅一次展示
    expect(wrapper.text()).toContain('agw-newplain')
  })

  it('renders usage rows with status badges', async () => {
    const client = (await import('../api/client')).apiClient
    vi.spyOn(client, 'get').mockImplementation((async (url: string) => {
      if (url.startsWith('/gateway/usage')) return USAGE
      return []
    }) as never)

    const wrapper = mountGateway()
    await flushPromises()
    const tabs = wrapper.findAll('.gateway-tab')
    await tabs[3].trigger('click')
    await flushPromises()

    expect(wrapper.text()).toContain('hy3')
    expect(wrapper.text()).toContain('15 (10+5)')
    const badge = wrapper.find('.gateway-badge.ok')
    expect(badge.exists()).toBe(true)
    expect(badge.text()).toBe('200')
  })
})
