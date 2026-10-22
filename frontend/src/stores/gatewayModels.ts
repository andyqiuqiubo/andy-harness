/**
 * 网关免费模型可用性 store —— 会话页实时同步模型可用/不可用状态。
 *
 * 设计要点（对应"检测结果与界面实时同步、避免状态延迟或不一致"）：
 * - **唯一事实源在后端**：网关的模型级健康（真实流量冷却）与清单探测结论统一
 *   经 `/api/gateway/model-availability` 暴露。前端不自行推断状态，只镜像后端，
 *   因此绝不会出现"界面显示可用、一调用就挂"或"界面显示不可用、其实已恢复"的错位。
 * - **定期轮询**：默认 20s 拉一次，覆盖后台探测带来的状态变化（恢复 / 新冷却）。
 * - **失败即刷新**：当以 gw-* provider 发送得到 error 帧（网关在请求失败时已同步
 *   把该模型标冷却），立即重拉一次可用性——把"用户撞上不可用 → 界面反应"的延迟
 *   压到一次往返，而不必等下一个轮询周期。
 */

import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { apiClient } from '../api/client'

/** 单个模型的可用性条目（与后端 availability_snapshot 对齐）。 */
export interface ModelAvailability {
  id: string
  available: boolean
  in_catalog: boolean
  cooldown_remaining: number
}

/** 单个通道（厂商）的可用性快照。 */
export interface ChannelAvailability {
  enabled: boolean
  provider_id: string
  label: string
  available: boolean
  models: Record<string, ModelAvailability>
}

interface AvailabilitySnapshot {
  channels: Record<string, ChannelAvailability>
  checked_at?: string
}

const POLL_MS = 20_000

export const useGatewayModelsStore = defineStore('gatewayModels', () => {
  const snapshot = ref<Record<string, ChannelAvailability>>({})
  const loaded = ref(false)
  const loading = ref(false)
  const error = ref<string | null>(null)
  let timer: ReturnType<typeof setInterval> | null = null

  /** 网关免费厂商的 provider id 集合（gw-*）。 */
  const gatewayProviderIds = computed(() =>
    Object.values(snapshot.value)
      .map((c) => c.provider_id)
      .filter((id): id is string => Boolean(id)),
  )

  /** provider id → 通道（反查表，由快照内容推出，不依赖前端硬编码）。 */
  const providerToChannel = computed(() => {
    const map: Record<string, string> = {}
    for (const [channel, info] of Object.entries(snapshot.value)) {
      if (info.provider_id) map[info.provider_id] = channel
    }
    return map
  })

  /** 所有当前不可用的模型完整 id（`channel/model`）。 */
  const unavailableModelIds = computed(() => {
    const set = new Set<string>()
    for (const info of Object.values(snapshot.value)) {
      for (const m of Object.values(info.models || {})) {
        if (!m.available) set.add(m.id)
      }
    }
    return set
  })

  /** 某 provider 的通道是否整体不可用（全部模型都冷却/下架）。 */
  function providerUnavailable(providerId: string): boolean {
    const channel = providerToChannel.value[providerId]
    if (!channel) return false
    const info = snapshot.value[channel]
    if (!info || !info.enabled) return false
    return info.available === false
  }

  /**
   * 具体模型是否可用。未被本 store 跟踪的 id 默认放行（返回 true），避免对非
   * 网关模型或快照尚未覆盖的模型误判为不可用。
   */
  function modelAvailable(modelId: string): boolean {
    return !unavailableModelIds.value.has(modelId)
  }

  async function fetch(): Promise<void> {
    loading.value = true
    try {
      const data = await apiClient.get<AvailabilitySnapshot>('/gateway/model-availability')
      snapshot.value = data?.channels || {}
      error.value = null
    } catch (e) {
      // 拉取失败保留上次快照（宁可显示旧状态，也不要清空导致全绿误报"可用"）
      error.value = e instanceof Error ? e.message : String(e)
    } finally {
      loading.value = false
      loaded.value = true
    }
  }

  function startPolling(): void {
    void fetch()
    if (timer) return
    timer = setInterval(() => void fetch(), POLL_MS)
  }

  function stopPolling(): void {
    if (timer) {
      clearInterval(timer)
      timer = null
    }
  }

  /**
   * 一次失败发送后的即时刷新：用 gw-* provider 发消息拿到 error 帧时调用，
   * 此时网关已把失败的模型同步标冷却，立即重拉让界面马上显示"不可用"。
   */
  function refreshAfterFailure(): void {
    void fetch()
  }

  return {
    snapshot,
    loaded,
    loading,
    error,
    gatewayProviderIds,
    providerToChannel,
    unavailableModelIds,
    providerUnavailable,
    modelAvailable,
    fetch,
    startPolling,
    stopPolling,
    refreshAfterFailure,
  }
})
