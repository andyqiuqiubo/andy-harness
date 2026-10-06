import { describe, expect, it } from 'vitest'
import { pickOnboardingTarget } from './onboarding'
import type { Provider } from '../api/types'

function makeProvider(partial: Partial<Provider>): Provider {
  return {
    id: 'x',
    name: 'X',
    base_url: 'https://x',
    models: ['m1'],
    has_api_key: false,
    enabled: true,
    ...partial,
  }
}

describe('pickOnboardingTarget', () => {
  it('列表为空时不引导（后端未就绪/加载失败）', () => {
    expect(pickOnboardingTarget([], false)).toEqual({ needed: false, provider: null })
  })

  it('模拟模式下不引导（无需 Key 也能体验）', () => {
    const providers = [makeProvider({ id: 'deepseek', name: 'DeepSeek' })]
    const r = pickOnboardingTarget(providers, true)
    expect(r.needed).toBe(false)
  })

  it('已有任一可用 provider 配置 Key 时不引导', () => {
    const providers = [
      makeProvider({ id: 'deepseek', name: 'DeepSeek', has_api_key: false }),
      makeProvider({ id: 'custom_x', name: 'X', has_api_key: true }),
    ]
    expect(pickOnboardingTarget(providers, false).needed).toBe(false)
  })

  it('已停用的 provider 即使有 Key 也不算可用', () => {
    const providers = [
      makeProvider({ id: 'deepseek', name: 'DeepSeek' }),
      makeProvider({ id: 'custom_x', name: 'X', has_api_key: true, enabled: false }),
    ]
    const r = pickOnboardingTarget(providers, false)
    expect(r.needed).toBe(true)
    expect(r.provider?.id).toBe('deepseek')
  })

  it('未配置 Key 时引导 deepseek（优先精确命中）', () => {
    const providers = [
      makeProvider({ id: 'custom_a', name: 'A' }),
      makeProvider({ id: 'deepseek', name: 'DeepSeek' }),
    ]
    const r = pickOnboardingTarget(providers, false)
    expect(r.needed).toBe(true)
    expect(r.provider?.id).toBe('deepseek')
  })

  it('没有 deepseek 时按 id 模糊匹配', () => {
    const providers = [makeProvider({ id: 'my-deepseek-proxy', name: 'DS' })]
    const r = pickOnboardingTarget(providers, false)
    expect(r.needed).toBe(true)
    expect(r.provider?.id).toBe('my-deepseek-proxy')
  })

  it('只有非 deepseek 的 provider 时：需要引导但无内嵌表单目标（provider=null，仅跳设置页）', () => {
    const providers = [makeProvider({ id: 'openai', name: 'OpenAI' })]
    const r = pickOnboardingTarget(providers, false)
    expect(r.needed).toBe(true)
    expect(r.provider).toBeNull()
  })
})
