/**
 * gatewayModels store：可用性镜像 + 实时同步语义。
 *
 * 验证「后端是唯一事实源」：store 只镜像 /gateway/model-availability，
 * modelAvailable/providerUnavailable 完全由快照驱动；拉取失败保留上次快照
 * （绝不因一次网络抖动把所有模型误判为可用）。
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { apiClient } from '../api/client'
import { useGatewayModelsStore } from './gatewayModels'

const SNAPSHOT = {
  channels: {
    cline: {
      enabled: true,
      provider_id: 'gw-cline',
      label: 'Cline',
      available: true,
      models: {
        'cline-free/minimax-m3.1-flash': {
          id: 'cline/cline-free/minimax-m3.1-flash',
          available: true,
          in_catalog: true,
          cooldown_remaining: 0,
        },
        'cline-free/gone': {
          id: 'cline/cline-free/gone',
          available: false,
          in_catalog: false,
          cooldown_remaining: 0,
        },
      },
    },
    workbuddy: {
      enabled: true,
      provider_id: 'gw-workbuddy',
      label: 'WorkBuddy',
      available: false,
      models: {
        hy3: { id: 'workbuddy/hy3', available: false, in_catalog: true, cooldown_remaining: 30 },
      },
    },
  },
}

describe('useGatewayModelsStore', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('mirrors backend snapshot: modelAvailable / providerUnavailable', async () => {
    const store = useGatewayModelsStore()
    const get = vi.spyOn(apiClient, 'get').mockResolvedValue(SNAPSHOT as never)

    await store.fetch()

    expect(get).toHaveBeenCalledWith('/gateway/model-availability')
    // 可用模型放行；下架/冷却模型标不可用
    expect(store.modelAvailable('cline/cline-free/minimax-m3.1-flash')).toBe(true)
    expect(store.modelAvailable('cline/cline-free/gone')).toBe(false)
    // 未跟踪的模型（非网关 / 其它厂商）默认放行，绝不误判
    expect(store.modelAvailable('deepseek/deepseek-flash')).toBe(true)
    // provider → channel 反查 + 整体不可用判定
    expect(store.providerToChannel['gw-cline']).toBe('cline')
    expect(store.providerUnavailable('gw-workbuddy')).toBe(true)
    expect(store.providerUnavailable('gw-cline')).toBe(false)
    expect(store.gatewayProviderIds).toEqual(['gw-cline', 'gw-workbuddy'])
  })

  it('keeps last snapshot when fetch fails (no false "all available")', async () => {
    const store = useGatewayModelsStore()
    vi.spyOn(apiClient, 'get').mockResolvedValue(SNAPSHOT as never)
    await store.fetch()
    expect(store.modelAvailable('cline/cline-free/gone')).toBe(false)

    // 网络失败：保留上次结论，不可用模型仍不可用
    vi.spyOn(apiClient, 'get').mockRejectedValue(new Error('network down'))
    await store.fetch()
    expect(store.error).toBe('network down')
    expect(store.modelAvailable('cline/cline-free/gone')).toBe(false)
    expect(store.snapshot.cline).toBeDefined()
  })

  it('startPolling fetches immediately and stops cleanly', async () => {
    vi.useFakeTimers()
    try {
      const store = useGatewayModelsStore()
      const get = vi.spyOn(apiClient, 'get').mockResolvedValue(SNAPSHOT as never)
      store.startPolling()
      await vi.advanceTimersByTimeAsync(0) // 冲刷立即 fetch 的微任务
      expect(get).toHaveBeenCalledTimes(1)
      await vi.advanceTimersByTimeAsync(20_000) // 一个轮询周期
      expect(get).toHaveBeenCalledTimes(2)
      store.stopPolling()
      await vi.advanceTimersByTimeAsync(60_000) // 停轮询后不再拉取
      expect(get).toHaveBeenCalledTimes(2)
    } finally {
      vi.useRealTimers()
    }
  })
})
